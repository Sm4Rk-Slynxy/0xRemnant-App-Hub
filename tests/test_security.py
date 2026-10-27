"""Regression tests for publication-review security fixes."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from apphub.engine import StreamingRedactor, redact
from apphub.packages import prepare, unpack
from tests import test_hub as hub_tests
from tests.test_runtime import python_service
from apphub.packages import STATIC_SERVER


class RedactionTests(unittest.TestCase):
    def test_every_chunk_boundary_and_short_values(self):
        values = {"TOKEN": "demo-sensitive-value", "PIN": "xy", "EMPTY": ""}
        text = "prefix demo-sensitive-value suffix xy end"
        expected = "prefix [redacted] suffix [redacted] end"
        for split in range(len(text) + 1):
            output = []
            stream = StreamingRedactor(values, output.append)
            stream.write(text[:split])
            stream.write(text[split:])
            stream.close()
            self.assertEqual("".join(output), expected, split)
        output = []
        stream = StreamingRedactor(values, output.append)
        for character in text:
            stream.write(character)
        stream.close()
        self.assertEqual("".join(output), expected)
        self.assertEqual(redact("redactor", {"KEY":"red"}), "[redacted]actor")


class SecurityHTTPTests(unittest.TestCase):
    setUp = hub_tests.HTTPTests.setUp
    tearDown = hub_tests.HTTPTests.tearDown
    request = hub_tests.HTTPTests.request
    login = hub_tests.HTTPTests.login
    def test_first_lan_visitor_cannot_claim_owner_without_terminal_token(self):
        token_file = Path(self.temp.name) / ".setup-token"
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        for token in (None, "wrong", "☃"):
            body = {"password":"correct-horse-123", "setup_token":token}
            self.assertEqual(self.request("/api/setup", body)[0], 403)
            self.assertFalse(self.hub.setting("password"))
        self.assertNotIn(token_file.read_text(), json.dumps(self.request("/api/auth/status")))
        self.login()
        self.assertFalse(token_file.exists())
        self.assertEqual(self.server.setup_token, "")

    def test_damaged_empty_setup_token_fails_closed(self):
        from apphub.server import Server
        token_file = Path(self.temp.name)/".setup-token"
        original = token_file.read_text()
        token_file.write_text("")
        try:
            with self.assertRaisesRegex(RuntimeError, "damaged"):
                Server(("127.0.0.1", 0), self.hub)
        finally:
            token_file.write_text(original)

    def test_cli_reads_token_while_server_owns_registry(self):
        import subprocess, sys
        result = subprocess.run([sys.executable,"-m","apphub.server","--data",self.temp.name,"--show-setup-token"],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), self.server.setup_token)

    def raw_request(self, lines):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as sock:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            return int(sock.recv(4096).split(b" ")[1])

    def test_ambiguous_headers_and_malformed_hosts_are_rejected(self):
        for host in ("127.0.0.1:bad", "127.0.0.1:99999", "127.0.0.1?x", "127.0.0.1#x", "127.0.0.1@evil.test"):
            self.assertEqual(self.request("/healthz", headers={"Host":host})[0], 403)
        self.assertEqual(self.raw_request(["GET /healthz HTTP/1.1", "Host: localhost", "Host: 127.0.0.1", "Connection: close"]), 403)
        self.assertEqual(self.raw_request(["POST /api/login HTTP/1.1", "Host: localhost", "Content-Length: 0", "Content-Length: 0", "Connection: close"]), 400)

    def test_setup_attempts_are_rate_limited(self):
        for _ in range(10):
            self.assertEqual(self.request("/api/setup", {"password":"correct-horse-123", "setup_token":"wrong"})[0], 403)
        self.assertEqual(self.request("/api/setup", {"password":"correct-horse-123", "setup_token":self.server.setup_token})[0], 429)


class SecurityEngineTests(unittest.TestCase):
    setUp = hub_tests.EngineTests.setUp
    tearDown = hub_tests.EngineTests.tearDown
    deploy = hub_tests.EngineTests.deploy
    def test_create_uses_private_temporary_env_file(self):
        app, job = self.deploy(env={"API_KEY":"private-demo-key"})
        self.assertEqual(job["status"], "succeeded")
        creates = [args for args in self.fake.calls if args[0] == "create"]
        self.assertNotIn("private-demo-key", json.dumps(creates))
        env_path = Path(creates[0][creates[0].index("--env-file")+1])
        self.assertFalse(env_path.exists())
        name = self.hub.container_name(app["id"], app["current_version"])
        self.assertEqual(self.fake.containers[name]["env"]["API_KEY"], "private-demo-key")

    def test_build_logs_redact_secrets_split_between_reads(self):
        original = self.fake.run
        def split_build(args, **kwargs):
            if args[0] == "build":
                callback = kwargs.get("log")
                callback("value=private-")
                callback("demo-key\n")
            return original(args, **kwargs)
        with patch.object(self.fake, "run", side_effect=split_build):
            _, job = self.deploy(env={"API_KEY":"private-demo-key"})
        self.assertEqual(job["status"], "succeeded")
        self.assertIn("value=[redacted]", job["log"])
        self.assertNotIn("private-demo-key", job["log"])


class StaticPrivacyTests(unittest.TestCase):
    def test_static_stage_excludes_private_files_and_helper_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/"index.html").write_text("<h1>Public app</h1>")
            (root/"apphub.json").write_text("{}")
            (root/".aws").mkdir()
            (root/".aws"/"credentials").write_text("demo only")
            (root/"private.key").write_text("demo only")
            (root/"assets").mkdir()
            (root/"assets"/"style.css").write_text("body{}")
            config = prepare(root)
            public = root/"__apphub_runtime"/"public"
            self.assertEqual(sorted(str(p.relative_to(public)) for p in public.rglob("*") if p.is_file()), ["assets/style.css", "index.html"])
            self.assertIn("__apphub_runtime/public/", (root/config["dockerfile"]).read_text())

    def test_static_server_blocks_sensitive_files_and_directory_listing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/"index.html").write_text("<h1>Public app</h1>")
            (root/"assets").mkdir()
            for name in ("apphub.json", "Dockerfile", "credentials.json", "private.pem", "notes.sqlite3"):
                (root/name).write_text("must stay private")
            script = root/"serve.py"
            script.write_text(STATIC_SERVER)
            import urllib.request, urllib.error
            with python_service(script, SITE_ROOT=str(root)) as base:
                for path in ("/assets/", "/apphub.json", "/Dockerfile", "/credentials.json", "/private.pem", "/notes.sqlite3"):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(base+path)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()


class PublicationGuardTests(unittest.TestCase):
    def test_token_and_private_files_report_location_without_secret(self):
        from scripts.check_publication import inspect_bytes
        token = "ghp_"+"a"*36
        findings = []
        inspect_bytes("apphub/example.py", ("key="+token).encode(), findings)
        self.assertEqual(findings,["apphub/example.py:1: GitHub token"])
        self.assertNotIn(token, "".join(findings))
        inspect_bytes("apphub/config/.env.local", b"example", findings)
        self.assertTrue(any("private/runtime filename" in item for item in findings))

    def test_bundled_archive_is_scanned_and_runtime_tree_not_released(self):
        from scripts.check_publication import inspect_bytes, release_files
        findings = []
        archive = hub_tests.zip_bytes({"index.html":"ok", ".env":"private demo"})
        inspect_bytes("examples/hello-app.zip", archive, findings)
        self.assertTrue(any("!.env" in item for item in findings))
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/"README.md").write_text("public")
            (root/".env").write_text("not for release")
            (root/"data").mkdir()
            (root/"data"/"records.sqlite3").write_bytes(b"private demo")
            self.assertEqual([p.relative_to(root).as_posix() for p in release_files(root)], ["README.md"])

    def test_zip_upload_excludes_common_credential_and_database_files(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder)/"app.zip"
            archive.write_bytes(hub_tests.zip_bytes({"index.html":"public", ".aws/credentials":"private demo", "id_rsa":"private demo", "assets/private.pem":"private demo", "records.sqlite3":"private demo", ".env.example":"private demo"}))
            root=unpack(archive, Path(folder)/"extracted")
            self.assertEqual([p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()], ["index.html"])
