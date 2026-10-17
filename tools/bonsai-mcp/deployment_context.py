# ---
# id: 01M3BT3KSC8TTPVPSFYHSXSA6R
# ---
"""
Deployment context for the boot payload (2026-09).

Split into its own module rather than edited into tools/bonsai-mcp/server.py directly: that
file is ~3,467 lines / 146KB, and neither MCP write primitive available to this session can
safely amend it in place. bonsai_write_file only supports a full-content overwrite (no patch
mode — see agents/<slug>/memory/working-state.md's own drift-surfaces note, "bonsai_read_file/
bonsai_write_file have no patch mode — a large file (server.py, ...)"), and bonsai_read_file
has no way to page a file this size back into a tool result to reconstruct one safely (it
either returns the whole file or spills to a cache file this session's tools cannot reliably
re-chunk). bonsai_append_file's append-only primitive can't substitute either: server.py ends
with `if __name__ == "__main__": ... mcp.run()`, and the host's service definitions
confirm the server is actually launched by running this file
directly as a script (`python tools/bonsai-mcp/server.py --transport http`) — so anything
appended after that guard would sit dead, never executed, in the running production server.

This module lands the reviewed, correct logic now, safely (new file, nothing existing at
risk), so the remaining server.py change is the minimum possible: one import line plus the
three call-site edits recorded on its card — small enough for a session with a verified way to
edit that file in place (or
the gardener, directly) to apply in minutes without re-deriving any of this.

See the card's own root-cause section for the two wrong statements this closes (an agent's — also
recorded in a nest-wiring runbook).

A later change (2026-09-14) extended this module with live git HEAD/branch reporting, sourced from
the server process's own checkout (git rev-parse against repo_root) rather than any
client-side assumption — used both in deployment_context_text's boot-payload line and in
host_and_head's compact per-write attribution tag.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path


def _git_head_short(repo_root: Path) -> str:
    """
    '<short-sha> (<branch>)' for repo_root's live checkout — sourced from git itself, run
    against repo_root, never a client-side assumption echoed back. Fail-safe: any git failure
    (detached worktree, missing binary, timeout) degrades to 'unknown' rather than raising,
    matching this module's own fail-safe contract.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not sha:
            return "unknown"
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return f"{sha} ({branch or 'unknown'})"
    except Exception:
        return "unknown"


def host_and_head(repo_root: Path) -> str:
    """
    Compact '<hostname>@<short-sha>(<branch>)' tag for per-write attribution — the
    same server-side ground truth (hostname, live git HEAD) as deployment_context_text's own
    host/HEAD line, reused rather than re-derived so the two can never drift against each
    other. Reporting only: callers append this to a message, it never gates or halts a write.
    """
    try:
        host = os.uname().nodename
    except Exception:
        host = "unknown"
    return f"{host}@{_git_head_short(repo_root)}"


def deployment_context_text(repo_root: Path) -> str:
    """
    Best-effort, fail-safe description of where the MCP server process is running, what
    commit its checkout is sitting at, and which Buzz relay it currently answers to, so an
    agent never has to guess or over-credit its environment. Never raises: any lookup failure
    degrades a field to 'unknown' rather than blocking a boot.

    Relay hosted/self-hosted truth is read from tools/buzz-nest/buzz-identity-map.json's own
    top-level `relay` (the original Block-hosted beta relay, decommissioned 2026-08-31) and
    `self_hosted_relay` (the active relay since 2026-08-26) blocks — the same file already
    kept in sync by hand as the traceability record for the Buzz excursion; this reads it
    rather than hand-encoding a relay URL that would drift the moment it changes again.
    """
    try:
        host = os.uname().nodename
    except Exception:
        host = "unknown"

    head = _git_head_short(repo_root)

    relay_kind = "unknown"
    relay_url = ""
    with contextlib.suppress(Exception):  # no identity map, or a broken one: the relay line stays "unknown"
        _map = json.loads((repo_root / "tools" / "buzz-nest" / "buzz-identity-map.json").read_text(encoding="utf-8"))
        _hosted = _map.get("relay") or {}
        _self_hosted = _map.get("self_hosted_relay") or {}
        if _hosted.get("status") == "decommissioned" and _self_hosted.get("url"):
            relay_kind, relay_url = "self-hosted", _self_hosted["url"]
        elif _hosted.get("url") and _hosted.get("status") != "decommissioned":
            relay_kind, relay_url = _hosted.get("kind", "hosted"), _hosted["url"]
        elif _self_hosted.get("url"):
            relay_kind, relay_url = "self-hosted", _self_hosted["url"]

    line = f"host: {host} · HEAD: {head}\nbuzz_relay: {relay_kind}"
    if relay_url:
        line += f" ({relay_url})"
    return line


def newest_folder_evidence_reason(root: Path) -> str | None:
    """
    Short, human-readable reason the folder-evidence staleness check (server.py's own
    _newest_folder_evidence) could not run at all — e.g. the agent's registry-declared root
    does not resolve on disk — or None when the folder exists and the ordinary check should
    proceed exactly as before.

    Kept as a thin, standalone predicate rather than a reimplementation of the scan itself:
    the mtime walk, .bak exclusion, and single-path exclusion stay in server.py's own
    _newest_folder_evidence, unchanged, so that logic lives in exactly one place. This
    function only answers the question _newest_folder_evidence's plain None return couldn't:
    "did the check even run?"

    The root cause this module was written for: an agent whose registry-declared root does not resolve (the
    named example is a nested *-shin like a sub-agent kin, whose real path is
    agents/<parent>/sub-agents/<slug>, not a flat agents/<slug> guess) got the exact same
    silent None as an agent whose folder is simply empty of new evidence — so the stale-boot
    banner could never fire for the broken-path case, and the agent never knew its own
    freshness check had quietly failed to run.
    """
    if not root.exists():
        return f"agent root does not resolve ({root})"
    return None
