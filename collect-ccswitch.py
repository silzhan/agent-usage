#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""collect-ccswitch.py - read REAL usage rollups from cc-switch's SQLite DB.

cc-switch (provider switcher for Claude Code / Codex / Gemini) pre-aggregates
daily usage (tokens + USD cost) from CLI transcripts into
~/.cc-switch/cc-switch.db BEFORE Claude Code's 30-day auto-cleanup deletes
them, so this is the authoritative historical source.

Outputs the same roots[] schema as collect.ps1.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

# Windows Python 3.8 defaults stdout to GBK; force UTF-8 so server.py can decode it
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.expandvars(r"C:\Users\%USERNAME%\.cc-switch\cc-switch.db")
APPS = {
    "claude":   ("ccs-claude", "Claude（cc-switch 汇总）"),
    "codex":    ("ccs-codex",  "Codex（cc-switch 汇总）"),
    "gemini":   ("ccs-gemini", "Gemini（cc-switch 汇总）"),
    "opencode": ("ccs-opencode", "OpenCode（cc-switch 汇总）"),
}

# Must stay byte-identical in behaviour to NormModel in collect.ps1 and normModel in
# index.html -- changing this also means bumping $StateVersion there.
_MODEL_DATE_RE = re.compile(r"(-[a-z]{1,4})?-[0-9]{4,8}$")
_MODEL_TAIL_RE = re.compile(r"[-_.]+$")


def norm_model(s):
    """`xiaomi/mimo-v2.5-pro` -> `mimo-v2.5-pro`, `deepseek-v4-flash-ga-260731` -> `deepseek-v4-flash`."""
    t = ("" if s is None else str(s)).strip().lower()
    if not t or t == "<synthetic>":
        return "unknown"
    i = t.rfind("/")
    if i >= 0:
        t = t[i + 1:]
    t = _MODEL_DATE_RE.sub("", t)
    t = _MODEL_TAIL_RE.sub("", t)
    return t if t and t != "<synthetic>" else "unknown"


def main():
    if not os.path.exists(DB):
        print(json.dumps({"generatedAt": None, "roots": [], "note": "cc-switch db not found"},
                         ensure_ascii=False))
        return
    con = sqlite3.connect(DB)
    roots = []
    for app, (key, label) in APPS.items():
        # `request_model` is a proxy_request_logs column, not one of these -- selecting it made
        # this query throw and server.py's except-swallow turned it into an empty roots[].
        rows = con.execute(
            "SELECT date, COALESCE(NULLIF(model,''), 'unknown') AS m,"
            "       SUM(request_count), SUM(success_count),"
            "       SUM(input_tokens), SUM(output_tokens),"
            "       SUM(cache_read_tokens), SUM(cache_creation_tokens),"
            "       SUM(CAST(total_cost_usd AS REAL))"
            " FROM usage_daily_rollups WHERE app_type=?"
            " GROUP BY date, m ORDER BY date", (app,)
        ).fetchall()
        if not rows:
            continue
        by_date = {}
        in_sum, out_sum, req_sum, cost_sum = 0, 0, 0, 0.0
        for date, m, rc, sc, it, ot, cr, cw, cost in rows:
            # input side = prompt + cache read + cache write (matches transcript billing view)
            tin = (it or 0) + (cr or 0) + (cw or 0)
            rc = rc or 0
            day = by_date.setdefault(date, {"requests": 0, "inTokens": 0, "outTokens": 0,
                                            "costUsd": 0.0, "byModel": {}})
            day["requests"] += rc
            day["inTokens"] += tin
            day["outTokens"] += ot or 0
            day["costUsd"] += cost or 0.0
            bm = day["byModel"].setdefault(norm_model(m), {"requests": 0, "inTokens": 0,
                                                           "outTokens": 0, "costUsd": 0.0})
            bm["requests"] += rc
            bm["inTokens"] += tin
            bm["outTokens"] += ot or 0
            bm["costUsd"] += cost or 0.0
            in_sum += tin
            out_sum += ot or 0
            req_sum += rc
            cost_sum += cost or 0.0
        days = []
        for date in sorted(by_date):
            d = by_date[date]
            if d["requests"] <= 0:
                continue   # reproduces the old whole-day `HAVING SUM(request_count)>0`
            d["costUsd"] = round(d["costUsd"], 2)
            d["byModel"] = {k: dict(v, costUsd=round(v["costUsd"], 4))
                            for k, v in sorted(d["byModel"].items())}
            days.append(dict(day=date, **d))
        roots.append({
            "key": key, "label": label, "sessions": None,
            "real": True, "source": "cc-switch",
            "requests": req_sum, "inTokens": in_sum, "outTokens": out_sum,
            "costUsd": round(cost_sum, 2),
            "days": days,
        })
    con.close()
    print(json.dumps({
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "conv": None,
        "note": "cc-switch rollups: real tokens + USD cost, aggregated before CC auto-cleanup",
        "roots": roots,
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
