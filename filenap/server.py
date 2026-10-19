from __future__ import annotations

import argparse
import concurrent.futures
import hmac
import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .demo import seed
from .engine import Engine, Problem, safe_category
from .planner import Planner


class App:
    def __init__(self, folder, planner, demo=False):
        self.engine = Engine(folder)
        self.planner = planner
        self.demo = demo
        self.token = secrets.token_hex(24)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.job_lock = threading.Lock()
        self.active = False

    def state(self):
        with self.engine.lock:
            return {"scan": self.engine.scan(), "plans": self.engine.plans(),
                    "batches": self.engine.batches(), "demo": self.demo,
                    "jobs": [dict(r) for r in self.engine.db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 10")]}

    def start_plan(self, data):
        with self.job_lock:
            if self.active:
                raise Problem("A local plan is already running.")
            ids = data.get("ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 16 or len(set(ids)) != len(ids):
                raise Problem("Choose one to sixteen different files.")
            scan = self.engine.scan()
            by_id = {f["id"]: f for f in scan["files"]}
            if any(i not in by_id for i in ids):
                raise Problem("The folder changed. Refresh it before planning.")
            categories = data.get("categories", [])
            if not isinstance(categories, list) or not 1 <= len(categories) <= 8:
                raise Problem("Choose one to eight destination folders.")
            categories = list(dict.fromkeys(safe_category(c) for c in categories))
            goal = data.get("goal", "")
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 1600:
                raise Problem("Write a short goal for this folder, up to 1,600 characters.")
            files = [by_id[i] for i in ids]
            job_id = uuid.uuid4().hex
            with self.engine.lock:
                self.engine.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (job_id, time.time(), "running", None, None))
                self.engine.db.commit()
            self.active = True

            def work():
                try:
                    proposal, record = self.planner.make(files, categories, goal)
                    plan = self.engine.store_plan(files, proposal, categories, record)
                    with self.engine.lock:
                        self.engine.db.execute("UPDATE jobs SET status='complete',result=? WHERE id=?", (plan["id"], job_id))
                        self.engine.db.commit()
                except Exception as exc:
                    message = str(exc) if isinstance(exc, Problem) else "Planning failed. No files were moved."
                    with self.engine.lock:
                        self.engine.db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", (message, job_id))
                        self.engine.db.commit()
                finally:
                    with self.job_lock:
                        self.active = False
            self.pool.submit(work)
            return {"id": job_id, "status": "running"}


def handler(app, port):
    static = Path(__file__).parent / "static"
    expected_host = "127.0.0.1:" + str(port)
    origin = "http://" + expected_host

    class Handler(BaseHTTPRequestHandler):
        server_version = "FileNap"

        def log_message(self, *_):
            pass

        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def permitted(self, api=False):
            if self.headers.get("Host") != expected_host:
                self.respond(403, {"error": "Use this app's 127.0.0.1 address."})
                return False
            if self.headers.get("Origin") not in {None, origin}:
                self.respond(403, {"error": "This request came from another site."})
                return False
            if api and not hmac.compare_digest(self.headers.get("X-FileNap-Token", ""), app.token):
                self.respond(403, {"error": "Reload FileNap to reconnect."})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.permitted(api=path.startswith("/api/")):
                return
            try:
                if path == "/favicon.ico":
                    return self.respond(204, b"", "image/x-icon")
                if path == "/api/state":
                    return self.respond(200, app.state())
                if path == "/api/model":
                    try:
                        return self.respond(200, {"ready": True, **app.planner.health()})
                    except Problem as exc:
                        return self.respond(200, {"ready": False, "name": app.planner.model, "error": str(exc)})
                if path.startswith("/api/jobs/"):
                    with app.engine.lock:
                        row = app.engine.db.execute("SELECT * FROM jobs WHERE id=?", (path.split("/")[-1],)).fetchone()
                    if row is None:
                        raise Problem("This planning job was not found.")
                    return self.respond(200, dict(row))
                if path in {"/", "/app.js", "/style.css"}:
                    name = "index.html" if path == "/" else path[1:]
                    body = (static / name).read_bytes()
                    if name == "index.html":
                        body = body.replace(b"FILENAP_SESSION_TOKEN", app.token.encode())
                    content_type = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                    return self.respond(200, body, content_type + "; charset=utf-8")
                self.respond(404, {"error": "Not found."})
            except (Problem, OSError) as exc:
                self.respond(409, {"error": str(exc)})

        def do_POST(self):
            if not self.permitted(api=True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise Problem("Expected a small JSON request.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise Problem("Expected a JSON object.")
                path = urlsplit(self.path).path
                if path == "/api/plan":
                    result = app.start_plan(data)
                elif path == "/api/apply":
                    result = app.engine.apply(data.get("plan_id"), data.get("ids"))
                elif path == "/api/undo":
                    result = app.engine.undo(data.get("batch_id"))
                elif path == "/api/resume":
                    result = app.engine.resume(data.get("batch_id"))
                else:
                    return self.respond(404, {"error": "Not found."})
                self.respond(200, result)
            except (Problem, OSError, ValueError, TypeError) as exc:
                self.respond(409, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Review local-model file plans and apply reversible moves.")
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--port", type=int, default=18731)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="gemma4:12b-it-q4_K_M")
    args = parser.parse_args()
    if args.demo == bool(args.folder):
        parser.error("Choose --demo or --folder PATH.")
    if args.demo:
        folder = Path.cwd() / ".local" / ("example-inbox-" + uuid.uuid4().hex[:8])
        folder.parent.mkdir(exist_ok=True)
        seed(folder)
    else:
        folder = args.folder
    app = App(folder, Planner(args.ollama_url, args.model), demo=args.demo)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(app, args.port))
    print("FileNap: http://127.0.0.1:" + str(args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.pool.shutdown(wait=True)
        app.engine.close()


if __name__ == "__main__":
    main()
