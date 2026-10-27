"""UI test fixture. Docker is deliberately modeled; never use for hosting."""
import os
import tempfile
from apphub.engine import Hub
from apphub.server import Server
from tests.test_hub import FakeDocker

hub = Hub(os.environ.get("APPHUB_TEST_DATA") or tempfile.mkdtemp(prefix="apphub-browser-"), FakeDocker(), recover=False)
hub.wait_ready = lambda *args: None
server = Server(("127.0.0.1", int(os.environ.get("APPHUB_TEST_PORT", "18764"))), hub)
print(f"Browser test fixture: http://127.0.0.1:{server.server_port} (modeled Docker)", flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
    hub.close()
