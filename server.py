#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Qoder CLI token usage - local web dashboard.

Serves a tiny web UI on http://127.0.0.1:8765 that shows estimated token
usage of qodercli (.qoder) and qoderclicn (.qoder-cn) from local transcripts.

Usage:
    python server.py [port]

Endpoints:
    GET /               -> dashboard page
    GET /api/usage      -> JSON stats (cached; add ?refresh=1 to force rescan)
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COLLECT_PS1 = os.path.join(BASE_DIR, "collect.ps1")
CCSWITCH_PY = os.path.join(BASE_DIR, "collect-ccswitch.py")
CACHE_PATH = os.path.join(BASE_DIR, "cache.json")
CACHE_TTL = 60  # seconds; incremental rescan is ~1s, so we can keep this short

_lock = threading.Lock()
_cache = {"data": None, "ts": 0.0, "scanning": False}


def _scan():
    """Run collect.ps1 and return parsed JSON."""
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", COLLECT_PS1],
        stderr=subprocess.STDOUT,
        timeout=300,
    )
    # PowerShell may emit a UTF-8 BOM
    text = out.decode("utf-8-sig", errors="replace").strip()
    return json.loads(text)


def _scan_ccswitch():
    """Real usage rollups from cc-switch SQLite DB (survives CC auto-cleanup)."""
    try:
        out = subprocess.check_output(
            [sys.executable, CCSWITCH_PY], stderr=subprocess.STDOUT, timeout=60,
        )
        return json.loads(out.decode("utf-8-sig", errors="replace").strip())
    except Exception as e:
        print("ccswitch scan failed:", e)
        return {"roots": []}


def get_usage(force=False):
    now = time.time()
    with _lock:
        if not force and _cache["data"] and now - _cache["ts"] < CACHE_TTL:
            return _cache["data"], False
        scanning = _cache["scanning"]
    if scanning:
        return _cache["data"], True
    with _lock:
        _cache["scanning"] = True
    try:
        data = _scan()           # transcript-based (qoder + claude)
        ccs = _scan_ccswitch()   # cc-switch rollups (historical real usage)
        roots = list(data.get("roots") or [])
        # dedup: drop transcript days already covered by cc-switch claude rollups
        ccs_claude_days = set()
        for r in ccs.get("roots") or []:
            if r.get("key") == "ccs-claude":
                ccs_claude_days = {d["day"] for d in r.get("days") or []}
        for r in roots:
            if r.get("key") == "cc":
                r["days"] = [d for d in (r.get("days") or []) if d["day"] not in ccs_claude_days]
                r["inTokens"] = sum(d["inTokens"] for d in r["days"])
                r["outTokens"] = sum(d["outTokens"] for d in r["days"])
                r["requests"] = sum(d["requests"] for d in r["days"])
        roots += list(ccs.get("roots") or [])
        data["roots"] = roots
        data["generatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with _lock:
            _cache["data"] = data
            _cache["ts"] = time.time()
        try:
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError:
            pass
    finally:
        with _lock:
            _cache["scanning"] = False
    return _cache["data"], False


def _load_disk_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
        with _lock:
            _cache["data"] = data
            _cache["ts"] = 0.0  # expired: rescan on first request, but serve stale meanwhile
        return True
    except (OSError, ValueError):
        return False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/usage":
            force = "refresh=1" in query
            data, scanning = get_usage(force=force)
            if data is None:
                self._send(202, json.dumps({"scanning": True, "message": "first scan running"}))
                return
            payload = dict(data)
            payload["stale"] = force and scanning or (time.time() - _cache["ts"] > CACHE_TTL)
            payload["scanning"] = scanning
            self._send(200, json.dumps(payload, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):
        pass  # silence access log


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    _load_disk_cache()
    # kick off a background scan on startup so first page load is fast
    threading.Thread(target=get_usage, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Qoder usage dashboard: http://127.0.0.1:%d" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
