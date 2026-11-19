"""Serve the Ubuntu training monitor at /training.

The trainer writes progress.json and training.log next to its output ZIP.
This server only reads a runs directory; it never starts or stops training.
"""
import argparse
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


WEB_DIR = Path(__file__).with_name("training_web")
RUN_ID = re.compile(r"^[^\x00-\x1f/\\]{1,120}$")
STATIC = {"app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}
LOG_LIMIT = 200_000


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def tail_text(path, limit=LOG_LIMIT):
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def run_directory(runs_dir, run_id):
    if not RUN_ID.fullmatch(run_id or "") or run_id in (".", ".."):
        return None
    base = runs_dir.resolve()
    directory = (base / run_id).resolve()
    if directory.parent != base or not directory.is_dir():
        return None
    return directory


def run_summary(directory):
    progress = read_json(directory / "progress.json") or {}
    trend = progress.get("validation_trend")
    latest = trend[-1] if isinstance(trend, list) and trend and isinstance(trend[-1], dict) else {}
    result = (directory / "result.zip").is_file()
    mtime = directory.stat().st_mtime
    return {
        "id": directory.name,
        "status": progress.get("status") or ("completed" if result else "waiting"),
        "task": (directory / "task.zip").is_file(),
        "result": result,
        "step": progress.get("step", 0),
        "total_steps": progress.get("total_steps", 0),
        "loss": progress.get("loss"),
        "epoch": progress.get("epoch"),
        "validation_mean": latest.get("mean"),
        "updated_at": progress.get("updated_at") or datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "mtime": mtime,
    }


def collect_runs(runs_dir):
    if not runs_dir.is_dir():
        return []
    runs = []
    for directory in runs_dir.iterdir():
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        if not any((directory / name).exists() for name in ("task.zip", "result.zip", "progress.json")):
            continue
        runs.append(run_summary(directory))
    runs.sort(key=lambda run: (run["status"] == "running", run["mtime"]), reverse=True)
    return runs


def run_detail(runs_dir, run_id):
    directory = run_directory(runs_dir, run_id)
    if directory is None:
        return None
    summary = run_summary(directory)
    summary["progress"] = read_json(directory / "progress.json")
    summary["log"] = tail_text(directory / "training.log")
    name = quote(directory.name, safe="")
    summary["links"] = {"result": f"/api/training/runs/{name}/result.zip" if summary["result"] else None}
    return summary


class Handler(BaseHTTPRequestHandler):
    server_version = "TrainingDashboard/1.0"

    @property
    def runs_dir(self):
        return self.server.runs_dir

    def log_message(self, format, *args):
        return

    def respond(self, status, content_type, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def body(self, status, content_type, data):
        self.respond(status, content_type, len(data))
        self.wfile.write(data)

    def json(self, value, status=200):
        self.body(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode())

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def serve_file(self, path, content_type, download=None):
        try:
            data = path.read_bytes()
        except OSError:
            self.json({"error": "文件不存在。"}, 404)
            return
        extra = ({"Content-Disposition": f"attachment; filename=\"result.zip\"; filename*=UTF-8''{quote(download)}"}
                 if download else None)
        self.respond(200, content_type, len(data), extra)
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/training"):
            self.redirect("/training/")
            return
        if path == "/training/":
            self.serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/training/"):
            name = path[len("/training/"):]
            if name in STATIC:
                self.serve_file(WEB_DIR / name, STATIC[name])
            else:
                self.json({"error": "页面不存在。"}, 404)
            return
        if path == "/api/training/runs":
            self.json({"runs": collect_runs(self.runs_dir), "generated_at": utc_now()})
            return
        prefix = "/api/training/runs/"
        if path.startswith(prefix):
            parts = [unquote(part) for part in path[len(prefix):].split("/")]
            if len(parts) == 1 and parts[0]:
                detail = run_detail(self.runs_dir, parts[0])
                if detail:
                    self.json(detail)
                else:
                    self.json({"error": "训练任务不存在。"}, 404)
                return
            if len(parts) == 2 and parts[1] == "result.zip":
                directory = run_directory(self.runs_dir, parts[0])
                if directory is None or not (directory / "result.zip").is_file():
                    self.json({"error": "结果包不存在。"}, 404)
                else:
                    self.serve_file(directory / "result.zip", "application/zip", f"{directory.name}-result.zip")
                return
        self.json({"error": "Not Found"}, 404)


def serve(runs_dir, host="127.0.0.1", port=8000):
    runs_dir = runs_dir.resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    server.runs_dir = runs_dir
    print(f"训练监控: http://{host}:{port}/training  (runs: {runs_dir})", flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=Path("training-runs"), help="训练任务目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.runs_dir, args.host, args.port)


if __name__ == "__main__":
    main()
