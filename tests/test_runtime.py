"""Real HTTP smoke tests for the generated static server and example backend.

These execute Python services directly; they do not require or simulate Docker.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from apphub.engine import Hub
from apphub.packages import STATIC_SERVER, unpack
from tests.test_hub import FakeDocker


@contextmanager
def python_service(script, **environment):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable,str(script)], env={**os.environ,**environment,'PORT':str(port),'HOST':'127.0.0.1'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    try:
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise AssertionError('Example service exited before startup')
            try:
                with urllib.request.urlopen(base,timeout=.3):
                    break
            except OSError:
                time.sleep(.025)
        else:
            raise AssertionError('Example service did not start')
        yield base
    finally:
        process.terminate()
        process.wait(timeout=5)


class RuntimeTests(unittest.TestCase):
    def test_uploaded_html_is_served_as_home_page(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'calculator.html'
            content = b'<!doctype html><html><title>Calculator</title><button id="add">Add</button><script>window.count=0;</script></html>'
            source.write_bytes(content)
            site = unpack(source, root/'site')
            script = root/'serve.py'
            script.write_text(STATIC_SERVER)
            with python_service(script,SITE_ROOT=str(site)) as base:
                with urllib.request.urlopen(base+'/') as response:
                    self.assertEqual(response.status,200)
                    self.assertEqual(response.headers.get_content_type(),'text/html')
                    self.assertEqual(response.read(),content)

    def test_static_service_routes_and_asset_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            site = root/'site'
            site.mkdir()
            (site/'index.html').write_text('<h1>SPA entry</h1>')
            (site/'.private').write_text('must not be served')
            (site/'app.js').write_text('window.ok=true;')
            script = root/'serve.py'
            script.write_text(STATIC_SERVER)
            with python_service(script,SITE_ROOT=str(site)) as base:
                with urllib.request.urlopen(urllib.request.Request(base+'/projects/123',headers={'Accept':'text/html'})) as response:
                    self.assertIn(b'SPA entry',response.read())
                with urllib.request.urlopen(base+'/app.js') as response:
                    self.assertIn(b'window.ok',response.read())
                for path in ('/missing.js','/.private','/api/missing'):
                    try:
                        urllib.request.urlopen(base+path)
                    except urllib.error.HTTPError as exc:
                        self.assertEqual(exc.code,404)
                        exc.close()
                    else:
                        self.fail(f'{path} should return 404')

    def test_python_backend_persists_after_process_restart(self):
        script = Path(__file__).parents[1]/'examples/python-counter/server.py'
        with tempfile.TemporaryDirectory() as data:
            with python_service(script,DATA_DIR=data) as base:
                with urllib.request.urlopen(urllib.request.Request(base+'/api/count',data=b'',method='POST')) as response:
                    self.assertEqual(json.loads(response.read())['count'],1)
            with python_service(script,DATA_DIR=data) as base:
                with urllib.request.urlopen(base+'/api/count') as response:
                    self.assertEqual(json.loads(response.read())['count'],1)
                with urllib.request.urlopen(base+'/health') as response:
                    self.assertEqual(json.loads(response.read())['status'],'ok')

    def test_readiness_accepts_redirect_without_following_it(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location','http://nonexistent.invalid/')
                self.end_headers()
            def log_message(self,*args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as data:
                fake = FakeDocker()
                fake.containers['ready-app'] = {'state':'running','labels':{}}
                hub = Hub(data,fake,recover=False)
                try:
                    hub.wait_ready('ready-app',{'container_port':server.server_port,'health_path':'/'})
                finally:
                    hub.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
