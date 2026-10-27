"""Run with python -m unittest discover -v. Docker is modeled, never required."""
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

from apphub.engine import Hub, StreamingRedactor, parse_env, redact
from apphub.packages import unpack, prepare
from apphub.server import Server


def zip_bytes(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, contents in files.items():
            z.writestr(name, contents)
    return out.getvalue()


class FakeDocker:
    """Model image builds, containers, port binding, and failure injection."""
    def __init__(self):
        self.containers = {}
        self.images = set()
        self.fail_build = False
        self.fail_start = False
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append(args)
        action = args[0]
        if action == "build":
            if self.fail_build:
                raise RuntimeError("Sample build failure")
            self.images.add(args[args.index("-t") + 1])
            if kwargs.get("log"):
                kwargs["log"]("Image built\n")
            return "image"
        if action == "network":
            return "[]"
        if action == "create":
            name = args[args.index("--name") + 1]
            if name in self.containers:
                raise RuntimeError("Container already exists")
            labels = {}
            env = {}
            for i, arg in enumerate(args):
                if arg in {"--label", "--env"}:
                    key, value = args[i + 1].split("=", 1)
                    (labels if arg == "--label" else env)[key] = value
            if "--env-file" in args:
                env = parse_env(Path(args[args.index("--env-file") + 1]).read_text())
            self.containers[name] = {"state": "created", "port": args[args.index("-p") + 1], "labels": labels, "env": env, "image": args[-1]}
            return name
        if action == "start":
            name = args[-1]
            if self.fail_start:
                self.fail_start = False
                raise RuntimeError("Sample startup failure")
            if name not in self.containers:
                raise RuntimeError("Container missing")
            for key, container in self.containers.items():
                if key != name and container["state"] == "running" and container["port"] == self.containers[name]["port"]:
                    raise RuntimeError("Port in use")
            self.containers[name]["state"] = "running"
            return name
        if action in {"stop", "restart"}:
            self.containers[args[-1]]["state"] = "exited" if action == "stop" else "running"
            return args[-1]
        if action == "rm":
            self.containers.pop(args[-1], None)
            return "removed"
        if action == "logs":
            return "Sample app log\n"
        if action == "inspect":
            name = args[-1]
            if name not in self.containers:
                raise RuntimeError("Container missing")
            c = self.containers[name]
            if "--format" in args:
                return "true" if c["state"] == "running" else "false"
            return json.dumps([{"State": {"Status": c["state"]}, "Config": {"Labels": c["labels"]}, "NetworkSettings": {"Networks": {"apphub-runtime": {"IPAddress": "127.0.0.1"}}}}])
        if action == "ps":
            if "-aq" in args:
                name_filter = next((a for a in args if a.startswith("name=^/")), None)
                if name_filter:
                    target = name_filter[len("name=^/"):-1]
                    return target if target in self.containers else ""
                return "\n".join(self.containers)
            return "\n".join(f"{key}\t{c['state']}" for key,c in self.containers.items())
        raise AssertionError("Unhandled Docker command: " + str(args))


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def extract(self, files):
        archive = self.root / "app.zip"
        archive.write_bytes(zip_bytes(files))
        return unpack(archive, self.root / "extracted")

    def test_wrapped_static_project_and_secret_exclusion(self):
        root = self.extract({"my-app/index.html": "hello", "my-app/.env": "SECRET=hidden", "my-app/node_modules/huge.js": "skip", "__MACOSX/._file": "skip"})
        self.assertEqual(root.name, "my-app")
        self.assertFalse((root / ".env").exists())
        self.assertFalse((root / "node_modules").exists())
        config = prepare(root)
        self.assertEqual(config["mode"], "static")
        self.assertEqual(config["container_port"], 8080)
        self.assertIn('USER 65534:65534', (root / config["dockerfile"]).read_text())

    def test_standalone_html_and_htm_become_exact_entry_page(self):
        content = b'\xef\xbb\xbf<!doctype html><title>My tool</title><script>window.count=1;</script>'
        for extension in ('.html', '.htm', '.HTML'):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as folder:
                source = Path(folder)/('my-tool'+extension)
                source.write_bytes(content)
                root = unpack(source, Path(folder)/'app')
                self.assertEqual((root/'index.html').read_bytes(), content)
                self.assertEqual(prepare(root)['mode'], 'static')

    def test_empty_html_reports_clear_error(self):
        for content in (b'', b' \n\t '):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                source = Path(folder)/'empty.html'
                source.write_bytes(content)
                with self.assertRaisesRegex(ValueError, 'HTML file is empty'):
                    unpack(source, Path(folder)/'app')

    def test_single_html_in_zip_keeps_original_url_and_assets(self):
        root = self.extract({'my-tool.htm':'<script src="assets/app.js"></script>', 'assets/app.js':'window.tool=true;'})
        self.assertEqual((root/'index.html').read_bytes(), (root/'my-tool.htm').read_bytes())
        self.assertTrue((root/'assets/app.js').is_file())
        self.assertEqual(prepare(root)['mode'], 'static')

    def test_multiple_html_pages_need_explicit_entry_page(self):
        with self.assertRaisesRegex(ValueError, 'starting page index.html'):
            self.extract({'one.html':'one', 'two.html':'two'})

    def test_html_alias_does_not_turn_node_server_into_static_app(self):
        root = self.extract({'package.json':json.dumps({'scripts':{'start':'node server.js'}}),'server.js':'server code','welcome.html':'a template'})
        self.assertFalse((root/'index.html').exists())
        self.assertEqual(prepare(root)['mode'], 'node')

    def test_traversal_rejected(self):
        for name in ("../evil", "/etc/evil", "x/../../evil", "C:\\evil", "foo\\..\\evil"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                archive = Path(folder) / "app.zip"
                archive.write_bytes(zip_bytes({name: "evil"}))
                with self.assertRaises(ValueError):
                    unpack(archive, Path(folder) / "out")

    def test_symlink_rejected(self):
        archive = self.root / "app.zip"
        with zipfile.ZipFile(archive, "w") as z:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            unpack(archive, self.root / "out")

    def test_vite_index_is_built_instead_of_served_as_source(self):
        package = {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "1"}}
        root = self.extract({"package.json": json.dumps(package), "index.html": '<script src="/src/main.jsx"></script>'})
        config = prepare(root)
        dockerfile = (root / config["dockerfile"]).read_text()
        self.assertEqual(config["mode"], "static")
        self.assertIn("RUN npm run build", dockerfile)
        self.assertIn("COPY --from=build /app/dist/ /site/", dockerfile)

    def test_next_is_a_server(self):
        root = self.extract({"package.json": json.dumps({"dependencies": {"next": "1"}, "scripts": {"build": "next build", "start": "next start"}})})
        config = prepare(root)
        self.assertEqual(config["mode"], "node")
        self.assertEqual(config["container_port"], 3000)

    def test_custom_dockerfile_and_manifest_preserved(self):
        root = self.extract({"Dockerfile": "FROM python:3.12-alpine", "apphub.json": json.dumps({"port":5000,"health_path":"/health"})})
        config = prepare(root)
        self.assertEqual(config["mode"], "docker")
        self.assertEqual(config["container_port"], 5000)
        self.assertEqual(config["health_path"], "/health")
        self.assertEqual((root / "Dockerfile").read_text(), "FROM python:3.12-alpine")

    def test_invalid_manifest_is_rejected(self):
        for config in ({"static_dir":"../../etc"},{"port":True},{"health_path":"//elsewhere"},{"port":70000}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)
                (path / "index.html").write_text("hello")
                (path / "apphub.json").write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    prepare(path)

    def test_environment_parser_and_redaction(self):
        env = parse_env("# comment\nAPI_KEY=secret-value\nURL=https://example.test?a=b\nEMPTY=\n")
        self.assertEqual(env["URL"], "https://example.test?a=b")
        self.assertEqual(env["EMPTY"], "")
        self.assertEqual(redact("key=secret-value", env), "key=[redacted]")
        with self.assertRaises(ValueError):
            parse_env("BAD KEY=value")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeDocker()
        self.hub = Hub(self.temp.name, self.fake, recover=False)
        self.hub.wait_ready = lambda *args: None

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def deploy(self, app_id=None, env=None):
        path = Path(self.temp.name) / (str(time.time_ns()) + ".zip")
        path.write_bytes(zip_bytes({"index.html": "hello"}))
        result = self.hub.enqueue(path, name="My App", app_id=app_id, env=env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.hub.job(result["job_id"])
            if job["status"] in {"succeeded", "failed"}:
                return self.hub.app(result["app_id"]), job
            time.sleep(.01)
        self.fail("Deployment worker did not finish")

    def test_deploy_update_and_restore_keep_port_data_and_environment(self):
        app, job = self.deploy(env={"API_KEY":"keep-secret"})
        self.assertEqual(job["status"], "succeeded")
        original_version = app["current_version"]
        updated, job = self.deploy(app["id"], {"NEW":"new-value"})
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(updated["port"], app["port"])
        self.assertNotEqual(updated["current_version"], original_version)
        release = self.hub.release(updated["current_version"])
        self.assertEqual(release["env"], {"API_KEY":"keep-secret","NEW":"new-value"})
        mounts = [args[args.index("--mount")+1] for args in self.fake.calls if args[0] == "create"]
        self.assertEqual(mounts[0], mounts[1])
        result = self.hub.rollback(app["id"], original_version)
        deadline = time.monotonic()+5
        while self.hub.job(result["job_id"])["status"] not in {"succeeded","failed"} and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(self.hub.app(app["id"])["current_version"], original_version)
        apps, ok = self.hub.list_apps()
        self.assertTrue(ok)
        self.assertEqual(apps[0]["status"], "running")
        self.assertEqual(apps[0]["env_keys"], ["API_KEY"])
        self.assertNotIn("keep-secret", json.dumps(apps))

    def test_build_failure_does_not_stop_working_app(self):
        app, _ = self.deploy()
        name = self.hub.container_name(app["id"], app["current_version"])
        self.fake.fail_build = True
        updated, job = self.deploy(app["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(updated["current_version"], app["current_version"])
        self.assertEqual(self.fake.containers[name]["state"], "running")
        self.assertFalse(any(c[0] == "stop" for c in self.fake.calls))

    def test_startup_failure_restarts_previous_version(self):
        app, _ = self.deploy()
        name = self.hub.container_name(app["id"], app["current_version"])
        self.fake.fail_start = True
        updated, job = self.deploy(app["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(updated["current_version"], app["current_version"])
        self.assertEqual(self.fake.containers[name]["state"], "running")
        self.assertEqual(len(self.fake.containers), 1)

    def test_delete_preserves_volume(self):
        app, _ = self.deploy()
        self.hub.action(app["id"], "delete")
        self.assertEqual(self.hub.list_apps()[0], [])
        self.assertFalse(any(c[0] == "volume" for c in self.fake.calls))

    def test_only_one_hub_can_use_registry(self):
        with self.assertRaisesRegex(RuntimeError, "Another App Hub"):
            Hub(self.temp.name, self.fake, recover=False)

    def test_deleted_container_does_not_prevent_card_removal(self):
        app, _ = self.deploy()
        self.fake.containers.clear()
        self.hub.action(app["id"], "delete")
        self.assertEqual(self.hub.list_apps()[0], [])

    def test_recovery_restores_committed_version(self):
        app, _ = self.deploy()
        name = self.hub.container_name(app["id"], app["current_version"])
        self.fake.containers[name]["state"] = "exited"
        self.fake.containers["orphan"] = {**self.fake.containers[name], "labels": {"com.apphub.app":app["id"],"com.apphub.version":"orphan"}}
        with self.hub.db() as db:
            db.execute("UPDATE apps SET status='starting' WHERE id=?", (app["id"],))
        self.hub.recover()
        self.assertEqual(self.fake.containers[name]["state"], "running")
        self.assertNotIn("orphan", self.fake.containers)

    def test_rollback_cannot_take_another_apps_version(self):
        first, _ = self.deploy()
        second, _ = self.deploy()
        with self.assertRaisesRegex(ValueError, "belonging"):
            self.hub.rollback(first["id"], second["current_version"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(self.temp.name, FakeDocker(), recover=False)
        self.hub.wait_ready = lambda *args: None
        self.server = Server(("127.0.0.1", 0), self.hub)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.hub.close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        data = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        h = {"Authorization": "Bearer " + self.token, **(headers or {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base+path, data=data, headers=h), timeout=5) as response:
                raw = response.read()
                value = json.loads(raw) if "application/json" in response.headers["Content-Type"] else raw
                return response.status, value
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def login(self):
        code, data = self.request('/api/setup', {"password":"correct-horse-123", "setup_token":self.server.setup_token})
        self.assertEqual(code, 200)
        self.token = data["token"]

    def test_auth_origin_upload_logout_and_private_configuration(self):
        self.assertEqual(self.request('/api/apps')[0], 401)
        self.assertEqual(self.request('/api/setup', {"password":"short", "setup_token":self.server.setup_token})[0], 400)
        self.assertEqual(self.request('/api/setup', {"password":"correct-horse-123"}, {"Origin":"https://evil.test"})[0], 403)
        self.login()
        self.assertEqual(self.request('/api/setup', {"password":"another-password"})[0], 409)
        self.assertEqual(self.request('/api/apps', headers={"Host":"evil.test"})[0], 403)
        code, settings = self.request('/api/upload-settings', {"env":"API_KEY=secret-value"})
        self.assertEqual(code, 200)
        code, result = self.request('/api/deploy?name=Example', zip_bytes({"index.html":"hello"}), {"X-AppHub-Settings":settings["id"],"Content-Type":"application/zip"})
        self.assertEqual(code, 202)
        deadline = time.monotonic()+5
        while self.hub.job(result["job_id"])["status"] not in {"failed","succeeded"} and time.monotonic()<deadline:
            time.sleep(.01)
        code, data = self.request('/api/apps')
        self.assertEqual(data["apps"][0]["status"], "running")
        self.assertNotIn("secret-value", json.dumps(data))
        self.assertEqual(self.request('/api/logout', {})[0], 200)
        self.assertEqual(self.request('/api/apps')[0], 401)

    def test_example_is_a_real_uploadable_zip(self):
        self.login()
        code, data = self.request('/api/example')
        self.assertEqual(code, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn('index.html', archive.namelist())
            self.assertIn(b'Little Notes', archive.read('index.html'))

    def test_html_upload_and_htm_update_use_same_app_address(self):
        self.login()
        builds = []
        original_run = self.hub.docker.run
        def record_build(args, **kwargs):
            if args[0] == 'build':
                builds.append((Path(kwargs['cwd'])/'index.html').read_bytes())
            return original_run(args, **kwargs)
        with patch.object(self.hub.docker, 'run', side_effect=record_build):
            app_id = None
            initial_port = None
            for filename, mime, content in (
                ('my-calculator.HTML','application/octet-stream',b'<!doctype html><h1>Version one</h1>'),
                ('updated-calculator.htm','text/html',b'<!doctype html><h1>Version two</h1>'),
            ):
                _, settings = self.request('/api/upload-settings', {'env':''})
                query = '/api/deploy?name=Calculator&filename='+filename
                if app_id:
                    query += '&id='+app_id
                code, result = self.request(query, content, {'Content-Type':mime,'X-AppHub-Settings':settings['id']})
                self.assertEqual(code,202,result)
                deadline = time.monotonic()+5
                while self.hub.job(result['job_id'])['status'] not in {'succeeded','failed'} and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertEqual(self.hub.job(result['job_id'])['status'],'succeeded')
                app_id = result['app_id']
                app = self.hub.app(app_id)
                self.assertEqual(self.hub.release(app['current_version'])['mode'],'static')
                if initial_port is None:
                    initial_port = app['port']
                self.assertEqual(app['port'], initial_port)
                self.assertEqual(builds[-1],content)

    def test_html_content_type_without_filename_is_supported(self):
        self.login()
        _,settings = self.request('/api/upload-settings', {'env':''})
        code,result = self.request('/api/deploy?name=No-filename', b'<h1>HTML entry page</h1>', {'Content-Type':'text/html; charset=utf-8','X-AppHub-Settings':settings['id']})
        self.assertEqual(code,202,result)
        deadline = time.monotonic()+5
        while self.hub.job(result['job_id'])['status'] not in {'succeeded','failed'} and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertEqual(self.hub.job(result['job_id'])['status'],'succeeded')

    def test_unsupported_upload_or_server_type_is_rejected_before_deployment(self):
        self.login()
        for query in ('filename=tool.exe','filename=tool.html&type=node','filename=tool.htm&type=docker'):
            code,result = self.request('/api/deploy?name=Invalid&'+query, b'<h1>Content</h1>', {'Content-Type':'text/html'})
            self.assertEqual(code,400,result)
        self.assertEqual(self.hub.list_apps()[0],[])

    def test_static_assets_and_headers(self):
        for path in ('/', '/app.js', '/style.css', '/favicon.svg'):
            self.assertEqual(self.request(path)[0], 200)
        with urllib.request.urlopen(self.base+'/') as response:
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])

    def test_session_expiration(self):
        self.login()
        with self.hub.db() as db:
            db.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.request('/api/apps')[0], 401)


if __name__ == '__main__':
    unittest.main()
