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


def main():
    if not os.path.exists(DB):
        print(json.dumps({"generatedAt": None, "roots": [], "note": "cc-switch db not found"},
                         ensure_ascii=False))
        return
    con = sqlite3.connect(DB)
    roots = []
    for app, (key, label) in APPS.items():
        rows = con.execute(
            "SELECT date, SUM(request_count), SUM(success_count),"
            "       SUM(input_tokens), SUM(output_tokens),"
            "       SUM(cache_read_tokens), SUM(cache_creation_tokens),"
            "       SUM(CAST(total_cost_usd AS REAL))"
            " FROM usage_daily_rollups WHERE app_type=?"
            " GROUP BY date HAVING SUM(request_count)>0 ORDER BY date", (app,)
        ).fetchall()
        if not rows:
            continue
        days, in_sum, out_sum, req_sum, cost_sum = [], 0, 0, 0, 0.0
        for date, rc, sc, it, ot, cr, cw, cost in rows:
            # input side = prompt + cache read + cache write (matches transcript billing view)
            tin = (it or 0) + (cr or 0) + (cw or 0)
            days.append({"day": date, "requests": rc, "inTokens": tin, "outTokens": ot or 0})
            in_sum += tin
            out_sum += ot or 0
            req_sum += rc
            cost_sum += cost or 0.0
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
