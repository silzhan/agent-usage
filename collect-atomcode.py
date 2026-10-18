#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""collect-atomcode.py - real token usage from AtomCode's session metadata.

AtomCode (~/.atomcode) stores per-turn usage in sessions/<proj>/<id>.meta:
turn_stats[].model_usage[] = {model_id, tokens:{input, output, cached_input}}.
input and cached_input are disjoint (used_tokens == input + cached_input), so
the dashboard input side is input + cached_input - the same billing view as
cc-switch. These are real numbers, not estimates.

The .meta has no per-turn timestamp, so each turn's UTC day is resolved as:
  1. sibling <id>.jsonl record for the same turn_id (ts / iso, UTC) -- only
     some sessions still have this file
  2. ~/.atomcode/datalog/*/<local-ts>_<ms>-<sid>-tN-pX-iY.jsonl filename
     (local wall time, converted to UTC), used only when the session's tN set
     covers exactly 1..turn_count -- retried turns drift the numbering
     (seen: 5 datalogs for 4 turns), and a mismatch falls back instead of
     misassigning days
  3. the session's updated_at (UTC)
Older sessions whose .meta has no model_usage but whose .jsonl records carry
usage:{prompt, completion, cached} are read from the .jsonl instead; prompt
already includes the cache figure, so "cached" is never added on top.

Newer builds write <id>.events (a session event log) instead of <id>.jsonl and
only materialise <id>.meta when the session ends, so a running session would be
invisible until it closes. Sessions with .events but no .meta are therefore read
from the events: kind:"usage" carries {turn, round, usage:{prompt, completion,
cached}} with an exact epoch-ms `at`, and the model comes from the matching
kind:"request_header" event for the same (turn, round). Same prompt-includes-
cached schema as the .jsonl -- never add "cached". Once the .meta appears the
events are skipped, so a session is never counted twice.

Emits the same roots[] schema as collect-ccswitch.py.
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HOME = os.path.expanduser("~")
SESSIONS = os.path.join(HOME, ".atomcode", "sessions")
DATALOG = os.path.join(HOME, ".atomcode", "datalog")

_MODEL_DATE_RE = re.compile(r"(-[a-z]{1,4})?-[0-9]{4,8}$")
_MODEL_TAIL_RE = re.compile(r"[-_.]+$")
_DATALOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_\d+-([0-9a-f-]{36})-t(\d+)-p\d+-i\d+\.jsonl$")


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


def utc_day(ms):
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%d")


def load_jsonl(path):
    """-> ({turn_id: ts_ms}, [records carrying a usage object])"""
    turns, usages = {}, []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return turns, usages
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            tid, ts = o.get("turn_id"), o.get("ts") or o.get("started_at")
            if tid is not None and ts:
                turns[tid] = ts
            if o.get("usage") and not o.get("undone"):
                usages.append(o)
    return turns, usages


def datalog_days():
    """session_id -> {turn_number: utc_day}. Filenames carry local wall time;
    'sessionless' entries match no session and are dropped by the regex."""
    per = {}
    for p in glob.glob(os.path.join(DATALOG, "*", "*.jsonl")):
        if ".cas." in os.path.basename(p):
            continue
        m = _DATALOG_RE.match(os.path.basename(p))
        if not m:
            continue
        try:
            local = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            continue
        day = local.astimezone(timezone.utc).strftime("%Y-%m-%d")
        per.setdefault(m.group(2), {})[int(m.group(3))] = day
    return per


def load_events(path):
    """-> [(at_ms, model, prompt, completion)] from a session event log. The header for a
    (turn, round) always precedes its usage event, so a single sequential pass suffices."""
    models, rows = {}, []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return rows
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            e = o.get("event") or {}
            k = e.get("kind")
            if k == "request_header":
                models[(e.get("turn"), e.get("round"))] = e.get("model")
            elif k == "usage":
                u = e.get("usage") or {}
                tin, tout = u.get("prompt") or 0, u.get("completion") or 0
                if tin or tout:
                    rows.append((o.get("at"), models.get((e.get("turn"), e.get("round"))), tin, tout))
    return rows


def main():
    dl = datalog_days()
    by_day = {}
    sessions = 0

    def add(day, model, req, tin, tout):
        d = by_day.setdefault(day, {"requests": 0, "inTokens": 0, "outTokens": 0, "byModel": {}})
        m = d["byModel"].setdefault(model, {"requests": 0, "inTokens": 0, "outTokens": 0})
        for t in (d, m):
            t["requests"] += req
            t["inTokens"] += tin
            t["outTokens"] += tout

    for meta_path in sorted(glob.glob(os.path.join(SESSIONS, "*", "*.meta"))):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        sid = meta.get("id") or os.path.basename(meta_path)[:-5]
        turn_count = int(meta.get("turn_count") or 0)
        jl_turns, jl_usage = load_jsonl(os.path.join(os.path.dirname(meta_path), sid + ".jsonl"))

        dlmap = dl.get(sid) or {}
        per_turn = dlmap if turn_count and set(dlmap) == set(range(1, turn_count + 1)) else {}
        fallback = utc_day(meta["updated_at"]) if meta.get("updated_at") else "unknown"

        used = False
        for t in meta.get("turn_stats") or []:
            rows = []
            for m in t.get("model_usage") or []:
                tk = m.get("tokens") or {}
                tin = (tk.get("input") or 0) + (tk.get("cached_input") or 0)
                tout = tk.get("output") or 0
                if tin or tout:
                    rows.append((m.get("model_id"), tin, tout))
            if not rows:
                continue
            tid = t.get("turn_id")
            ts = jl_turns.get(tid)
            if ts:
                day = utc_day(ts)
            else:
                day = per_turn.get(tid, fallback)
            req = int(t.get("round_count") or 1) or 1
            for model, tin, tout in rows:
                add(day, norm_model(model), req, tin, tout)
            used = True

        if not used and jl_usage:
            # pre-meta sessions: only the .jsonl kept usage; prompt includes cached
            for o in jl_usage:
                u = o.get("usage") or {}
                tin, tout = u.get("prompt") or 0, u.get("completion") or 0
                if not (tin or tout):
                    continue
                ts = o.get("ts") or o.get("started_at")
                add(utc_day(ts) if ts else "unknown", "unknown", 1, tin, tout)
                used = True

        if used:
            sessions += 1

    # live sessions (new builds): .events exists but .meta only appears on session end.
    # Skipped as soon as the .meta exists -- meta is authoritative, events are provisional.
    for ev_path in sorted(glob.glob(os.path.join(SESSIONS, "*", "*.events"))):
        if os.path.exists(ev_path[:-len(".events")] + ".meta"):
            continue
        used = False
        for at, model, tin, tout in load_events(ev_path):
            add(utc_day(at) if at else "unknown", norm_model(model), 1, tin, tout)
            used = True
        if used:
            sessions += 1

    if not by_day:
        print(json.dumps({"generatedAt": None, "roots": [],
                          "note": "atomcode: no usage rows found"}, ensure_ascii=False))
        return

    days = [dict(day=k, **by_day[k]) for k in sorted(by_day)]
    print(json.dumps({
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "atomcode meta turn_stats: real tokens; day from jsonl ts > datalog filename > session updated_at; live sessions without .meta read from .events",
        "roots": [{
            "key": "atomcode", "label": "AtomCode (.atomcode)", "sessions": sessions,
            "real": True, "source": "meta",
            "requests": sum(d["requests"] for d in days),
            "inTokens": sum(d["inTokens"] for d in days),
            "outTokens": sum(d["outTokens"] for d in days),
            "days": days,
        }],
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())