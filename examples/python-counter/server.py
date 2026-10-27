"""A small real backend; counter data lives in the app's persistent /data volume."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3

data = Path(os.environ.get("DATA_DIR", "/data"))
data.mkdir(parents=True, exist_ok=True)
database = data / "counter.sqlite3"
with sqlite3.connect(database) as db:
    db.execute("CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
    db.execute("INSERT OR IGNORE INTO counter VALUES(1, 0)")

PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Persistent Counter</title><style>body{background:#14251e;color:#e5f9eb;font:16px system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;text-align:center;padding:20px}main{max-width:500px}h1{font-size:35px;letter-spacing:-1px}strong{display:block;font-size:100px;margin:25px}button{background:#bbf7d0;border:0;border-radius:10px;padding:15px 35px;color:#193225;font-weight:700;cursor:pointer}p{color:#a3bfa9;line-height:1.7}small{color:#86a58b;font-size:11px}</style><main><small>APP HUB · REAL BACKEND EXAMPLE</small><h1>Some things are worth keeping.</h1><p>This counter is saved in a SQLite database on the server. Upload an update or restart the app: your count stays.</p><strong id="count">…</strong><button id="increment">Add one +</button><p id="error"></p><small>Persistent file: /data/counter.sqlite3</small></main><script>async function load(method='GET'){const r=await fetch('/api/count',{method});if(!r.ok)throw new Error('Could not update counter');document.getElementById('count').textContent=(await r.json()).count;}document.getElementById('increment').onclick=()=>load('POST').catch(e=>document.getElementById('error').textContent=e.message);load().catch(e=>document.getElementById('error').textContent=e.message);</script></html>'''


class Handler(BaseHTTPRequestHandler):
    def respond(self, value, mime="application/json"):
        content = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/health":
            self.respond({"status":"ok"})
        elif self.path == "/api/count":
            with sqlite3.connect(database) as db:
                value = db.execute("SELECT value FROM counter WHERE id=1").fetchone()[0]
            self.respond({"count":value})
        elif self.path == "/":
            self.respond(PAGE, "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/count":
            self.send_error(404)
            return
        with sqlite3.connect(database) as db:
            db.execute("UPDATE counter SET value=value+1 WHERE id=1")
            value = db.execute("SELECT value FROM counter WHERE id=1").fetchone()[0]
        self.respond({"count":value})


ThreadingHTTPServer((os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
