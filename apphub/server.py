"""Dependency-free management HTTP API and static dashboard."""
import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import tempfile
import threading
import time
import zipfile
from urllib.parse import parse_qs, urlsplit

from .engine import Hub, parse_env
from .packages import MAX_UPLOAD

WEB = Path(__file__).parent / "web"


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, hub):
        self.hub = hub
        self.failures = {}
        self.auth_lock = threading.Lock()
        self.request_slots = threading.BoundedSemaphore(64)
        self.setup_token_path = hub.data / ".setup-token"
        self.setup_token = ""
        if not hub.setting("password"):
            if not self.setup_token_path.exists():
                fd = os.open(self.setup_token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as out:
                    out.write(secrets.token_urlsafe(32))
            self.setup_token_path.chmod(0o600)
            self.setup_token = self.setup_token_path.read_text().strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.setup_token):
                raise RuntimeError("The setup-token file is damaged. Remove .setup-token from the private data volume and restart the hub.")
        else:
            self.setup_token_path.unlink(missing_ok=True)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "AppHub/1.0"
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def log_message(self, format, *args):
        # Paths may contain app names; never log credentials or request bodies.
        print(f"{self.client_address[0]} {format % args}", flush=True)

    def send_data(self, code, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src http: https:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def error(self, code, message):
        self.send_data(code, {"error": message})

    def host_allowed(self):
        host = self.headers.get("Host", "")
        hostname = None
        if len(self.headers.get_all("Host", [])) != 1 or any(c.isspace() or c in "@/\\?#" for c in host):
            return False
        try:
            parsed = urlsplit("http://" + host)
            hostname = parsed.hostname
            parsed.port  # Reject invalid and out-of-range ports.
            if not hostname:
                return False
        except ValueError:
            return False
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            allowed = {"localhost", *(s.strip().lower() for s in os.environ.get("APPHUB_ALLOWED_HOSTS", "").split(","))}
            return hostname in allowed

    def same_origin(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host")
        return True  # CLI clients authenticate with an explicit bearer token.

    def authenticated(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        token = hashlib.sha256(header[7:].encode()).hexdigest()
        with self.server.hub.db() as db:
            row = db.execute("SELECT expires FROM sessions WHERE token=?", (token,)).fetchone()
        return bool(row and row[0] > time.time())

    def body_length(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked uploads are not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]+", lengths[0])):
            raise ValueError("Invalid request size.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request size.")
        if size < 0 or size > limit:
            raise ValueError(f"Request exceeds {limit // (1024 * 1024) or 1} MB.")
        return size

    def read_json(self):
        size = self.body_length(64000)
        data = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def auth(self, path):
        hub = self.server.hub
        body = self.read_json()
        password = body.get("password", "")
        if not isinstance(password, str) or len(password) > 1024:
            raise ValueError("Invalid password.")
        with self.server.auth_lock:
            client = self.client_address[0]
            recent = [t for t in self.server.failures.get(client, []) if t > time.time() - 300]
            self.server.failures = {key: [t for t in timestamps if t > time.time() - 300] for key, timestamps in self.server.failures.items() if timestamps and timestamps[-1] > time.time() - 300}
            if len(recent) >= 10:
                self.error(429, "Too many attempts. Try again in five minutes.")
                return
            encoded = hub.setting("password")
            if path == "/api/setup":
                if encoded:
                    self.error(409, "App Hub already has an owner. Sign in instead.")
                    return
                provided = body.get("setup_token", "")
                if not isinstance(provided, str) or not hmac.compare_digest(provided.encode(), self.server.setup_token.encode()):
                    self.server.failures[client] = [*recent, time.time()]
                    self.error(403, "Enter the setup token from your VM terminal.")
                    return
                if len(password) < 12:
                    raise ValueError("Choose a password with at least 12 characters.")
                salt = secrets.token_hex(16)
                encoded = json.dumps({"salt": salt, "hash": password_hash(password, salt)})
                with hub.db() as db:
                    db.execute("INSERT INTO settings(key,value) VALUES('password',?)", (encoded,))
                self.server.setup_token = ""
                self.server.setup_token_path.unlink(missing_ok=True)
            elif not encoded:
                self.error(409, "Create your owner password first.")
                return
            else:
                stored = json.loads(encoded)
                if not hmac.compare_digest(stored["hash"], password_hash(password, stored["salt"])):
                    self.server.failures[client] = [*recent, time.time()]
                    self.error(401, "Incorrect password.")
                    return
            self.server.failures.pop(client, None)
            token = secrets.token_urlsafe(40)
            with hub.db() as db:
                db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
                db.execute("INSERT INTO sessions(token,expires) VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + 86400))
            self.send_data(200, {"token": token})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.connection.settimeout(30)
            if not self.host_allowed():
                self.error(403, "Use the VM IP address or configure APPHUB_ALLOWED_HOSTS.")
                return
            path = urlsplit(self.path).path
            hub = self.server.hub
            if path == "/healthz":
                self.send_data(200, {"status": "ok"})
                return
            if path == "/api/auth/status":
                self.send_data(200, {"setup": bool(hub.setting("password")), "authenticated": self.authenticated()})
                return
            if path.startswith("/api/"):
                if not self.authenticated():
                    self.error(401, "Sign in to continue.")
                    return
                if path == "/api/apps":
                    apps, docker_ok = hub.list_apps()
                    self.send_data(200, {"apps": apps, "docker": docker_ok, "port_start": hub.first_port, "port_end": hub.last_port})
                elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})", path):
                    self.send_data(200, hub.job(match[1]))
                elif match := re.fullmatch(r"/api/apps/([a-f0-9]{12})/logs", path):
                    self.send_data(200, {"log": hub.logs(match[1])})
                elif path == "/api/example":
                    output = io.BytesIO()
                    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.writestr("index.html", (WEB / "example.html").read_text())
                    self.send_data(200, output.getvalue(), "application/zip")
                elif path == "/api/backup":
                    self.send_data(200, {"instructions": "Stop App Hub and back up its data volume plus all apphub-data-* volumes. See README.md for commands."})
                else:
                    self.error(404, "Endpoint not found.")
                return
            asset = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}.get(path)
            if not asset:
                self.error(404, "Page not found.")
                return
            file = WEB / asset
            mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self.send_data(200, file.read_bytes(), mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else ""))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.error(400, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self.error(503, str(exc)[-3000:])
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            print("Request failed:", repr(exc), flush=True)
            self.error(500, "Request failed. Check the App Hub service log.")

    def do_POST(self):
        archive = None
        try:
            self.connection.settimeout(15)
            if not self.host_allowed() or not self.same_origin():
                self.error(403, "Request origin is not allowed.")
                self.close_connection = True
                return
            path = urlsplit(self.path).path
            hub = self.server.hub
            if path in {"/api/setup", "/api/login"}:
                self.auth(path)
                return
            if not self.authenticated():
                self.error(401, "Sign in to continue.")
                self.close_connection = True
                return
            if path == "/api/logout":
                self.read_json()
                token = self.headers.get("Authorization", "")[7:]
                with hub.db() as db:
                    db.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))
                self.send_data(200, {"ok": True})
            elif path == "/api/deploy":
                self.connection.settimeout(120)
                query = parse_qs(urlsplit(self.path).query)
                name = query.get("name", [""])[0]
                app_id = query.get("id", [None])[0]
                mode = query.get("type", ["auto"])[0]
                port_string = query.get("port", [""])[0]
                port = int(port_string) if port_string else None
                if port is not None and not 1 <= port <= 65535:
                    raise ValueError("Internal port must be between 1 and 65535.")
                if mode not in {"auto", "static", "node", "docker"}:
                    raise ValueError("Unknown app type.")
                original_name = query.get("filename", [""])[0]
                extension = Path(original_name).suffix.lower()
                if original_name and extension not in {".zip", ".html", ".htm"}:
                    raise ValueError("Upload a ZIP project or a standalone HTML (.html/.htm) file.")
                # Existing API clients send ZIP bodies without a filename.
                # HTML clients may use text/html when no filename is available.
                if not original_name:
                    extension = ".html" if self.headers.get_content_type() == "text/html" else ".zip"
                if extension in {".html", ".htm"}:
                    if mode not in {"auto", "static"}:
                        raise ValueError("A standalone HTML file uses Static app type. Use a ZIP for Node or Docker projects.")
                    mode = "static"
                # Settings use a separate authenticated JSON request, so secrets
                # never appear in URLs, request logs, or upload filenames.
                settings_id = self.headers.get("X-AppHub-Settings", "")
                if not re.fullmatch(r"[a-f0-9]{32}", settings_id):
                    raise ValueError("Upload settings are missing. Select the file again.")
                with hub.db() as db:
                    entry = db.execute("SELECT value FROM settings WHERE key=?", ("upload:" + settings_id,)).fetchone()
                    if not entry:
                        raise ValueError("Upload settings expired. Try again.")
                    saved = json.loads(entry[0])
                    if saved["expires"] < time.time():
                        raise ValueError("Upload settings expired. Try again.")
                    env = saved["env"]
                    db.execute("DELETE FROM settings WHERE key=?", ("upload:" + settings_id,))
                length = self.body_length(MAX_UPLOAD)
                if not length:
                    raise ValueError("Select an HTML file or ZIP project first.")
                incoming = hub.data / "incoming"
                incoming.mkdir(exist_ok=True)
                fd, filename = tempfile.mkstemp(suffix=extension, dir=incoming)
                archive = Path(filename)
                with os.fdopen(fd, "wb") as out:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError("File upload was interrupted.")
                        out.write(chunk)
                        remaining -= len(chunk)
                result = hub.enqueue(archive, name=name, app_id=app_id, mode=mode, port=port, env=env)
                archive = None  # Ownership transferred to the deployment worker.
                self.send_data(202, result)
            elif path == "/api/upload-settings":
                body = self.read_json()
                env_text = body.get("env", "")
                if not isinstance(env_text, str):
                    raise ValueError("Environment settings must be text.")
                env = parse_env(env_text)
                settings_id = secrets.token_hex(16)
                with hub.db() as db:
                    # Bound temporary settings and remove expired entries.
                    for row in db.execute("SELECT key,value FROM settings WHERE key LIKE 'upload:%'").fetchall():
                        if json.loads(row["value"])["expires"] < time.time():
                            db.execute("DELETE FROM settings WHERE key=?", (row["key"],))
                    count = db.execute("SELECT count(*) FROM settings WHERE key LIKE 'upload:%'").fetchone()[0]
                    if count >= 20:
                        raise ValueError("Too many pending uploads. Try again in a few minutes.")
                    db.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("upload:" + settings_id, json.dumps({"env": env, "expires": time.time() + 600})))
                self.send_data(200, {"id": settings_id})
            elif match := re.fullmatch(r"/api/apps/([a-f0-9]{12})/(start|stop|restart|delete|rollback)", path):
                body = self.read_json()
                if match[2] == "rollback":
                    self.send_data(202, hub.rollback(match[1], body.get("version", "")))
                else:
                    hub.action(match[1], match[2])
                    self.send_data(200, {"ok": True})
            else:
                self.close_connection = True
                self.error(404, "Endpoint not found.")
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.close_connection = True
            self.error(400, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self.close_connection = True
            self.error(503, str(exc)[-3000:])
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            print("Request failed:", repr(exc), flush=True)
            self.close_connection = True
            self.error(500, "Request failed. Check the App Hub service log.")
        finally:
            if archive:
                archive.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="App Hub: upload a ZIP, launch an app.")
    parser.add_argument("--host", default=os.environ.get("APPHUB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("APPHUB_PORT", "8080")))
    parser.add_argument("--data", default=os.environ.get("APPHUB_DATA", "./data"))
    parser.add_argument("--reset-password", action="store_true")
    parser.add_argument("--show-setup-token", action="store_true", help="Read the first-run token locally without starting another server")
    args = parser.parse_args()
    if args.show_setup_token:
        token_path = Path(args.data).resolve() / ".setup-token"
        if not token_path.is_file():
            raise SystemExit("No setup token. Start the hub first; an existing owner does not need a token.")
        print(token_path.read_text().strip())
        return
    hub = Hub(args.data, recover=not args.reset_password)
    if args.reset_password:
        import getpass
        password = getpass.getpass("New owner password (at least 12 characters): ")
        if len(password) < 12 or password != getpass.getpass("Repeat password: "):
            hub.close()
            raise SystemExit("Passwords must match and contain at least 12 characters.")
        salt = secrets.token_hex(16)
        with hub.db() as db:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('password',?)", (json.dumps({"salt": salt, "hash": password_hash(password, salt)}),))
            db.execute("DELETE FROM sessions")
        (hub.data / ".setup-token").unlink(missing_ok=True)
        hub.close()
        print("Owner password updated; existing sessions revoked.")
        return
    server = Server((args.host, args.port), hub)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"App Hub ready at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        hub.close()


if __name__ == "__main__":
    main()
