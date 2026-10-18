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
SQLITE_PY = os.path.join(BASE_DIR, "collect-sqlite.py")
ATOMCODE_PY = os.path.join(BASE_DIR, "collect-atomcode.py")
DSH_PY = os.path.join(BASE_DIR, "collect-dsh.py")
CACHE_PATH = os.path.join(BASE_DIR, "cache.json")
# Sources hidden from the dashboard by request (2026-10-18): still collected,
# just dropped from the payload. Delete a key to bring its tab back.
HIDDEN_SOURCE_KEYS = {"ccs-opencode", "ccs-gemini"}
CACHE_TTL = 60  # seconds; incremental rescan is ~1s, so we can keep this short
# The transcripts only record the selector key ("qfmodel"); the product name lives in the
# CLI/IDE text bundle. Mapping it here keeps it display-only -- no rescan, no state bump.
MODEL_TEXTS = [
    os.path.join(os.path.expanduser("~"), ".qoder", ".auth", "dynamic-texts.json"),
    os.path.join(os.path.expanduser("~"), ".qoder-cn", ".auth", "dynamic-texts.json"),
]
# Non-Qoder sources spell some models differently: AtomCode catalogs record GLM-5.3-Flash
# as `glm5.3-flash` (no hyphen after glm), which would otherwise split off as a twin row of
# the cc-switch/zcode/Qoder bucket. Same display-only mechanism as the bundle map.
MODEL_ALIAS_FIXUPS = {"glm5.3-flash": "GLM-5.3-Flash"}

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


def _run_collector(script, timeout=60):
    """Run a Python collector; return (payload, error). A crash must not become
    an empty roots[] silently -- that is how the cc-switch tabs vanished."""
    name = os.path.basename(script)
    try:
        out = subprocess.check_output(
            [sys.executable, script], stderr=subprocess.STDOUT, timeout=timeout,
        )
        return json.loads(out.decode("utf-8-sig", errors="replace").strip()), None
    except Exception as e:
        return {"roots": []}, "%s: %s" % (name, e)


def _model_names():
    """{alias: product name} from the text bundles (plus MODEL_ALIAS_FIXUPS, applied last so
    manual entries win), newest file winning on conflict (they disagree: MiniMax-M3 vs M2.7
    for mmodel). 'en' beats 'zh-CN' -- model names are ASCII there, only the tiers differ."""
    names = {}
    def mt(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    for p in sorted(MODEL_TEXTS, key=mt):
        try:
            with open(p, encoding="utf-8-sig") as f:
                locs = (json.load(f).get("locales") or {})
        except (OSError, ValueError):
            continue
        per = {}
        for loc in ("zh-CN", "en"):
            for k, v in (locs.get(loc) or {}).items():
                if k.startswith("modelSelector.item.") and k.count(".") == 2 and isinstance(v, str):
                    per[k.rsplit(".", 1)[1]] = v
        names.update(per)
    names.update(MODEL_ALIAS_FIXUPS)
    return names


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
        data = _scan()                        # transcript-based (qoder + claude)
        ccs, ccs_err = _run_collector(CCSWITCH_PY)   # cc-switch rollups (real usage)
        sdb, sdb_err = _run_collector(SQLITE_PY)     # ZCode / Qoder desktop SQLite
        acs, acs_err = _run_collector(ATOMCODE_PY)   # AtomCode session metadata
        dsh, dsh_err = _run_collector(DSH_PY)        # DSH zstd transcripts (via node)
        errors = [e for e in (ccs_err, sdb_err, acs_err, dsh_err) if e]
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
        roots += list(sdb.get("roots") or [])
        roots += list(acs.get("roots") or [])
        roots += list(dsh.get("roots") or [])
        roots = [r for r in roots if r.get("key") not in HIDDEN_SOURCE_KEYS]
        data["roots"] = roots
        data["modelNames"] = _model_names()
        data["collectorErrors"] = errors
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
