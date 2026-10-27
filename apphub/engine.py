"""Persistent app registry and serialized Docker deployments. No Docker SDK needed."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import codecs
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from .packages import unpack, prepare

BUSY = {"queued", "building", "starting"}
LABEL = "com.apphub.managed=1"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_env(text):
    result = {}
    if len(text) > 32000:
        raise ValueError("Environment settings are too large.")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ValueError("Environment settings must use KEY=value, one per line.")
        if "\x00" in value:
            raise ValueError("Environment values cannot contain NUL characters.")
        result[key.strip()] = value
    return result


def redact(text, env):
    values = sorted({value for value in env.values() if value}, key=len, reverse=True)
    if not values:
        return text
    return re.sub("|".join(re.escape(value) for value in values), lambda _: "[redacted]", text)


class StreamingRedactor:
    """Keep enough lookahead to redact values crossing subprocess read boundaries."""
    def __init__(self, env, emit):
        values = sorted({value for value in env.values() if value}, key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(v) for v in values)) if values else None
        self.lookahead = max(map(len, values), default=1) - 1
        self.pending = ""
        self.emit = emit

    def write(self, text, *, final=False):
        self.pending += text
        limit = len(self.pending) if final else max(0, len(self.pending) - self.lookahead)
        position = 0
        output = []
        while position < limit:
            match = self.pattern.match(self.pending, position) if self.pattern else None
            if match:
                output.append("[redacted]")
                position = match.end()
            else:
                output.append(self.pending[position])
                position += 1
        self.pending = self.pending[position:]
        if output:
            self.emit("".join(output))

    def close(self):
        self.write("", final=True)


class Docker:
    """Small command runner with bounded logs and process-group timeouts."""
    def run(self, args, *, cwd=None, timeout=30, log=None, input_text=None):
        try:
            process = subprocess.Popen(["docker", *args], cwd=cwd,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except FileNotFoundError as exc:
            raise RuntimeError("Docker CLI is missing. Start App Hub using the supplied Compose file.") from exc
        if input_text is not None:
            process.stdin.write(input_text.encode())
            process.stdin.close()
        output = bytearray()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        started = time.monotonic()
        try:
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    raise TimeoutError(f"Docker command timed out after {timeout} seconds.")
                for key, _ in selector.select(0.25):
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > 256000:
                        del output[:-256000]
                    if log:
                        log(decoder.decode(chunk))
            if log:
                log(decoder.decode(b"", final=True))
            code = process.wait(timeout=5)
            message = output.decode("utf-8", "replace")
            if code:
                raise RuntimeError(message[-10000:] or f"Docker exited with code {code}.")
            return message
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


class Hub:
    def __init__(self, data, docker=None, *, recover=True):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.data.chmod(0o700)
        self.instance_lock = (self.data / ".instance.lock").open("w")
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.instance_lock.close()
            raise RuntimeError("Another App Hub instance is using this data folder.") from exc
        self.db_path = self.data / "hub.sqlite3"
        self.mutex = threading.RLock()
        self.docker = docker or Docker()
        self.network = os.environ.get("APPHUB_NETWORK", "apphub-runtime")
        self.bind_ip = os.environ.get("APPHUB_BIND_IP", "0.0.0.0")
        self.first_port = int(os.environ.get("APPHUB_PORT_START", "10000"))
        self.last_port = int(os.environ.get("APPHUB_PORT_END", "10999"))
        if not 1024 <= self.first_port <= self.last_port <= 65535:
            raise ValueError("App port range must be between 1024 and 65535.")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deploy")
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS apps (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, port INTEGER UNIQUE NOT NULL,
                    status TEXT NOT NULL, current_version TEXT, error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS releases (
                    version TEXT PRIMARY KEY, app_id TEXT NOT NULL, image TEXT NOT NULL,
                    mode TEXT NOT NULL, container_port INTEGER NOT NULL, health_path TEXT NOT NULL,
                    env TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, app_id TEXT NOT NULL, status TEXT NOT NULL,
                    log TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, finished_at TEXT);
            ''')
        self.db_path.chmod(0o600)
        if recover:
            self.recover()

    @contextmanager
    def db(self):
        with self.mutex:
            db = sqlite3.connect(self.db_path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def close(self):
        self.pool.shutdown(wait=True)
        self.instance_lock.close()

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='failed', finished_at=?, log=log || ? WHERE status IN ('queued','building','starting')",
                (now(), "\nDeployment interrupted by a hub restart. Upload again to retry.\n"))
            db.execute("UPDATE apps SET status=CASE WHEN current_version IS NULL THEN 'failed' ELSE 'running' END, error=? WHERE status IN ('queued','building','starting')",
                ("Previous deployment was interrupted. Check the app status and redeploy if needed.",))
            apps = [dict(a) for a in db.execute("SELECT * FROM apps")]
        # A crash between stopping the old version and committing the new pointer
        # restores the committed version; only this hub's labeled containers count.
        try:
            ids = self.docker.run(["ps", "-aq", "--filter", "label=" + LABEL], timeout=5).split()
            for identifier in ids:
                info = json.loads(self.docker.run(["inspect", identifier], timeout=5))[0]
                labels = info.get("Config", {}).get("Labels", {}) or {}
                app = next((a for a in apps if a["id"] == labels.get("com.apphub.app")), None)
                version = labels.get("com.apphub.version")
                if app and version != app["current_version"]:
                    self.docker.run(["rm", "-f", identifier], timeout=15)
            for app in apps:
                if app["current_version"] and app["status"] == "running":
                    try:
                        self.docker.run(["start", self.container_name(app["id"], app["current_version"])], timeout=20)
                    except RuntimeError:
                        pass
        except (RuntimeError, TimeoutError, json.JSONDecodeError):
            pass
        shutil.rmtree(self.data / "builds", ignore_errors=True)
        shutil.rmtree(self.data / "incoming", ignore_errors=True)

    def setting(self, key):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def app(self, app_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
            if not row:
                raise ValueError("App does not exist.")
            return dict(row)

    def release(self, version):
        with self.db() as db:
            row = db.execute("SELECT * FROM releases WHERE version=?", (version,)).fetchone()
            if not row:
                raise ValueError("Version does not exist.")
            result = dict(row)
            result["env"] = json.loads(result["env"])
            return result

    @staticmethod
    def container_name(app_id, version):
        return f"apphub-{app_id}-{version}"

    def list_apps(self):
        states = {}
        available = True
        try:
            lines = self.docker.run(["ps", "-a", "--filter", "label=" + LABEL, "--format", "{{.Names}}\t{{.State}}"], timeout=5)
            states = dict(line.split("\t", 1) for line in lines.splitlines() if "\t" in line)
        except (RuntimeError, TimeoutError):
            available = False
        with self.db() as db:
            apps = [dict(row) for row in db.execute("SELECT * FROM apps ORDER BY created_at DESC")]
            for app in apps:
                release = db.execute("SELECT * FROM releases WHERE version=?", (app["current_version"],)).fetchone()
                app["type"] = release["mode"] if release else None
                app["container_port"] = release["container_port"] if release else None
                app["env_keys"] = list(json.loads(release["env"])) if release else []
                app["versions"] = [dict(v) for v in db.execute("SELECT version, mode, created_at FROM releases WHERE app_id=? ORDER BY created_at DESC, rowid DESC", (app["id"],))]
                job = db.execute("SELECT id, status FROM jobs WHERE app_id=? ORDER BY rowid DESC LIMIT 1", (app["id"],)).fetchone()
                app["job"] = dict(job) if job else None
                if app["status"] not in BUSY and app["current_version"]:
                    app["status"] = states.get(self.container_name(app["id"], app["current_version"]), "missing") if available else "unknown"
        return apps, available

    def log(self, job_id, text):
        with self.db() as db:
            db.execute("UPDATE jobs SET log=substr(log || ?, -120000) WHERE id=?", (text, job_id))

    def status(self, app_id, job_id, value):
        with self.db() as db:
            db.execute("UPDATE apps SET status=?, updated_at=? WHERE id=?", (value, now(), app_id))
            db.execute("UPDATE jobs SET status=? WHERE id=?", (value, job_id))

    def enqueue(self, archive, *, name="", app_id=None, mode="auto", port=None, env=None):
        env = env or {}
        name = name.strip()[:80]
        with self.db() as db:
            if app_id:
                app = self.app(app_id)
                if app["status"] in BUSY:
                    raise ValueError("This app already has a deployment in progress.")
                if not name:
                    name = app["name"]
            else:
                if not name:
                    raise ValueError("Give your app a name.")
                used = {r[0] for r in db.execute("SELECT port FROM apps")}
                used.add(int(os.environ.get("APPHUB_PUBLIC_PORT", os.environ.get("APPHUB_PORT", "8080"))))
                host_port = next((p for p in range(self.first_port, self.last_port + 1) if p not in used), None)
                if host_port is None:
                    raise ValueError("All app ports are in use. Increase APPHUB_PORT_END.")
                app_id = uuid.uuid4().hex[:12]
                db.execute("INSERT INTO apps(id,name,port,status,created_at,updated_at) VALUES(?,?,?,'queued',?,?)", (app_id, name, host_port, now(), now()))
            job_id = uuid.uuid4().hex
            db.execute("UPDATE apps SET name=?, status='queued', error='', updated_at=? WHERE id=?", (name, now(), app_id))
            db.execute("INSERT INTO jobs(id,app_id,status,created_at) VALUES(?,?,'queued',?)", (job_id, app_id, now()))
        self.pool.submit(self.deploy, app_id, job_id, Path(archive), mode, port, env)
        return {"app_id": app_id, "job_id": job_id}

    def deploy(self, app_id, job_id, archive, mode, port, env):
        work = self.data / "builds" / job_id
        try:
            self.status(app_id, job_id, "building")
            self.log(job_id, f"App ID: {app_id}\nPersistent volume: apphub-data-{app_id} → /data\n")
            self.log(job_id, "Preparing HTML file as index.html…\n" if archive.suffix.lower() in {".html", ".htm"} else "Unpacking ZIP and identifying the application…\n")
            root = unpack(archive, work)
            config = prepare(root, mode, port)
            app = self.app(app_id)
            previous = self.release(app["current_version"]) if app["current_version"] else None
            merged_env = {**(previous["env"] if previous else {}), **env}
            version = uuid.uuid4().hex[:12]
            image = f"apphub/{app_id}:{version}"
            self.log(job_id, f"Building {config['mode']} app. Internal port: {config['container_port']}.\n")
            stream = StreamingRedactor(merged_env, lambda s: self.log(job_id, s))
            try:
                self.docker.run(["build", "--label", LABEL, "--label", f"com.apphub.app={app_id}", "-t", image, "-f", config["dockerfile"], "."],
                    cwd=root, timeout=900, log=stream.write)
            finally:
                stream.close()
            release = {"version": version, "app_id": app_id, "image": image,
                "mode": config["mode"], "container_port": config["container_port"], "health_path": config["health_path"], "env": merged_env}
            self.activate(app, release, previous, job_id)
        except Exception as exc:
            self.failed(app_id, job_id, str(exc), env)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)

    def activate(self, app, release, previous, job_id):
        app_id = app["id"]
        name = self.container_name(app_id, release["version"])
        old_name = self.container_name(app_id, previous["version"]) if previous else None
        old_was_running = False
        try:
            self.status(app_id, job_id, "starting")
            try:
                self.docker.run(["network", "inspect", self.network], timeout=10)
            except RuntimeError:
                self.docker.run(["network", "create", self.network], timeout=15)
            if old_name:
                try:
                    old_was_running = self.docker.run(["inspect", "--format", "{{.State.Running}}", old_name], timeout=5).strip() == "true"
                except RuntimeError:
                    pass
                if old_was_running:
                    self.docker.run(["stop", "--time", "15", old_name], timeout=25)
            args = ["create", "--name", name, "--label", LABEL, "--label", f"com.apphub.app={app_id}",
                "--label", f"com.apphub.version={release['version']}", "--network", self.network,
                "--restart", "unless-stopped", "--memory", os.environ.get("APPHUB_APP_MEMORY", "512m"),
                "--cpus", os.environ.get("APPHUB_APP_CPUS", "1"), "--pids-limit", "256",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                "-p", f"{self.bind_ip}:{app['port']}:{release['container_port']}",
                "--mount", f"type=volume,src=apphub-data-{app_id},dst=/data"]
            self.log(job_id, "Starting app and checking its HTTP response…\n")
            # Values stay out of process arguments and are removed after create.
            with tempfile.NamedTemporaryFile(mode="w", prefix=".container-env-", dir=self.data) as env_file:
                for key, value in release["env"].items():
                    env_file.write(f"{key}={value}\n")
                env_file.flush()
                args.extend(["--env-file", env_file.name, release["image"]])
                self.docker.run(args, timeout=30)
            self.docker.run(["start", name], timeout=30)
            self.wait_ready(name, release)
            # Commit the pointer before removing the old container: restart recovery
            # can always identify the last successful deployment.
            self.log(job_id, f"Ready. Your app is available on port {app['port']}.\n")
            with self.db() as db:
                db.execute("INSERT OR IGNORE INTO releases(version,app_id,image,mode,container_port,health_path,env,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (release["version"], app_id, release["image"], release["mode"], release["container_port"], release["health_path"], json.dumps(release["env"]), now()))
                db.execute("UPDATE apps SET current_version=?, status='running', error='', updated_at=? WHERE id=?", (release["version"], now(), app_id))
                db.execute("UPDATE jobs SET status='succeeded', finished_at=? WHERE id=?", (now(), job_id))
        except Exception:
            try:
                logs = self.docker.run(["logs", "--tail", "60", name], timeout=10)
                self.log(job_id, redact(logs, release["env"]))
            except (RuntimeError, TimeoutError):
                pass
            try:
                self.docker.run(["rm", "-f", name], timeout=15)
            except (RuntimeError, TimeoutError):
                pass
            if old_name and old_was_running:
                try:
                    self.docker.run(["start", old_name], timeout=30)
                    self.log(job_id, "Previous version restarted.\n")
                except Exception as exc:
                    known = {**release["env"], **previous["env"]}
                    self.log(job_id, "Previous version could not restart: " + redact(str(exc), known)[:1000])
            raise
        if old_name:
            try:
                self.docker.run(["rm", old_name], timeout=15)
            except (RuntimeError, TimeoutError):
                self.log(job_id, "Old container cleanup deferred until hub restart.\n")

    def wait_ready(self, name, release):
        deadline = time.monotonic() + int(os.environ.get("APPHUB_START_TIMEOUT", "90"))
        last_error = "no HTTP response"
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        while time.monotonic() < deadline:
            info = json.loads(self.docker.run(["inspect", name], timeout=5))[0]
            if info["State"]["Status"] in {"exited", "dead"}:
                raise RuntimeError("App exited before it was ready. Check the startup log.")
            address = info["NetworkSettings"]["Networks"].get(self.network, {}).get("IPAddress")
            if address:
                url = f"http://{address}:{release['container_port']}{release['health_path']}"
                try:
                    with opener.open(urllib.request.Request(url, headers={"User-Agent": "AppHub-health"}), timeout=2) as response:
                        if response.status < 400:
                            return
                except urllib.error.HTTPError as exc:
                    try:
                        if 300 <= exc.code < 400 or exc.code in {401, 403}:
                            return
                        last_error = f"HTTP {exc.code} at {release['health_path']}"
                    finally:
                        exc.close()
                except (OSError, urllib.error.URLError) as exc:
                    last_error = str(exc)
            time.sleep(0.5)
        raise RuntimeError("App did not become ready: " + last_error[:300] + ". Check the internal port, listen address (0.0.0.0), and health_path.")

    def failed(self, app_id, job_id, message, env=None):
        app = self.app(app_id)
        secrets = env or {}
        if app["current_version"]:
            secrets = {**self.release(app["current_version"])["env"], **secrets}
        message = redact(message, secrets)[-6000:]
        self.log(job_id, "\nDeployment failed: " + message + "\n")
        with self.db() as db:
            db.execute("UPDATE apps SET status=?, error=?, updated_at=? WHERE id=?", ("running" if app["current_version"] else "failed", message, now(), app_id))
            db.execute("UPDATE jobs SET status='failed', finished_at=? WHERE id=?", (now(), job_id))

    def rollback(self, app_id, version):
        with self.db() as db:
            app = self.app(app_id)
            if app["status"] in BUSY:
                raise ValueError("Wait for the current deployment to finish.")
            target = self.release(version)
            if target["app_id"] != app_id or version == app["current_version"]:
                raise ValueError("Choose a different version belonging to this app.")
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,app_id,status,created_at) VALUES(?,?,'queued',?)", (job_id, app_id, now()))
            db.execute("UPDATE apps SET status='queued' WHERE id=?", (app_id,))
        def work():
            try:
                previous = self.release(app["current_version"]) if app["current_version"] else None
                self.log(job_id, "Restoring previous app image and environment. Saved data is unchanged.\n")
                self.activate(app, target, previous, job_id)
            except Exception as exc:
                self.failed(app_id, job_id, str(exc), target["env"])
        self.pool.submit(work)
        return {"app_id": app_id, "job_id": job_id}

    def action(self, app_id, action):
        with self.mutex:
            app = self.app(app_id)
            if app["status"] in BUSY:
                raise ValueError("Wait for the deployment to finish.")
            if action not in {"start", "stop", "restart", "delete"}:
                raise ValueError("Unknown app action.")
            if app["current_version"]:
                name = self.container_name(app_id, app["current_version"])
                if action == "delete":
                    # A card can still be removed if its container was deleted
                    # outside the hub. A daemon error must still fail visibly.
                    existing = self.docker.run(["ps", "-aq", "--filter", "label=" + LABEL,
                        "--filter", f"name=^/{name}$"], timeout=10).split()
                    if existing:
                        self.docker.run(["rm", "-f", name], timeout=30)
                else:
                    self.docker.run([action, name], timeout=45)
            elif action != "delete":
                raise ValueError("Upload a working version before starting this app.")
            with self.db() as db:
                if action == "delete":
                    # Retain the named data volume deliberately; removal is explicit
                    # and documented, so deleting a card cannot delete saved data.
                    db.execute("DELETE FROM apps WHERE id=?", (app_id,))
                    db.execute("DELETE FROM jobs WHERE app_id=?", (app_id,))
                    db.execute("DELETE FROM releases WHERE app_id=?", (app_id,))
                else:
                    db.execute("UPDATE apps SET status=?, updated_at=? WHERE id=?", ("stopped" if action == "stop" else "running", now(), app_id))

    def logs(self, app_id):
        app = self.app(app_id)
        if not app["current_version"]:
            return "No running version yet. Open the deployment log."
        release = self.release(app["current_version"])
        return redact(self.docker.run(["logs", "--tail", "200", self.container_name(app_id, app["current_version"])], timeout=10), release["env"])

    def job(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Deployment does not exist.")
            return dict(row)
