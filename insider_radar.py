#!/usr/bin/env python3
"""Insider Buy Radar - desktop mode.

Run:  python3 insider_radar.py      (your browser opens at http://127.0.0.1:8765)
Serves the same app as the iPhone version, plus a "Scan now" button that runs on this computer.
Only needs Python 3.9+.
"""
from __future__ import annotations

import json
import mimetypes
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from radar.congress import run_congress_scan  # noqa: E402
from radar.engine import run_scan  # noqa: E402
from radar.sec_client import CACHE_DIR, SecClient, SecError  # noqa: E402

DOCS = HERE / "docs"
SETTINGS_FILE = CACHE_DIR / "settings.json"
RESULTS_FILE = DOCS / "data" / "latest.json"
CONGRESS_FILE = DOCS / "data" / "congress.json"

STATE = {"running": False, "stage": "", "done": 0, "total": 0, "error": None}
LOCK = threading.Lock()
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str), encoding="utf-8")
    tmp.replace(path)


def make_client(settings: dict) -> SecClient:
    return SecClient(settings.get("email", ""))


def scan_worker(params: dict) -> None:
    def progress(stage: str, done: int, total: int) -> None:
        with LOCK:
            STATE.update(stage=stage, done=done, total=total)

    try:
        client = make_client(load_json(SETTINGS_FILE, {}))
        result = run_scan(client, days=int(params.get("days", 7)), include_live=True,
                          deep_min=float(params.get("deep_min", 50_000)), use_prices=bool(params.get("use_prices", True)),
                          progress=progress)
        save_json(RESULTS_FILE, result)
        try:  # Congress trades (a separate, unranked feed); failures here never block the insider scan
            cres = run_congress_scan(client, days=90, insider_rows=result["rows"], previous=load_json(CONGRESS_FILE, None),
                                     progress=progress)
            save_json(CONGRESS_FILE, cres)
        except Exception:
            traceback.print_exc()
        with LOCK:
            STATE.update(running=False, stage="Done", error=None)
    except SecError as e:
        with LOCK:
            STATE.update(running=False, error=str(e))
    except Exception as e:  # pragma: no cover
        traceback.print_exc()
        with LOCK:
            STATE.update(running=False, error=f"Unexpected error: {e}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/api/state":
            settings = load_json(SETTINGS_FILE, {})
            with LOCK:
                job = dict(STATE)
            return self._json({"local": True, "email": settings.get("email", ""), "job": job})
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (DOCS / rel).resolve()
        if DOCS.resolve() not in target.parents and target != DOCS.resolve() or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            return self._send(403, b"forbidden", "text/plain")
        if self.path == "/api/settings":
            email = str(self._body().get("email", "")).strip()
            if "@" not in email or len(email) > 200:
                return self._json({"error": "Please enter a valid e-mail address."}, 400)
            settings = load_json(SETTINGS_FILE, {})
            settings["email"] = email
            save_json(SETTINGS_FILE, settings)
            return self._json({"ok": True})
        if self.path == "/api/scan":
            params = self._body()
            with LOCK:
                if STATE["running"]:
                    return self._json({"error": "A scan is already running."}, 409)
                STATE.update(running=True, stage="Starting", done=0, total=0, error=None)
            threading.Thread(target=scan_worker, args=(params,), daemon=True).start()
            return self._json({"ok": True})
        self._send(404, b"not found", "text/plain")


def main() -> None:
    for port in range(8765, 8785):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit("No free port between 8765 and 8784.")
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Insider Buy Radar is running at {url}\n  Keep this window open. Press Ctrl+C to stop.\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
