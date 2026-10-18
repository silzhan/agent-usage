#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""collect-dsh.py - REAL token usage from DSH (DeepSeek Harness) sessions.

DSH stores an append-only event log, zstd-compressed as many concatenated
frames, at ~/.dsh/sessions/<project>/<session>/session.v4.jsonl.zstd. Python's
stdlib cannot decompress zstd, so a small inline Node script (node -e) walks
the frame magics; Node's one-shot zstdDecompressSync releases exactly one frame
and silently ignores any tail, which is the behaviour this walk relies on. A
false magic inside a frame's compressed payload is harmless: it either throws
or yields garbage that the JSON-line filter below drops.

Usage is recorded per step on assistant/message events:
data.usage = {inputTokens, outputTokens, cacheReadTokens}. inputTokens excludes
cache hits (verified against every step of a 7-turn session), so the dashboard
input side is inputTokens + cacheReadTokens - the cc-switch billing view.
Event time is epoch-ms UTC. The active model comes from the latest
request/context (or request/header) event before the step, so sessions that
switch models mid-way are attributed per step. Failed attempts
(assistant/attempt) report all-zero usage chunks and are skipped.

The legacy session.jsonl.zstd holds the same usage under the same chunk
schema; it is read only when no .v4 file exists for the session, so nothing is
counted twice.

Emits the same roots[] schema as collect-ccswitch.py.
"""
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SESSIONS = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")

_ZSTD_JS = r"""
const fs = require("fs"), z = require("zlib");
const magic = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const out = {};
for (const f of process.argv.slice(1)) {
  const data = fs.readFileSync(f);
  let offs = [], i = data.indexOf(magic);
  while (i >= 0) { offs.push(i); i = data.indexOf(magic, i + 1); }
  let text = "";
  for (const o of offs) {
    try { const b = z.zstdDecompressSync(data.subarray(o)); if (b.length) text += b.toString("utf8"); }
    catch (e) {}
  }
  out[f] = text;
}
process.stdout.write(JSON.stringify(out));
"""

_MODEL_DATE_RE = re.compile(r"(-[a-z]{1,4})?-[0-9]{4,8}$")
_MODEL_TAIL_RE = re.compile(r"[-_.]+$")


def norm_model(s):
    """Byte-identical to NormModel in collect.ps1 and norm_model in the other collectors."""
    t = ("" if s is None else str(s)).strip().lower()
    if not t or t == "<synthetic>":
        return "unknown"
    i = t.rfind("/")
    if i >= 0:
        t = t[i + 1:]
    t = _MODEL_DATE_RE.sub("", t)
    t = _MODEL_TAIL_RE.sub("", t)
    return t if t and t != "<synthetic>" else "unknown"


def session_files():
    """One decompressible file per session dir: prefer .v4, fall back to the
    legacy session.jsonl.zstd only when no .v4 exists (legacy duplicates it)."""
    by_dir = {}
    for p in glob.glob(os.path.join(SESSIONS, "**", "session*.jsonl.zstd"), recursive=True):
        by_dir.setdefault(os.path.dirname(p), []).append(p)
    picked = []
    for d in sorted(by_dir):
        ps = sorted(by_dir[d])
        v4 = [p for p in ps if os.path.basename(p) == "session.v4.jsonl.zstd"]
        picked.append(v4[-1] if v4 else ps[-1])
    return picked


def parse_session(text):
    """-> [(utc_day, model_key, inTokens, outTokens)] per successful step."""
    rows = []
    model = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = o.get("type")
        d = o.get("data") or {}
        if t == "request/context":
            model = d.get("model") or model
        elif t == "request/header":
            cfg = (d.get("header") or {}).get("config") or {}
            if cfg.get("model"):
                model = cfg["model"]
        elif t == "assistant/message":
            u = d.get("usage")
            if not isinstance(u, dict):
                continue
            tin = (u.get("inputTokens") or 0) + (u.get("cacheReadTokens") or 0)
            tout = u.get("outputTokens") or 0
            if not (tin or tout):
                continue
            ms = o.get("time")
            day = datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%d") if ms else "unknown"
            rows.append((day, norm_model(model), tin, tout))
    return rows


def main():
    files = session_files()
    if not files:
        print(json.dumps({"generatedAt": None, "roots": [],
                          "note": "dsh: no session files found"}, ensure_ascii=False))
        return
    out = subprocess.check_output(["node", "-e", _ZSTD_JS] + files, timeout=120)
    blocks = json.loads(out)

    by_day = {}
    sessions = 0
    for f in files:
        rows = parse_session(blocks.get(f) or "")
        if not rows:
            continue
        sessions += 1
        for day, model, tin, tout in rows:
            d = by_day.setdefault(day, {"requests": 0, "inTokens": 0, "outTokens": 0, "byModel": {}})
            m = d["byModel"].setdefault(model, {"requests": 0, "inTokens": 0, "outTokens": 0})
            for t in (d, m):
                t["requests"] += 1
                t["inTokens"] += tin
                t["outTokens"] += tout

    if not by_day:
        print(json.dumps({"generatedAt": None, "roots": [],
                          "note": "dsh: no usage rows found"}, ensure_ascii=False))
        return

    days = [dict(day=k, **by_day[k]) for k in sorted(by_day)]
    print(json.dumps({
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "dsh assistant/message usage: real tokens (input + cacheRead); model from latest request/context",
        "roots": [{
            "key": "dsh", "label": "DSH (DeepSeek Harness)", "sessions": sessions,
            "real": True, "source": "transcript",
            "requests": sum(d["requests"] for d in days),
            "inTokens": sum(d["inTokens"] for d in days),
            "outTokens": sum(d["outTokens"] for d in days),
            "days": days,
        }],
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())