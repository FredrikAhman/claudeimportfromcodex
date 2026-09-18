#!/usr/bin/env python3
"""
codex_import — convert OpenAI Codex rollout transcripts into Claude Code sessions.

Codex writes one JSONL "rollout" per thread under ~/.codex/sessions/YYYY/MM/DD/.
Claude Code reads one JSONL per session from ~/.claude/projects/<slug>/<uuid>.jsonl,
where <slug> is the session's cwd with every non-alphanumeric character replaced by
a dash. The desktop app additionally needs a session card (see desktop_register.py).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desktop_register

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# Codex records every Claude session it imported here. Those threads are Claude
# history that already round-tripped once; importing them whole would duplicate
# transcripts the user already has, so only their Codex-side turns come back.
CODEX_IMPORT_LEDGER = Path.home() / ".codex" / "external_agent_session_imports.json"
# Our own ledger, so a re-run skips what it already did and `uninstall` can remove
# exactly what we created and nothing else. It lives beside the data it describes,
# not in this checkout: moving or deleting the tool must not orphan the imports.
LEDGER = Path.home() / ".claude" / "codex-imports.json"
LEGACY_LEDGER = Path(__file__).resolve().parent.parent / "imported.json"

TOOL_VERSION = "1.0"

# Stamped into every assistant record we synthesise.
IMPORT_MODEL = "codex-imported"
# Stamped on the trailing custom-title record of every transcript we write. Unlike
# IMPORT_MODEL it is present even in a session with no assistant turns, so it is what
# `uninstall --scan` fingerprints when the ledger is missing.
IMPORT_MARK = "codex-import"

# Codex record types that carry conversation content. Everything else in a rollout
# (event_msg/*, token_count, token_usage_record, world_state, thread_settings_applied)
# is either UI-stream duplication of these or pure telemetry.
CONTENT_TYPES = {"message", "reasoning", "function_call", "function_call_output",
                 "custom_tool_call", "custom_tool_call_output"}

# Codex tool -> Claude Code tool. Only names whose *input shape* we can honestly
# rewrite are mapped; everything else keeps its Codex name behind a prefix so it is
# visibly foreign rather than silently impersonating a real Claude tool.
TOOL_MAP = {"exec": "Bash", "shell": "Bash", "local_shell": "Bash"}

# When Codex imports a Claude session it replays that history as turns with these ids,
# ending in a task_complete. Everything after the last one was done in Codex.
EXTERNAL_TURN_PREFIX = "external-import-turn"

# Where transcripts are copied before new Codex turns are appended to them.
BACKUPS = Path.home() / ".claude" / "codex-import-backups"


# ------------------------------------------------------- machine-preamble stripping
#
# Codex packs machine-generated content into `role: "user"` messages: a
# <recommended_plugins> catalogue (often thousands of words), the project's AGENTS.md,
# an <environment_context> block, pasted-file manifests. Imported verbatim, all of it
# renders as though the user typed it — the single biggest source of mess. Anything
# matched here is background, and is emitted as `isMeta: true` instead.

WRAPPER_TAGS = ("recommended_plugins", "system-reminder", "command-name",
                "command-message", "command-args", "command-contents",
                "user_instructions", "environment_context", "turn_aborted",
                "local-command-stdout", "local-command-caveat", "INSTRUCTIONS",
                "user-prompt-submit-hook")
_TAGS = "|".join(WRAPPER_TAGS)
WRAPPER_RE = re.compile(rf"<({_TAGS})\b.*?</\1>|<({_TAGS})\b[^>]*/?>", re.S | re.I)

# Codex separates its preamble from the real prompt with this header. When present
# it is authoritative — everything after it is what the human actually typed.
REQUEST_RE = re.compile(r"^#{1,6}[ \t]*My request:?[ \t]*$", re.M | re.I)

# "# Files pasted by the user:" … runs until the next top-level heading.
FILES_BLOCK_RE = re.compile(
    r"^#{1,6}[ \t]*Files (?:mentioned|pasted|attached) by the user:?.*?(?=^#{1,6}[ \t]|\Z)",
    re.M | re.S | re.I)
AGENTS_HDR_RE = re.compile(r"^#{1,6}[ \t]*(AGENTS|CLAUDE)\.md instructions[ \t]*$", re.M | re.I)

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
URL_RE = re.compile(r"https?://([^/\s]+)(/\S*)?")
# Boilerplate that survives stripping but says nothing about the thread.
NOISE_RE = re.compile(
    r"^(files (mentioned|pasted|attached) by the user|pasted text contains|"
    r"background command completed|my request|attachments?:|"
    r"distinguish instructions in attached documents|"
    r"agents\.md instructions|claude\.md instructions)\b", re.I)
# Attachment manifest entries — "some-name: /Users/…/file" — name a file, not a topic.
MANIFEST_RE = re.compile(r"^\S.{0,80}?:\s*[~/]\S+$")


def split_user_message(raw: str) -> tuple[str, str]:
    """Split a Codex user turn into (background, human). Either may be empty."""
    raw = raw or ""
    m = REQUEST_RE.search(raw)
    if m:
        return raw[:m.start()].strip(), raw[m.end():].strip()

    removed: list[str] = []

    def cut(mo: re.Match) -> str:
        removed.append(mo.group(0))
        return "\n"

    txt = WRAPPER_RE.sub(cut, raw)
    txt = FILES_BLOCK_RE.sub(cut, txt)
    txt = AGENTS_HDR_RE.sub(cut, txt)
    human = txt.strip()
    background = "\n\n".join(x.strip() for x in removed).strip()
    if not human:
        return (background or raw.strip()), ""
    return background, human


def clean_title(raw: str) -> str:
    """First line of real prose in a user turn, tidied enough to read in a sidebar."""
    _, human = split_user_message(raw)
    human = MD_LINK_RE.sub(r"\1", human)
    # A pasted URL is rarely what the thread is *about*, and it eats the whole
    # sidebar width. Drop it and keep the prose around it.
    human = URL_RE.sub(" ", human)
    for line in human.splitlines():
        line = line.strip().strip("#").strip().strip('"“”\'')
        if len(line) < 12 or line.startswith(("- ", "* ", "/", "|", "```")):
            continue
        if NOISE_RE.match(line) or MANIFEST_RE.match(line):
            continue
        return re.sub(r"\s+", " ", line)[:60].strip()
    return ""


# --------------------------------------------------------------------------- utils

def slug_for(cwd: str) -> str:
    """Claude Code's project-directory rule: every non-alphanumeric char becomes '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


# Codex Desktop gives every thread its own throwaway workspace at
# ~/Documents/Codex/<date>/<thread-slug>. Imported as-is, each one becomes its own
# folder in the Claude sidebar — ten threads, ten junk folders named after truncated
# prompt slugs. They collapse to the shared parent instead.
SCRATCH_RE = re.compile(r"^(?P<root>.*/Documents/Codex)/\d{4}-\d{2}-\d{2}/[^/]+/?$")


def group_scratch(cwd: str) -> str:
    m = SCRATCH_RE.match(cwd or "")
    return m.group("root") if m else cwd


def iso(ts: str | None) -> str:
    if ts:
        return ts
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ms_of(ts: str) -> int | None:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def git_branch(cwd: str) -> str:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def claude_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().split()[0]
    except Exception:
        return "2.1.271"


def text_of(blocks) -> str:
    """Flatten a Codex content/output array into plain text."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") in ("input_text", "output_text", "text"):
            parts.append(b.get("text", ""))
        elif b.get("type") == "input_image":
            parts.append("[image]")
    return "".join(parts)


def unwrap_exec(raw: str) -> str:
    """Codex `exec` output is a JSON envelope around the real stdout. Dig it out."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    if isinstance(obj, dict) and "output" in obj and isinstance(obj["output"], str):
        exit_code = obj.get("exit_code")
        body = obj["output"]
        return body if exit_code in (0, None) else f"(exit {exit_code})\n{body}"
    return raw


# ----------------------------------------------------------------------- discovery

def records_from(data: bytes, start: int = 0) -> list[tuple[int, dict]]:
    """(offset just past the line, record) for each complete JSON line in data[start:].

    A line Codex is still writing fails to parse and is left for the next run, so an
    offset taken from here never lands inside a record.
    """
    out = []
    pos = start
    while pos < len(data):
        nl = data.find(b"\n", pos)
        end = len(data) if nl < 0 else nl + 1
        line = data[pos:end].strip()
        if line:
            try:
                out.append((end, json.loads(line)))
            except Exception:
                pass
        pos = end
    return out


def read_rollout(path: Path) -> list[dict]:
    return [r for _, r in records_from(path.read_bytes())]


def external_import_end(recs: list[tuple[int, dict]]) -> int | None:
    """Offset just past the Claude history Codex replayed into a round-trip thread."""
    end = None
    for off, r in recs:
        p = r.get("payload") or {}
        if (r.get("type") == "event_msg" and p.get("type") == "task_complete"
                and str(p.get("turn_id", "")).startswith(EXTERNAL_TURN_PREFIX)):
            end = off
    return end


def prefix_offset(data: bytes, digest: str) -> int | None:
    """Length of the line-aligned prefix of `data` whose sha256 is `digest`.

    Codex only ever appends to a rollout, so for ledger entries written before byte
    offsets were recorded, the sha taken at import time recovers how much of the
    file that import covered.
    """
    h = hashlib.sha256()
    pos = 0
    while True:
        if h.hexdigest() == digest:
            return pos
        if pos >= len(data):
            return None
        nl = data.find(b"\n", pos)
        end = len(data) if nl < 0 else nl + 1
        h.update(data[pos:end])
        pos = end


def round_tripped_ids() -> dict[str, str]:
    """Codex thread id -> the Claude transcript it was originally imported from."""
    try:
        data = json.loads(CODEX_IMPORT_LEDGER.read_text())
    except Exception:
        return {}
    recs = data.get("records", data if isinstance(data, list) else [])
    return {r["imported_thread_id"]: r.get("source_path", "")
            for r in recs if r.get("imported_thread_id")}


def summarize(path: Path) -> dict | None:
    """Cheap header read: metadata + a usable title, without parsing the whole file."""
    meta, n = None, 0
    candidates: list[str] = []
    with path.open(errors="replace") as fh:
        for line in fh:
            n += 1
            if meta and len(candidates) >= 6:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") == "session_meta":
                meta = r.get("payload", {})
            elif (len(candidates) < 6 and r.get("type") == "response_item"
                  and r.get("payload", {}).get("type") == "message"
                  and r["payload"].get("role") == "user"):
                candidates.append(text_of(r["payload"].get("content")))
    if not meta:
        return None
    return {"path": path, "id": meta.get("id") or meta.get("session_id"),
            "cwd": meta.get("cwd", ""), "ts": meta.get("timestamp", ""),
            "originator": meta.get("originator", ""), "lines": n,
            "first_user": next((t for t in (clean_title(c) for c in candidates) if t), "")}


def discover(root: Path) -> list[dict]:
    out = []
    for p in sorted(root.rglob("*.jsonl")):
        s = summarize(p)
        if s:
            out.append(s)
    ledger = round_tripped_ids()
    for s in out:
        s["round_trip"] = ledger.get(s["id"] or "", "")
    out.sort(key=lambda s: s["ts"])
    return out


# -------------------------------------------------------------------------- ledger

def load_ledger() -> dict:
    for path in (LEDGER, LEGACY_LEDGER):
        try:
            return json.loads(path.read_text())
        except Exception:
            continue
    return {}


def save_ledger(d: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, indent=2, sort_keys=True))
    if LEGACY_LEDGER.exists():
        LEGACY_LEDGER.unlink()


def provenance_banner(sess: dict, mode: str) -> str:
    """Shown as background at the top of an imported thread, so its origin is visible
    when the session is opened, not only recorded in metadata."""
    return (f"Imported from OpenAI Codex by codeximporterforclaude {TOOL_VERSION}.\n"
            f"Codex thread {sess['id']}, started {sess['ts'][:19]} "
            f"({sess['originator'] or 'codex'}).\n"
            f"Source rollout: {sess['path']}\n"
            f"Mode: {mode}. Reasoning is not carried over — Codex stores it encrypted, "
            f"and Anthropic thinking blocks require a signature that cannot be minted.")


# ----------------------------------------------------------------------- conversion

class Converter:
    def __init__(self, sess: dict, mode: str, keep_developer: bool, version: str,
                 cwd: str, session_id: str | None = None, parent: str | None = None):
        self.mode = mode
        self.keep_developer = keep_developer
        self.version = version
        # Both set when continuing an existing transcript rather than starting one.
        self.session_id = session_id or str(uuid.uuid4())
        self.cwd = cwd
        self.branch = git_branch(self.cwd)
        self.parent = parent
        self.out: list[dict] = []
        self.pending_calls: dict[str, str] = {}   # codex call_id -> claude toolu id
        self.open_tool_use: str | None = None     # toolu id awaiting a tool_result
        self.stats = {"user": 0, "assistant": 0, "tool_use": 0, "tool_result": 0,
                      "reasoning_kept": 0, "reasoning_dropped": 0, "background": 0,
                      "orphan_results": 0, "synthetic_results": 0}

    # -- envelope ----------------------------------------------------------------
    def envelope(self, kind: str, ts: str) -> dict:
        u = str(uuid.uuid4())
        rec = {
            "parentUuid": self.parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": self.version,
            "gitBranch": self.branch,
            "entrypoint": "cli",
            "type": kind,
            "uuid": u,
            "timestamp": iso(ts),
        }
        self.parent = u
        return rec

    def emit_user(self, content, ts: str, meta: bool = False):
        rec = self.envelope("user", ts)
        rec["message"] = {"role": "user", "content": content}
        if meta:
            # Claude Code's own marker for machine-generated turns (its
            # <local-command-caveat> records use it). Keeps the content in the
            # transcript without rendering it as something the user said.
            rec["isMeta"] = True
        self.out.append(rec)
        return rec

    def emit_assistant(self, content: list, ts: str, stop: str = "end_turn"):
        rec = self.envelope("assistant", ts)
        rec["message"] = {
            "id": "msg_codeximport_" + uuid.uuid4().hex[:20],
            "type": "message",
            "role": "assistant",
            "model": IMPORT_MODEL,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      "service_tier": "standard"},
        }
        self.out.append(rec)
        return rec

    # -- tool-call pairing --------------------------------------------------------
    def close_open_tool_use(self, ts: str):
        """The API requires every tool_use to be answered. Backfill if Codex didn't."""
        if self.open_tool_use:
            self.stats["synthetic_results"] += 1
            self.emit_user([{"type": "tool_result", "tool_use_id": self.open_tool_use,
                             "content": "(no output recorded in the Codex rollout)",
                             "is_error": False}], ts)
            self.open_tool_use = None

    def do_tool_call(self, p: dict, ts: str):
        call_id = p.get("call_id") or p.get("id") or uuid.uuid4().hex
        raw_name = p.get("name", "tool")
        ns = p.get("namespace")
        full = f"{ns}__{raw_name}" if ns else raw_name

        if p.get("type") == "function_call":
            try:
                args = json.loads(p.get("arguments") or "{}")
            except Exception:
                args = {"arguments": p.get("arguments", "")}
        else:
            args = {"command": p.get("input", ""), "description": f"codex {raw_name}"}

        if self.mode == "archive":
            body = args.get("command") or json.dumps(args, indent=2, ensure_ascii=False)
            self.emit_assistant(
                [{"type": "text", "text": f"**[codex tool call: `{full}`]**\n\n```\n{body}\n```"}], ts)
            self.pending_calls[call_id] = None
            return

        self.close_open_tool_use(ts)
        name = TOOL_MAP.get(raw_name, full if str(full).startswith("mcp__") else f"codex__{full}")
        if name == "Bash" and "description" not in args:
            args["description"] = "imported from Codex"
        tid = "toolu_codeximport_" + uuid.uuid4().hex[:20]
        self.pending_calls[call_id] = tid
        self.open_tool_use = tid
        self.stats["tool_use"] += 1
        self.emit_assistant([{"type": "tool_use", "id": tid, "name": name, "input": args}],
                            ts, stop="tool_use")

    def do_tool_output(self, p: dict, ts: str):
        call_id = p.get("call_id")
        body = unwrap_exec(text_of(p.get("output")))

        if self.mode == "archive":
            self.emit_user(f"**[codex tool output]**\n\n```\n{body}\n```", ts, meta=True)
            return

        tid = self.pending_calls.get(call_id)
        if not tid:
            # A result with no call in this file (e.g. a rollout that starts mid-turn).
            self.stats["orphan_results"] += 1
            self.emit_user(f"**[orphaned codex tool output]**\n\n```\n{body}\n```", ts, meta=True)
            return
        self.stats["tool_result"] += 1
        self.open_tool_use = None
        rec = self.emit_user([{"type": "tool_result", "tool_use_id": tid,
                               "content": body, "is_error": False}], ts)
        rec["toolUseResult"] = {"stdout": body, "stderr": "", "interrupted": False,
                                "isImage": False}

    # -- main loop ----------------------------------------------------------------
    def run(self, recs: list[dict]) -> list[dict]:
        for r in recs:
            if r.get("type") != "response_item":
                continue
            p = r.get("payload") or {}
            t = p.get("type")
            if t not in CONTENT_TYPES:
                continue
            ts = r.get("timestamp", "")

            if t == "message":
                role = p.get("role")
                body = text_of(p.get("content")).strip()
                if not body:
                    continue
                if role == "user":
                    self.close_open_tool_use(ts)
                    background, human = split_user_message(body)
                    if background:
                        self.stats["background"] += 1
                        self.emit_user(background, ts, meta=True)
                    if human:
                        self.stats["user"] += 1
                        self.emit_user(human, ts)
                elif role == "assistant":
                    self.stats["assistant"] += 1
                    self.emit_assistant([{"type": "text", "text": body}], ts)
                elif role == "developer":
                    if self.keep_developer:
                        self.stats["background"] += 1
                        self.emit_user(body, ts, meta=True)

            elif t == "reasoning":
                # Codex reasoning lives in `encrypted_content`, an opaque OpenAI blob.
                # It cannot become a Claude `thinking` block: those carry an
                # Anthropic-issued `signature` that is verified on resume, and there
                # is no way to mint one. Only the plaintext summary survives.
                summary = " ".join(s.get("text", "") for s in (p.get("summary") or [])
                                   if isinstance(s, dict)).strip()
                if summary:
                    self.stats["reasoning_kept"] += 1
                    self.emit_assistant([{"type": "text", "text": f"_(codex reasoning)_ {summary}"}], ts)
                else:
                    self.stats["reasoning_dropped"] += 1

            elif t in ("function_call", "custom_tool_call"):
                self.do_tool_call(p, ts)

            elif t in ("function_call_output", "custom_tool_call_output"):
                self.do_tool_output(p, ts)

        self.close_open_tool_use(self.out[-1]["timestamp"] if self.out else "")
        return self.out


def validate(recs: list[dict], root: str | None = None) -> list[str]:
    """Check the invariants the Messages API enforces on replayed history.

    `root` is the uuid the first record hangs off when `recs` continues a transcript.
    """
    errs = []
    seen = set()
    prev = root
    open_ids: set[str] = set()
    for i, r in enumerate(recs):
        u = r.get("uuid")
        if u in seen:
            errs.append(f"line {i}: duplicate uuid {u}")
        seen.add(u)
        if r.get("parentUuid") != prev:
            errs.append(f"line {i}: parentUuid chain broken")
        prev = u
        content = (r.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_use":
                    open_ids.add(b["id"])
                elif b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid not in open_ids:
                        errs.append(f"line {i}: tool_result for unknown tool_use {tid}")
                    else:
                        open_ids.discard(tid)
    for tid in open_ids:
        errs.append(f"unanswered tool_use {tid}")
    return errs


# ----------------------------------------------------------------------------- sync
#
# A thread can move on in Codex after it reached Claude: an import of ours the user
# kept working on in Codex, or a Claude session Codex imported (a "round-trip") that
# was continued there. Either way the new Codex turns are appended to the Claude
# transcript that already holds the thread, so it stays one conversation.

# Records that sit on the conversation chain. Others (last-prompt, custom-title,
# file-history-snapshot, ...) are session metadata and must not be a parent.
CHAIN_TYPES = {"user", "assistant", "system", "attachment"}


def transcript_tail(path: Path) -> dict:
    """Where an existing Claude transcript's conversation ends, and what it is called."""
    info = {"leaf": None, "cwd": None, "sessionId": None, "title": None, "lastTs": ""}
    with path.open(errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") in CHAIN_TYPES and r.get("uuid") and not r.get("isSidechain"):
                info["leaf"] = r["uuid"]
                info["cwd"] = r.get("cwd") or info["cwd"]
                info["sessionId"] = r.get("sessionId") or info["sessionId"]
                info["lastTs"] = r.get("timestamp") or info["lastTs"]
            elif r.get("type") == "custom-title":
                info["title"] = r.get("customTitle") or info["title"]
    return info


def sync_banner(sess: dict, mode: str, first: str, last: str) -> str:
    return (f"Continued in OpenAI Codex, {first[:16].replace('T', ' ')} to "
            f"{last[:16].replace('T', ' ')} UTC. Brought across by "
            f"codeximporterforclaude {TOOL_VERSION} from Codex thread {sess['id']}.\n"
            f"Source rollout: {sess['path']}\n"
            f"Mode: {mode}. Reasoning is not carried over.")


def append_codex_turns(sess: dict, data: bytes, start: int, target: Path, *, mode: str,
                       keep_developer: bool, version: str, banner: bool = True,
                       native: bool = False, dry_run: bool = False) -> dict:
    """Convert the rollout past byte `start` and continue `target`'s conversation with it.

    `native` means the transcript is a Claude session rather than one this tool wrote,
    so no import title/provenance record is appended to it.
    """
    recs = records_from(data, start)
    result = {"end": recs[-1][0] if recs else start, "out": [], "errs": [],
              "backup": None, "tail": None}
    content = [r for _, r in recs if r.get("type") == "response_item"
               and (r.get("payload") or {}).get("type") in CONTENT_TYPES]
    if not content:
        return result

    tail = transcript_tail(target)
    result["tail"] = tail
    conv = Converter(sess, mode, keep_developer, version,
                     tail["cwd"] or sess["cwd"] or str(Path.home()),
                     session_id=tail["sessionId"] or target.stem, parent=tail["leaf"])
    first, last = content[0].get("timestamp", ""), content[-1].get("timestamp", "")
    if banner:
        conv.emit_user(sync_banner(sess, mode, first, last), first, meta=True)
    n0 = len(conv.out)
    out = conv.run([r for _, r in recs])
    if len(out) == n0:
        return result
    result.update(out=out, errs=validate(out, root=tail["leaf"]), stats=conv.stats,
                  first=first, last=last, sessionId=conv.session_id)

    lines = [json.dumps(r, ensure_ascii=False) for r in out]
    said = [r["message"]["content"] for r in out if r["type"] == "user"
            and not r.get("isMeta") and isinstance(r["message"]["content"], str)]
    # Claude Code records the conversation's leaf here; point it past the new turns.
    lines.append(json.dumps({"type": "last-prompt", "lastPrompt": (said or [""])[-1][:200],
                             "leafUuid": out[-1]["uuid"], "sessionId": conv.session_id},
                            ensure_ascii=False))
    digest = hashlib.sha256(data[:result["end"]]).hexdigest()
    result["digest"] = digest
    if not native:
        lines.append(json.dumps({
            "type": "custom-title", "customTitle": tail["title"] or sess["first_user"],
            "sessionId": conv.session_id, "importedFrom": IMPORT_MARK,
            "codexThreadId": sess["id"], "codexRollout": str(sess["path"]),
            "codexRolloutSha256": digest,
            "syncedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "importToolVersion": TOOL_VERSION,
        }, ensure_ascii=False))

    if not dry_run:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = BACKUPS / f"{target.stem}.{stamp}.jsonl"
        backup.write_bytes(existing := target.read_bytes())
        result["backup"] = backup
        with target.open("a") as fh:
            if existing and not existing.endswith(b"\n"):
                fh.write("\n")
            fh.write("\n".join(lines) + "\n")
    return result


# ----------------------------------------------------------------------------- cli

def cmd_list(args):
    sessions = discover(Path(args.codex_dir))
    if not sessions:
        print(f"No Codex rollouts found under {args.codex_dir}")
        return
    done = load_ledger()
    print(f"{len(sessions)} Codex session(s) under {args.codex_dir}\n")
    for s in sessions:
        tags = []
        if s["round_trip"]:
            tags.append("round-trip from Claude")
        if s["id"] in done:
            tags.append("synced back" if done[s["id"]].get("kind") == "round-trip"
                        else "already imported")
        tag = f"  [{'; '.join(tags)}]" if tags else ""
        print(f"  {s['id']}{tag}")
        print(f"    {s['ts'][:19]}  {s['lines']:>6} lines  {s['originator']}")
        print(f"    cwd: {s['cwd'] or '(none)'}")
        print(f"    {s['first_user'] or '(no user message)'}")
        print()


def cmd_import(args):
    sessions = discover(Path(args.codex_dir))
    if args.session:
        sessions = [s for s in sessions if s["id"] and s["id"].startswith(args.session)]
        if not sessions:
            sys.exit(f"No Codex session matching {args.session!r}")
    elif not args.all:
        sys.exit("Pass --all, or --session <id-prefix>. Use `list` to see them.")

    # Threads already in Claude — ours, or Claude sessions Codex imported — get any
    # newer Codex turns appended instead of a second copy.
    ledger = load_ledger()
    syncs, fresh = [], []
    for s in sessions:
        entry = ledger.get(s["id"])
        if s["round_trip"] and not args.include_round_trips:
            (syncs if not args.no_sync else []).append(s)
            if args.no_sync:
                print(f"skip {s['id'][:8]}  (round-trip: Codex imported this from "
                      f"{Path(s['round_trip']).name})")
        elif entry and not args.force:
            (syncs if not args.no_sync else []).append(s)
            if args.no_sync:
                print(f"skip {s['id'][:8]}  (already imported {entry['importedAt'][:10]})")
        else:
            fresh.append(s)

    version = claude_version()
    state = {"warned": False, "current": 0, "appended": 0}
    for s in syncs:
        sync_one(s, ledger.get(s["id"]), args, version, ledger, state)
    if state["current"]:
        print(f"  -- {state['current']} already in Claude and up to date\n")
    sessions = fresh

    if not sessions:
        if not state["appended"]:
            print("Nothing to do.")
        return

    if args.desktop and not args.dry_run and desktop_register.desktop_running():
        print("! Claude desktop is running. It caches the session list in memory and "
              "may not\n  pick these up until relaunch. Quit it first, or quit and "
              "reopen afterwards.\n")

    for s in sessions:
        data = s["path"].read_bytes()
        parsed = records_from(data)
        recs = [r for _, r in parsed]
        consumed = parsed[-1][0] if parsed else 0
        cwd = s["cwd"] or str(Path.home())
        if not args.no_group_scratch:
            cwd = group_scratch(cwd)
        conv = Converter(s, args.mode, args.keep_developer, version, cwd)
        if not args.no_banner:
            conv.emit_user(provenance_banner(s, args.mode), s["ts"], meta=True)
        out = conv.run(recs)
        if not out:
            print(f"skip {s['id'][:8]}  (no convertible content)")
            continue

        errs = validate(out)
        target_dir = Path(args.claude_dir) / slug_for(cwd)
        target = target_dir / f"{conv.session_id}.jsonl"

        base = s["first_user"] or "Codex import"
        title = f"{args.title_prefix}{base}" if args.title_prefix else base
        lines = [json.dumps(r, ensure_ascii=False) for r in out]
        digest = hashlib.sha256(data[:consumed]).hexdigest()
        lines.append(json.dumps({
            "type": "custom-title", "customTitle": title,
            "sessionId": conv.session_id,
            # Provenance, carried in the transcript itself so it survives losing the
            # ledger, moving the file, or deleting this checkout.
            "importedFrom": IMPORT_MARK,
            "codexThreadId": s["id"],
            "codexRollout": str(s["path"]),
            "codexRolloutSha256": digest,
            "importedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "importMode": args.mode,
            "importToolVersion": TOOL_VERSION,
        }, ensure_ascii=False))

        if not args.dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines) + "\n")

        card = None
        if args.desktop:
            card = desktop_register.register(
                conv.session_id, cwd, title, created_ms=ms_of(s["ts"]),
                turns=conv.stats["user"], dry_run=args.dry_run,
                provenance={"source": IMPORT_MARK, "codexThreadId": s["id"],
                            "codexRollout": str(s["path"]),
                            "importToolVersion": TOOL_VERSION})

        print(f"{'would write' if args.dry_run else 'wrote'} {target}")
        print(f"    from {s['path'].name}")
        print(f"    title: {title}")
        print_stats(conv.stats, errs)
        if args.desktop:
            print(f"    desktop card: {card.name if card else '! registry not found'}")

        if not args.dry_run:
            ledger[s["id"]] = {
                "claudeSessionId": conv.session_id,
                "transcript": str(target),
                "card": str(card) if card else None,
                "cwd": cwd,
                "title": title,
                "importedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sourceRollout": str(s["path"]),
                "sourceSha256": digest,
                "sourceMtime": int(s["path"].stat().st_mtime),
                "sourceBytes": consumed,
                "kind": "import",
                "mode": args.mode,
                "toolVersion": TOOL_VERSION,
            }
            save_ledger(ledger)
        print()


def print_stats(st: dict, errs: list[str]) -> None:
    print(f"    {st['user']} user, {st['assistant']} assistant, "
          f"{st['tool_use']} tool calls, {st['tool_result']} results, "
          f"{st['background']} background")
    print(f"    reasoning: {st['reasoning_kept']} kept as text, "
          f"{st['reasoning_dropped']} dropped (encrypted, unrecoverable)")
    if st["synthetic_results"]:
        print(f"    backfilled {st['synthetic_results']} missing tool result(s)")
    if st["orphan_results"]:
        print(f"    {st['orphan_results']} orphaned tool output(s) kept as text")
    if errs:
        print("    VALIDATION:")
        for e in errs[:10]:
            print(f"      ! {e}")


def sync_one(s: dict, entry: dict | None, args, version: str, ledger: dict,
             state: dict) -> None:
    """Append Codex turns newer than what Claude already has for this thread."""
    tid = s["id"]
    native = entry is None or entry.get("kind") == "round-trip"
    size = s["path"].stat().st_size
    if entry and entry.get("sourceBytes") == size:
        state["current"] += 1
        return
    data = s["path"].read_bytes()
    if entry and entry.get("sourceBytes") is not None:
        start = entry["sourceBytes"]
    elif entry:
        start = prefix_offset(data, entry.get("sourceSha256", ""))
    else:
        start = external_import_end(records_from(data))
    if start is None or start > len(data):
        print(f"skip {tid[:8]}  (" + (
            "the rollout was rewritten rather than appended to since import; "
            "`import --force` re-imports it" if entry else
            "round-trip, but there is no marker for where Codex's copy of the Claude "
            "history ends") + ")")
        return
    newer = records_from(data, start)
    if not any(r.get("type") == "response_item"
               and (r.get("payload") or {}).get("type") in CONTENT_TYPES
               for _, r in newer):
        state["current"] += 1
        end = newer[-1][0] if newer else start
        if entry and not args.dry_run and entry.get("sourceBytes") != end:
            entry["sourceBytes"] = end
            save_ledger(ledger)
        return

    target = Path(entry["transcript"] if entry else s["round_trip"])
    if not target.exists():
        hits = list(Path(args.claude_dir).glob(f"*/{target.stem}.jsonl"))
        if not hits:
            print(f"skip {tid[:8]}  (" + (
                "has newer Codex turns, but its Claude transcript is gone; "
                "`import --force` makes a fresh copy" if entry else
                "round-trip with newer Codex turns, but the Claude session Codex "
                "imported it from is gone; --include-round-trips imports it as a new "
                "session") + ")")
            return
        target = hits[0]

    mode = entry.get("mode", args.mode) if entry else args.mode
    res = append_codex_turns(s, data, start, target, mode=mode,
                             keep_developer=args.keep_developer, version=version,
                             banner=not args.no_banner, native=native,
                             dry_run=args.dry_run)
    if not res["out"]:
        state["current"] += 1
        if entry and not args.dry_run and entry.get("sourceBytes") != res["end"]:
            entry["sourceBytes"] = res["end"]
            save_ledger(ledger)
        return

    if not args.dry_run and not state["warned"] and desktop_register.desktop_running():
        state["warned"] = True
        print("! Claude desktop is running. A session it already has open keeps its "
              "cached copy,\n  and a message sent there would branch off before the "
              "Codex turns. Quit and\n  reopen it before continuing these sessions.\n")

    state["appended"] += 1
    tail = res["tail"]
    title = tail["title"] or (entry or {}).get("title") or s["first_user"]
    print(f"{'would append to' if args.dry_run else 'appended to'} {target}")
    print(f"    from {s['path'].name}, Codex turns {res['first'][:16].replace('T', ' ')}"
          f" to {res['last'][:16].replace('T', ' ')}")
    print(f"    title: {title}" + ("  (native Claude session; Codex imported it)"
                                   if native else ""))
    print_stats(res["stats"], res["errs"])
    if tail["lastTs"] > res["first"]:
        print(f"    note: this session also moved on in Claude (last turn "
              f"{tail['lastTs'][:16].replace('T', ' ')}); the Codex turns follow it")
    if args.dry_run:
        print()
        return

    cards = desktop_register.touch(res["sessionId"], ms_of(res["last"]),
                                   res["stats"]["user"])
    print(f"    backup: {res['backup']}")
    print(f"    desktop card(s) updated: {len(cards)}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = ledger.setdefault(tid, {
        "claudeSessionId": res["sessionId"], "card": str(cards[0]) if cards else None,
        "cwd": tail["cwd"], "title": title, "importedAt": now, "kind": "round-trip",
        "sourceRollout": str(s["path"]), "mode": mode})
    entry.update({"transcript": str(target), "syncedAt": now,
                  "sourceSha256": res["digest"], "sourceBytes": res["end"],
                  "sourceMtime": int(s["path"].stat().st_mtime),
                  "toolVersion": TOOL_VERSION})
    save_ledger(ledger)
    print()


def first_assistant_model(head: str) -> str | None:
    """Model of the first assistant record. Ours for a transcript this tool wrote; a
    real Claude model for a native session, even one Codex turns were appended to."""
    for line in head.splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") == "assistant":
            return (r.get("message") or {}).get("model")
    return None


def cmd_uninstall(args):
    """Remove what this tool created — transcripts and desktop cards — and nothing else."""
    ledger = load_ledger()
    entries = list(ledger.items())

    if args.scan:
        # Fallback for imports whose ledger entry is gone. Every transcript we write
        # ends with a custom-title record carrying IMPORT_MARK — present even in a
        # session with no assistant turns, unlike the IMPORT_MODEL stamp.
        known = {v["transcript"] for v in ledger.values()}
        for p in sorted(Path(args.claude_dir).glob("*/*.jsonl")):
            if str(p) in known:
                continue
            try:
                with p.open("rb") as fh:
                    fh.seek(max(0, p.stat().st_size - 8192))
                    tail = fh.read().decode("utf-8", "replace")
                    fh.seek(0)
                    head = fh.read(200_000).decode("utf-8", "replace")
            except Exception:
                continue
            title = None
            for line in tail.splitlines():
                if IMPORT_MARK in line and '"custom-title"' in line:
                    try:
                        title = json.loads(line).get("customTitle")
                    except Exception:
                        title = "(found by scan)"
                    break
            if title is None and first_assistant_model(head) == IMPORT_MODEL:
                title = "(found by scan)"
            if title is not None:
                entries.append((f"scan:{p.stem}", {"transcript": str(p), "card": None,
                                                   "title": title}))

    if not entries:
        print("Nothing to remove.")
        return

    cards = {}
    reg = desktop_register.registry_dir()
    if reg:
        for f in reg.glob("local_*.json"):
            try:
                cards[json.loads(f.read_text()).get("cliSessionId")] = f
            except Exception:
                pass

    print(f"{'Would remove' if args.dry_run else 'Removing'} {len(entries)} import(s):\n")
    for key, v in entries:
        t = Path(v["transcript"])
        if v.get("kind") == "round-trip":
            # A Claude session of the user's own that Codex turns were appended to.
            # It and its card stay; only our record of the sync goes.
            print(f"  {v.get('title', '')[:56]}")
            print(f"    kept:       {t}  (native Claude session; the appended Codex "
                  f"turns stay, backups in {BACKUPS})")
            if not args.dry_run:
                ledger.pop(key, None)
            continue
        card = Path(v["card"]) if v.get("card") else cards.get(t.stem)
        print(f"  {v.get('title', '')[:56]}")
        print(f"    transcript: {t}{'' if t.exists() else '  (already gone)'}")
        print(f"    card:       {card if card else '(none)'}")
        if not args.dry_run:
            t.unlink(missing_ok=True)
            if card and card.exists():
                card.unlink()
            ledger.pop(key, None)
    if args.orphan_cards and reg:
        dead = []
        for f in reg.glob("local_*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            cli = d.get("cliSessionId")
            if cli and not list(Path(args.claude_dir).glob(f"*/{cli}.jsonl")):
                dead.append((f, d.get("title", "")))
        if dead:
            print(f"\n{'Would remove' if args.dry_run else 'Removing'} "
                  f"{len(dead)} card(s) with no transcript:")
            for f, t in dead:
                print(f"    {t[:58]}  ({f.name})")
                if not args.dry_run:
                    f.unlink()

    if not args.dry_run:
        save_ledger(ledger)
        print("\nRelaunch Claude desktop for the sidebar to catch up.")


def dead_cards(claude_dir: str) -> list[tuple[Path, str]]:
    """Desktop cards whose transcript is gone — the sidebar rows that open on
    'session not found on disk'."""
    reg = desktop_register.registry_dir()
    out = []
    if not reg:
        return out
    for f in sorted(reg.glob("local_*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        cli = d.get("cliSessionId")
        if cli and not list(Path(claude_dir).glob(f"*/{cli}.jsonl")):
            out.append((f, d.get("title", "")))
    return out


def cmd_repair(args):
    """Clear dead sidebar rows and re-import anything whose transcript went missing.

    Claude desktop keeps the session list in memory and writes it back, so cards
    deleted while it is running can reappear. Quit it before repairing.
    """
    if desktop_register.desktop_running():
        print("! Claude desktop is running. It rewrites cards from memory, so ones "
              "removed now\n  can come back. Quit it, run this again, then reopen.\n")

    dead = dead_cards(args.claude_dir)
    if dead:
        print(f"{'Would remove' if args.dry_run else 'Removing'} {len(dead)} dead "
              f"sidebar row(s):")
        for f, t in dead:
            print(f"    {t[:60]}")
            if not args.dry_run:
                f.unlink()
        print()
    else:
        print("No dead sidebar rows.\n")

    ledger = load_ledger()
    missing = {tid: v for tid, v in ledger.items() if not Path(v["transcript"]).exists()}
    if missing:
        print(f"{len(missing)} import(s) lost their transcript:")
        for tid, v in missing.items():
            print(f"    {v['title'][:60]}")
        print("\n  Re-import them with:  import --all --force"
              if not args.dry_run else "")
    else:
        print("All recorded imports still have their transcript.")


def cmd_status(args):
    """Show what has been imported, and whether both sides still line up."""
    ledger = load_ledger()
    if not ledger:
        print(f"No imports recorded ({LEDGER} is empty or missing).")
        return
    reg = desktop_register.registry_dir()
    cards = {}
    if reg:
        for f in reg.glob("local_*.json"):
            try:
                cards[json.loads(f.read_text()).get("cliSessionId")] = f
            except Exception:
                pass

    print(f"{len(ledger)} import(s) recorded in {LEDGER}\n")
    issues = 0
    for tid, v in sorted(ledger.items(), key=lambda kv: kv[1]["importedAt"]):
        t = Path(v["transcript"])
        src = Path(v.get("sourceRollout", ""))
        card = Path(v["card"]) if v.get("card") else cards.get(t.stem)
        notes = []
        if not t.exists():
            notes.append("transcript missing")
        if card is None or not card.exists():
            notes.append("no desktop card")
        if not src.exists():
            notes.append("source rollout gone")
        elif v.get("sourceMtime") and int(src.stat().st_mtime) != v["sourceMtime"]:
            notes.append("source rollout changed since import")
        issues += bool(notes)
        print(f"  {'!' if notes else ' '} {v['title'][:56]}")
        print(f"      codex {tid}  ->  claude {v['claudeSessionId']}")
        print(f"      {v['importedAt'][:19]}  mode={v.get('mode','?')}  cwd={v['cwd']}")
        if notes:
            print(f"      ! {'; '.join(notes)}")
    print(f"\n{len(ledger) - issues} healthy, {issues} needing attention.")


def account_label(reg: Path) -> str:
    return f"{reg.parent.name[:8]}/{reg.name[:8]}"


def resolve_registry(spec: str | None, default: Path | None, role: str) -> Path | None:
    regs = desktop_register.registries()
    if spec:
        hits = [r for r in regs if f"{r.parent.name}/{r.name}".startswith(spec)
                or r.parent.name.startswith(spec)]
        if len(hits) != 1:
            sys.exit(f"--{role} {spec!r} matches {len(hits)} registries; pass "
                     f"<account>/<workspace> (prefixes are fine)")
        return hits[0]
    return default


def cmd_move_account(args):
    """Bring desktop session cards from another account into the active one.

    Transcripts live in ~/.claude/projects/ and belong to no account, so they are
    already usable from any login on this machine. What is per-account is the desktop
    app's card registry — which is why signing into another account empties the
    sidebar. This copies the cards across, so the same conversations appear (and can
    be continued) under the new account.
    """
    dst = resolve_registry(args.to, desktop_register.registry_dir(), "to")
    if dst is None:
        sys.exit("No destination registry found. Open Claude desktop signed into the "
                 "target account and start one session there first, then retry.")
    others = [r for r in desktop_register.registries() if r != dst]
    src = resolve_registry(args.from_, others[0] if len(others) == 1 else None, "from")
    if src is None:
        print("Several source registries; pick one with --from:")
        for r in others:
            print(f"    {r.parent.name}/{r.name}  {len(list(r.glob('local_*.json')))} "
                  f"card(s)  {account_label(r)}")
        return
    if src == dst:
        sys.exit("Source and destination are the same registry.")

    template = desktop_register.template_entry(dst)
    have = desktop_register.cards_by_cli_id(dst)
    print(f"from  {account_label(src)}\nto    {account_label(dst)}\n")

    if not args.dry_run and desktop_register.desktop_running():
        print("! Claude desktop is running and caches the session list. Quit and "
              "reopen it\n  afterwards; if any row is missing, run this again — it "
              "skips what is already there.\n")

    ledger = load_ledger()
    by_claude_id = {v["claudeSessionId"]: tid for tid, v in ledger.items()}
    moved = skipped = missing = 0
    for f in sorted(src.glob("local_*.json"), key=lambda p: p.stat().st_mtime):
        card = json.loads(f.read_text())
        cli = card.get("cliSessionId")
        title = card.get("title", "")[:60]
        if cli in have:
            skipped += 1
            print(f"  skip  {title}  (already in destination)")
            continue
        if not cli or not list(Path(args.claude_dir).glob(f"*/{cli}.jsonl")):
            missing += 1
            print(f"  skip  {title}  (transcript not on disk)")
            continue
        entry = desktop_register.transfer(card, template)
        target = dst / f"{entry['sessionId']}.json"
        print(f"  {'would copy' if args.dry_run else 'copy'}  {title}")
        moved += 1
        if args.dry_run:
            continue
        target.write_text(json.dumps(entry, indent=2))
        if args.remove_source:
            f.unlink()
        if cli in by_claude_id:
            ledger[by_claude_id[cli]]["card"] = str(target)

    if not args.dry_run and moved:
        save_ledger(ledger)
    verb = "would copy" if args.dry_run else ("moved" if args.remove_source else "copied")
    print(f"\n{verb} {moved}; {skipped} already there; "
          f"{missing} without a transcript")
    if not args.dry_run and moved:
        print("Quit and reopen Claude desktop for the sidebar to pick them up.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codex-dir", default=str(CODEX_SESSIONS))
    ap.add_argument("--claude-dir", default=str(CLAUDE_PROJECTS))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the Codex sessions available to import")

    imp = sub.add_parser("import", help="convert Codex sessions into Claude Code transcripts")
    imp.add_argument("--session", help="Codex session id (prefix match)")
    imp.add_argument("--all", action="store_true", help="import every session found")
    imp.add_argument("--mode", choices=["archive", "faithful"], default="archive",
                     help="archive: tool traffic flattened to readable text, always "
                          "safe to resume. faithful: real tool_use/tool_result pairs, "
                          "richer UI, replays foreign tool names to the API.")
    imp.add_argument("--keep-developer", action="store_true",
                     help="keep Codex developer/system injections as background messages")
    imp.add_argument("--include-round-trips", action="store_true",
                     help="import Codex threads that Codex itself imported from Claude "
                          "Code as new sessions (default: append their Codex-side "
                          "turns to the Claude session they came from)")
    imp.add_argument("--force", action="store_true",
                     help="re-import sessions this tool has already imported, as a "
                          "fresh copy, instead of appending their newer Codex turns")
    imp.add_argument("--no-sync", action="store_true",
                     help="skip threads already in Claude instead of appending the "
                          "Codex turns they gained since")
    imp.add_argument("--desktop", action="store_true",
                     help="also register each session with the Claude desktop app so "
                          "it appears in the sidebar (relaunch the app afterwards)")
    imp.add_argument("--title-prefix", default="",
                     help="prefix every imported title, e.g. '[codex] ' (default: none)")
    imp.add_argument("--no-group-scratch", action="store_true",
                     help="keep Codex Desktop's per-thread scratch dirs as separate "
                          "sidebar folders instead of collapsing them")
    imp.add_argument("--no-banner", action="store_true",
                     help="omit the background provenance banner at the top of each "
                          "imported thread (metadata is still recorded)")
    imp.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="show what has been imported and whether it is intact")

    rep = sub.add_parser("repair",
                         help="clear sidebar rows whose transcript is gone "
                              "('session not found on disk') and report lost imports")
    rep.add_argument("--dry-run", action="store_true")

    un = sub.add_parser("uninstall", help="remove transcripts and cards this tool created")
    un.add_argument("--scan", action="store_true",
                    help="also find imports missing from the ledger, by fingerprint")
    un.add_argument("--orphan-cards", action="store_true",
                    help="also remove desktop cards whose transcript no longer exists "
                         "(dead sidebar rows, from any source)")
    un.add_argument("--dry-run", action="store_true")

    mv = sub.add_parser("move-account",
                        help="copy desktop sidebar sessions from another account (e.g. "
                             "one you signed out of) into the active one")
    mv.add_argument("--from", dest="from_", metavar="ACCOUNT[/WORKSPACE]",
                    help="source registry (default: the only other one with sessions)")
    mv.add_argument("--to", metavar="ACCOUNT[/WORKSPACE]",
                    help="destination registry (default: the account signed in now)")
    mv.add_argument("--remove-source", action="store_true",
                    help="delete the source cards after copying (default: keep them, "
                         "so the sessions still show if you sign back in)")
    mv.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    {"list": cmd_list, "import": cmd_import, "status": cmd_status,
     "repair": cmd_repair, "uninstall": cmd_uninstall,
     "move-account": cmd_move_account}[args.cmd](args)


if __name__ == "__main__":
    main()
