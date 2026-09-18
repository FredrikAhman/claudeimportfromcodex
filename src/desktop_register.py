#!/usr/bin/env python3
"""
desktop_register — make an imported transcript visible in the Claude desktop app.

Writing `~/.claude/projects/<slug>/<uuid>.jsonl` is enough for the CLI: `claude -r`
scans that directory directly. The desktop app does not. It lists sessions from its
own registry:

    ~/Library/Application Support/Claude/claude-code-sessions/<account>/<workspace>/local_<uuid>.json

Each of those is a session card — title, cwd, model, timestamps — joined to the
transcript on disk by its `cliSessionId` field. No registry entry, no row in the
sidebar, however valid the transcript is.

This writes that entry. It clones the most recent real entry in the same account
so the heavy fields (MCP server config, promptAppendSnapshot, toolSurfaceSnapshot)
stay consistent with what the app expects, then overrides the identity fields.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

SUPPORT = Path.home() / "Library" / "Application Support" / "Claude"
SESSIONS = SUPPORT / "claude-code-sessions"
CONFIG = SUPPORT / "config.json"

# Fields that describe one specific past session and must not be inherited
# from the template entry.
DROP = {"error", "errorAt", "priorErrorMark", "worktreePath", "worktreeName",
        "branch", "sourceBranch", "spawnedFrom", "bridgeSessionIds",
        "scratchPromptRecents", "envScopeId", "chromeTabGroupId",
        "pendingSystemReminder", "resolvedBackgroundTaskSuggestions",
        "writtenBranches", "latestUserFrameAt", "autoChosenInApp", "titleTurn"}


def active_account() -> str | None:
    try:
        return json.loads(CONFIG.read_text()).get("lastKnownAccountUuid")
    except Exception:
        return None


def registry_dir(account: str | None = None) -> Path | None:
    """The <account>/<workspace> directory the app is currently writing session cards to."""
    if not SESSIONS.is_dir():
        return None
    account = account or active_account()
    candidates = []
    for acct_dir in SESSIONS.iterdir():
        if not acct_dir.is_dir() or (account and acct_dir.name != account):
            continue
        for ws_dir in acct_dir.iterdir():
            if ws_dir.is_dir() and list(ws_dir.glob("local_*.json")):
                newest = max(p.stat().st_mtime for p in ws_dir.glob("local_*.json"))
                candidates.append((newest, ws_dir))
    if not candidates and account:
        return registry_dir(account="")   # fall back to any account
    if not candidates:
        return None
    return max(candidates)[1]


def template_entry(reg: Path) -> dict | None:
    entries = sorted(reg.glob("local_*.json"), key=lambda p: p.stat().st_mtime)
    if not entries:
        return None
    return json.loads(entries[-1].read_text())


def register(cli_session_id: str, cwd: str, title: str, created_ms: int | None = None,
             turns: int = 0, reg: Path | None = None, dry_run: bool = False,
             provenance: dict | None = None) -> Path | None:
    """Write a desktop session card pointing at an imported transcript."""
    reg = reg or registry_dir()
    if reg is None:
        return None
    tmpl = template_entry(reg)
    if tmpl is None:
        return None

    entry = {k: v for k, v in tmpl.items() if k not in DROP}
    now = int(time.time() * 1000)
    created = created_ms or now
    local_id = "local_" + str(uuid.uuid4())

    entry.update({
        "sessionId": local_id,
        "cliSessionId": cli_session_id,
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": created,
        "lastActivityAt": created,
        # Sort position in the sidebar. Keep it at the session's own age rather than
        # now, so imports land chronologically instead of jumping to the top.
        "lastFocusedAt": created,
        "title": title,
        "titleSource": "custom",
        "isArchived": False,
        "completedTurns": turns,
    })

    if provenance:
        # Best effort: the app owns this file and may drop unknown keys when it
        # rewrites the card. The transcript and the ledger are the durable records.
        entry["codexImport"] = provenance

    target = reg / f"{local_id}.json"
    if not dry_run:
        target.write_text(json.dumps(entry, indent=2))
    return target


# Fields that belong to the account a card was written under rather than to the
# conversation: the connector list, tool surface and prompt snapshots the app took
# when that account was signed in, plus transient runtime state. A card moved to
# another account takes these from that account's own newest card instead.
ACCOUNT_BOUND = DROP - {"worktreePath", "worktreeName", "branch", "sourceBranch",
                        "writtenBranches", "latestUserFrameAt", "titleTurn",
                        "scratchPromptRecents"} | {
    "remoteMcpServersConfig", "enabledMcpTools", "promptAppendSnapshot",
    "toolSurfaceSnapshot", "remoteControlAutoEligible"}


def registries() -> list[Path]:
    """Every <account>/<workspace> directory holding at least one session card."""
    if not SESSIONS.is_dir():
        return []
    return sorted(ws for acct in SESSIONS.iterdir() if acct.is_dir()
                  for ws in acct.iterdir()
                  if ws.is_dir() and any(ws.glob("local_*.json")))


def cards_by_cli_id(reg: Path) -> dict[str, Path]:
    out = {}
    for f in reg.glob("local_*.json"):
        try:
            out[json.loads(f.read_text()).get("cliSessionId")] = f
        except Exception:
            pass
    return out


def transfer(src_card: dict, template: dict) -> dict:
    """A card for `src_card`'s conversation, fit to live in `template`'s account.

    The conversation's own fields (title, cwd, timestamps, model, provenance) come
    from the source card; everything account-bound comes from the template.
    """
    entry = {k: v for k, v in template.items() if k not in DROP}
    entry.update({k: v for k, v in src_card.items() if k not in ACCOUNT_BOUND})
    entry["sessionId"] = "local_" + str(uuid.uuid4())
    return entry


def touch(cli_session_id: str, last_ms: int | None, turns: int) -> list[Path]:
    """Record new activity on every card for a session, in every account.

    Used after Codex turns are appended to a transcript, so the row sorts by when the
    thread was last worked on. Best effort, like everything written to these files.
    """
    out = []
    for reg in registries():
        f = cards_by_cli_id(reg).get(cli_session_id)
        if f is None:
            continue
        card = json.loads(f.read_text())
        if last_ms:
            for k in ("lastActivityAt", "lastFocusedAt"):
                card[k] = max(card.get(k) or 0, last_ms)
        card["completedTurns"] = (card.get("completedTurns") or 0) + turns
        f.write_text(json.dumps(card, indent=2))
        out.append(f)
    return out


def desktop_running() -> bool:
    return os.system("pgrep -qf '/Applications/Claude.app/Contents/MacOS/Claude'") == 0


if __name__ == "__main__":
    reg = registry_dir()
    print(f"registry dir: {reg}")
    print(f"active account: {active_account()}")
    if reg:
        print(f"existing cards: {len(list(reg.glob('local_*.json')))}")
    print(f"desktop running: {desktop_running()}")
