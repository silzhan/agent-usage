#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""collect-sqlite.py - real token usage from tools that store SQLite, not JSONL.

Covers ZCode (~/.zcode/cli/db/db.sqlite) and the Qoder desktop app
(%APPDATA%/Qoder/SharedClientCache/cache/db/local.db). Both record genuine per-request
usage, so these are `real: true` roots -- unlike the Qoder CLI transcripts, which the
dashboard has to estimate from character counts.

Emits the same roots[] schema as collect-ccswitch.py.

Every database is opened read-only (`mode=ro`). A read-write connection checkpoints the
`-wal` file into the main DB, which mutates another application's live data file.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HOME = os.path.expanduser("~")

# model_usage.input_tokens already contains cache reads, and chat_message.token_info's
# cached_tokens is a subset of prompt_tokens -- both verified against every row on this
# machine. Never add the cache figure to the input side here.
SOURCES = [
    {
        "key": "zcode",
        "label": "ZCode (.zcode/cli)",
        "db": os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite"),
        "sql": "SELECT started_at, model_id, input_tokens, output_tokens, session_id"
               " FROM model_usage WHERE input_tokens IS NOT NULL",
        "ts": "started_at", "model": "model_id",
        "in": "input_tokens", "out": "output_tokens", "sess": "session_id",
    },
    {
        "key": "qoder-ide",
        "label": "Qoder 桌面端",
        "db": os.path.join(os.environ.get("APPDATA", ""), "Qoder",
                           "SharedClientCache", "cache", "db", "local.db"),
        "sql": "SELECT gmt_create, model_info, token_info, session_id"
               " FROM chat_message WHERE role = 'assistant'",
        "ts": "gmt_create", "model": "model_info", "usage": "token_info",
        "sess": "session_id",
    },
]

_MODEL_DATE_RE = re.compile(r"(-[a-z]{1,4})?-[0-9]{4,8}$")
_MODEL_TAIL_RE = re.compile(r"[-_.]+$")


def norm_model(s):
    """Byte-identical to NormModel in collect.ps1 and norm_model in collect-ccswitch.py."""
    t = ("" if s is None else str(s)).strip().lower()
    if not t or t == "<synthetic>":
        return "unknown"
    i = t.rfind("/")
    if i >= 0:
        t = t[i + 1:]
    t = _MODEL_DATE_RE.sub("", t)
    t = _MODEL_TAIL_RE.sub("", t)
    return t if t and t != "<synthetic>" else "unknown"


def utc_day(ms):
    return datetime.utcfromtimestamp(ms / 1000.0).strftime("%Y-%m-%d")


def read_json(cell):
    """token_info / model_info are JSON text columns; a NULL, '' or truncated row is not fatal."""
    if not cell:
        return {}
    try:
        v = json.loads(cell)
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def collect(src):
    if not os.path.exists(src["db"]):
        return None
    con = sqlite3.connect("file:" + src["db"].replace("\\", "/") + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(src["sql"]).fetchall()
    finally:
        con.close()

    by_date = {}
    sessions = set()
    in_sum = out_sum = req_sum = 0
    for r in rows:
        if "usage" in src:
            ti = read_json(r[src["usage"]])
            it, ot = ti.get("prompt_tokens"), ti.get("completion_tokens")
            model = read_json(r[src["model"]]).get("model_key") or ""
        else:
            it, ot, model = r[src["in"]], r[src["out"]], r[src["model"]]
        if it is None and ot is None:
            continue
        it, ot = int(it or 0), int(ot or 0)
        ts = r[src["ts"]]
        day = utc_day(ts) if ts else "unknown"
        sessions.add(r[src["sess"]])
        d = by_date.setdefault(day, {"requests": 0, "inTokens": 0, "outTokens": 0, "byModel": {}})
        m = d["byModel"].setdefault(norm_model(model), {"requests": 0, "inTokens": 0, "outTokens": 0})
        d["requests"] += 1
        d["inTokens"] += it
        d["outTokens"] += ot
        m["requests"] += 1
        m["inTokens"] += it
        m["outTokens"] += ot
        in_sum += it
        out_sum += ot
        req_sum += 1

    if not req_sum:
        return None
    return {
        "key": src["key"], "label": src["label"], "sessions": len(sessions),
        "real": True, "source": "sqlite",
        "requests": req_sum, "inTokens": in_sum, "outTokens": out_sum,
        "days": [dict(day=k, **by_date[k]) for k in sorted(by_date)],
    }


def main():
    roots, notes = [], []
    for src in SOURCES:
        try:
            got = collect(src)
        except sqlite3.Error as e:
            notes.append("%s: %s" % (src["key"], e))
            continue
        if got:
            roots.append(got)
        else:
            notes.append("%s: no rows (db missing or empty)" % src["key"])
    print(json.dumps({
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "sqlite-backed real usage; cache reads are already inside the input figure",
        "notes": notes,
        "roots": roots,
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
