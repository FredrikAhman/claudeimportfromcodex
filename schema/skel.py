#!/usr/bin/env python3
"""Dump the structural skeleton (dotted key paths + leaf types) of a JSONL file.

Used to diff the Codex rollout format against the Claude Code transcript format
without dumping the (large, private) values themselves.
"""
import json, sys, collections

paths = collections.Counter()

def walk(o, p=""):
    if isinstance(o, dict):
        if not o:
            paths[p + "{}"] += 1
        for k, v in o.items():
            walk(v, p + "." + k)
    elif isinstance(o, list):
        if not o:
            paths[p + "[]"] += 1
        for v in o[:5]:          # 5 elements is enough to see union types
            walk(v, p + "[]")
    else:
        paths[p + " :" + type(o).__name__] += 1

for fn in sys.argv[1:]:
    with open(fn, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                walk(json.loads(line))
            except Exception:
                pass

for k, v in sorted(paths.items()):
    print(f"{v:8d}  {k}")
