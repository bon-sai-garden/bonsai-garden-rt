#!/usr/bin/env python3
# ---
# id: 01M2D6E13QQ1ZKPA05YGTZSYBK
# title: Server
# type: script
# tended-by: baisai
# created: 2026-09-13
# updated: 2026-10-17
# copyright: © 2026 Bonsai Garden · bon-sai.garden
# ---
"""
bonsai_mcp — Gated file access MCP for the bonsai-garden repo.

Exposes read/write/search/git/metadata tools scoped to REPO_ROOT.
All paths are relative to $BONSAI_GARDEN; traversal outside the repo
is rejected at every tool boundary.

Transport: stdio (default). Switch to HTTP with --transport http.

Usage:
    python server.py                         # stdio
    python server.py --transport http        # HTTP on 127.0.0.1:8000
    python server.py --transport http --port 9000

Env:
    BONSAI_GARDEN   Absolute path to the repo root (required).
                    Falls back to three levels above this file if unset.
    BONSAI_CALLER   Dedicated-daemon identity override (default: unidentified).
                    Load-bearing, not informational: _require_identified()
                    blocks the literal string "unidentified" from all reads
                    and writes, and owner-lock keys on
                    this same resolved identity. Leave unset for interactive
                    installs — a session identifies itself via
                    bonsai_session_start(slug) instead. (2026-09-05)

Tools (14):
    bonsai_read_file          Read a file; parses frontmatter for .md files
    bonsai_write_file         Write / overwrite a file; auto-re-seals (v2 sidecar) a sealed .md
                              (constitution files — about-me.md, SKILL.md — get a stale warning instead)
    bonsai_append_file        Append to an existing file without rewriting it — safe for
                              large working-state/journal files; same auto-re-sign rules
    bonsai_delete_file        Tombstone (.bak) or hard-delete a file
    bonsai_move_file          Move / rename within the repo
    bonsai_list_directory     List a directory (optionally recursive)
    bonsai_search             Grep-style search (rg preferred, grep fallback)
    bonsai_git_status         git status --short
    bonsai_git_log            git log with optional path / since filters
    bonsai_git_diff           git diff with optional ref / path
    bonsai_validate_metadata  Run tools/validate-metadata.py
    bonsai_find_by_id         Find a file by its ULID frontmatter id
    bonsai_session_start      Load handover + working-state in one call
    bonsai_get_shared_context Load workshop-context + codex + routines in one call

Schema shape:
    Each tool exposes its arguments as top-level JSON-schema properties
    (flat — no outer `params:` wrapper). Callers pass arguments by name,
    e.g. {"path": "CLAUDE.md"}, not {"params": {"path": "CLAUDE.md"}}.
"""

import argparse
import contextlib
import datetime as _dt
import difflib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile as _tempfile
import time as _time_module
import uuid
import weakref
from pathlib import Path, PurePosixPath
from typing import Annotated

import anyio
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Fail-safe, not fall-back (AGENTS.md → "Finding the garden"; the gardener's directive,
# 2026-09-22). The repo root comes from $BONSAI_GARDEN or this server refuses
# to start. The fallback this replaces — three directory levels up from this
# file — served whichever tree happened to hold the running copy: a worktree,
# a mirror, a stale checkout, each answering every read with a straight face.
# Every real spawn path (~/.claude.json, claude_desktop_config.json, the PM2
# ecosystem) already sets the variable; only a bare `python3 server.py` did not,
# and that is exactly the launch that should stop and say so.
_env_root = os.environ.get("BONSAI_GARDEN", "")
# A pot launches its own server from its own folder (.mcp.json: `--garden .`). The garden is then named on the command
# line, explicitly, by the config the pot's session reads: still no fallback, only a second place to say it. The flag
# wins over the environment so a user-level $BONSAI_GARDEN (the origin's) can never be served inside a pot by accident.
for _i, _a in enumerate(sys.argv):
    if _a == "--garden" and _i + 1 < len(sys.argv):
        _env_root = os.path.abspath(os.path.expanduser(sys.argv[_i + 1]))
    elif _a.startswith("--garden="):
        _env_root = os.path.abspath(os.path.expanduser(_a.split("=", 1)[1]))
if not _env_root:
    sys.exit(
        "bonsai-mcp: refusing to start — $BONSAI_GARDEN is not set.\n"
        "  Set it in the config that spawns this server (~/.claude.json "
        "mcpServers.bonsai-garden.env · claude_desktop_config.json · the PM2 "
        "ecosystem) to the garden repo's root.\n"
        "  There is deliberately no fallback to this file's own location."
    )
REPO_ROOT: Path = Path(_env_root).resolve()


# The tree this daemon RUNS FROM is not the garden it SERVES (Bonsai's rehearsal, gap 56, 2026-10-11). REPO_ROOT is
# the served garden: its registry, keys, memory. The daemon's own tooling, the verifier and signer (tools/sign.py)
# and the modules the exec gate checks, belong to the tree this file sits in. A new garden's pot has no tools/, so
# a gate or a write that reached sign.py through the served root could never serve it. Found by marker, never by
# counting directory levels, and never from $BONSAI_GARDEN: this is not a fallback for the served garden (there is
# none), it is where the daemon's own code lives. Today it is the mono checkout; when the runtime serves, the runtime.
def _find_server_root() -> Path:
    for d in Path(__file__).resolve().parents:
        # mono (tools/sign.py), a runtime checkout (tools/rt/rt-slice.json) or an installed rt (RT-MANIFEST.json): the tree the daemon runs from
        if (
            (d / "tools" / "sign.py").is_file()
            or (d / "tools" / "rt" / "rt-slice.json").is_file()
            or (d / "RT-MANIFEST.json").is_file()
        ):
            return d
    sys.exit(
        f"bonsai-mcp: refusing to start — no mono (tools/sign.py), runtime (tools/rt/rt-slice.json) or rt (RT-MANIFEST.json) above {Path(__file__).resolve()}: the daemon cannot find its own tree"
    )


SERVER_ROOT: Path = _find_server_root()


def _origin_sign_py() -> Path | None:
    """mono's tools/sign.py: in the daemon's own tree when it runs from mono, else in the served garden (the origin's own, when the
    daemon is hosted by runtime). None for a pot, which has neither."""
    for r in (SERVER_ROOT, REPO_ROOT):
        if (r / "tools" / "sign.py").is_file():
            return r / "tools" / "sign.py"
    return None


def _is_pot() -> bool:
    """The served garden is a POT: not where the daemon runs from, and not a garden that carries the origin's own signer. Decided by
    what the garden holds, never by where the daemon sits (a runtime-hosted daemon serves the origin without being it)."""
    return REPO_ROOT.resolve() != SERVER_ROOT.resolve() and not (REPO_ROOT / "tools" / "sign.py").is_file()


# The two sync copies are never the repo (AGENTS.md: "Never write to these").
_MIRROR_MARKERS = ("/Library/Mobile Documents/", "/Library/CloudStorage/")
if any(m in str(REPO_ROOT) + "/" for m in _MIRROR_MARKERS):
    sys.exit(f"bonsai-mcp: refusing to start — $BONSAI_GARDEN points at a sync mirror, not the repo: {REPO_ROOT}")
# Resolve roots by marker, never by directory levels (AGENTS.md).
if not (REPO_ROOT / "agents" / "registry.json").is_file():
    sys.exit(
        f"bonsai-mcp: refusing to start — $BONSAI_GARDEN has no agents/registry.json "
        f"marker, so it is not a garden root: {REPO_ROOT}"
    )

# Immutable host default caller. Read from BONSAI_CALLER once at import and
# NEVER reassigned (fix, 2026-08-14). A missing per-session identity
# now falls back to this neutral host default instead of a mutable process
# global — so a lookup miss can never surface another session's leaked slug.
# The prior mutable `CALLER` global (which bonsai_session_start reassigned on
# every activation) was the root cause of both the dispatch-flip and the
# no-dispatch stale-identity defects; it has been removed in favour of this.
_DEFAULT_CALLER: str = os.environ.get("BONSAI_CALLER", "unidentified")

# Per-connection caller identity (caller-topology fix, 2026-07-17).
#
# The bare module global above is only safe if exactly one logical MCP
# connection is ever alive against this process at a time. That assumption
# turned out to be false in the wild: a shared/long-lived bonsai-mcp process
# (Cowork's per-app-session instance, or the streamable-http daemon) can end
# up serving more than one live conversation/connection concurrently, so a
# `global CALLER = slug` mutation from one session's bonsai_session_start()
# was observed clobbering — and being clobbered by — other concurrently
# active sessions' identities. See
# the handover of 2026-07-15 (reopened 2026-07-17) for the reproduction.
#
# Fix: key the identity by the live MCP ServerSession object instead of the
# process. Each real client connection gets its own ServerSession instance
# (confirmed against the installed mcp SDK: Context.session returns
# request_context.session, a plain object with default identity hashing —
# no __eq__/__hash__ override), so this dict is automatically scoped
# correctly whether the process serves one connection (plain stdio) or
# several (a shared/http-transport daemon). WeakKeyDictionary means a
# closed connection's entry is garbage-collected with the session object —
# no explicit cleanup, no unbounded growth on a long-lived daemon.
_CALLER_BY_SESSION: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

# Session-token registry (Layer 1, 2026-08-28).
# Maps UUID tokens → (slug, expiry_timestamp). Tokens survive SSE connection
# drops where ctx.session changes per-request — the root cause of caller-drift
# on streamable-http transports. TTL: 8 hours.
_SESSION_TOKENS: dict = {}  # str → (str, float)
_SESSION_TOKEN_TTL: float = 28800.0  # 8 hours


def _effective_caller(ctx: Context | None = None) -> str:
    """Resolve the caller identity for THIS call.

    Prefers the identity registered for ctx's own MCP session (set by that
    session's own bonsai_session_start(slug) call, keyed by the session
    object so concurrent sessions can never see each other's identity);
    falls back to the immutable host default _DEFAULT_CALLER (env
    BONSAI_CALLER, or 'unidentified') when ctx is absent or its session was never
    registered. Never raises - a missing or unusual ctx just falls back to
    the neutral host default (never another session's leaked slug).
    """
    if ctx is not None:
        with contextlib.suppress(AttributeError, TypeError):  # a context without a usable session identifies no one
            session = ctx.session
            cached = _CALLER_BY_SESSION.get(session)
            if cached:
                return cached
    return _DEFAULT_CALLER


def _resolve_caller_ex(
    as_caller: str = "", ctx: Context | None = None, session_token: str = "", caller_pubkey: str = ""
) -> "tuple[str, bool]":
    """Resolve the caller identity for a tool invocation, and report whether
    resolution fell all the way through to the immutable host default.

    Same five-layer precedence as _resolve_caller() below (now a thin
    wrapper over this function) — see its docstring for the full
    layer-by-layer account. Returns (caller, defaulted):
      - defaulted is False whenever layers 1-4 answered — an explicit
        as_caller, a live session_token, a resolved caller_pubkey, or a hit
        in the per-session cache (_CALLER_BY_SESSION, set by this session's
        own bonsai_session_start call). None of these are a guess.
      - defaulted is True only when none of those answered and resolution
        fell all the way to the immutable host default (_DEFAULT_CALLER —
        env BONSAI_CALLER, or 'unidentified' when unset, which
        _require_identified already blocks outright). This is the one case
        that IS a guess — 's fix is making the tool say so.

    Deliberately re-implements the layer-4 cache lookup inline rather than
    calling _effective_caller(ctx) (which only returns the resolved slug,
    not which of its two branches answered) — _effective_caller() itself is
    left untouched because the caller-identity regression probes call it
    directly as a regression probe; changing its signature would break them.
    Never raises; any failure falls through to the next level.
    (2026-09-05 — the return-string "(defaulted, not asserted)"
    marker in bonsai_write_file/append_file/delete_file/move_file is powered
    by the second element of this tuple.)
    """
    with contextlib.suppress(
        OSError, ValueError, AttributeError
    ):  # an unreadable registry names no one; the caller stays unidentified
        slug = (as_caller or "").strip().lower()
        if slug and _registry_entry(slug):
            return slug, False
    # Session token (Layer 1) — transport-independent identity.
    if session_token:
        with contextlib.suppress(TypeError, ValueError):  # a malformed token entry identifies no one
            _tok = session_token.strip()
            _entry = _SESSION_TOKENS.get(_tok)
            if _entry:
                _tok_slug, _tok_expiry = _entry
                if _time_module.time() < _tok_expiry:
                    return _tok_slug, False
                _SESSION_TOKENS.pop(_tok, None)  # prune expired
    # Buzz envelope pubkey (Layer 2) — stateless, survives multi-process MCP.
    if caller_pubkey:
        with contextlib.suppress(
            OSError, ValueError, AttributeError, TypeError
        ):  # an unknown or unreadable identity map names no one
            _pk_slug = _slug_from_buzz_pubkey(caller_pubkey)
            if _pk_slug and _registry_entry(_pk_slug):
                return _pk_slug, False
    # Per-session cache (Layer 4) — same lookup _effective_caller(ctx) does,
    # inlined so a cache hit (not defaulted) is distinguishable from falling
    # all the way through to the immutable host default (defaulted).
    if ctx is not None:
        with contextlib.suppress(AttributeError, TypeError):  # a context without a usable session identifies no one
            session = ctx.session
            cached = _CALLER_BY_SESSION.get(session)
            if cached:
                return cached, False
    return _DEFAULT_CALLER, True


def _resolve_caller(
    as_caller: str = "", ctx: Context | None = None, session_token: str = "", caller_pubkey: str = ""
) -> str:
    """Resolve the caller identity for a tool invocation.

    Precedence (Layer 1, 2026-08-28):
      1. explicit `as_caller` — a registry-validated slug the caller asserts for
         this specific call. Honoured only for a real registry slug; an unknown
         slug is silently ignored.
      2. session token — a UUID issued by bonsai_session_start and passed by
         the caller as `session_token`. Transport-independent: survives SSE/HTTP
         connection drops where ctx.session changes per-request. Pruned on expiry.
      3. caller_pubkey — Nostr/Buzz secp256k1 x-only hex pubkey resolved via
         buzz-identity-map.json. Stateless: no process-shared state, survives
         multi-process MCP deployments. Preferred for Buzz-hosted agents.
         (Layer 2, 2026-09-01)
      4. per-session cache via _effective_caller(ctx) — works only on long-lived
         connections (stdio, WebSocket); evaporates on SSE reconnect.
      5. immutable host default _DEFAULT_CALLER — env BONSAI_CALLER, or
         'unidentified' when unset. An unidentified caller is blocked from all
         reads and writes by _require_identified (Layer 0).

    Never raises; any failure falls through to the next level. Thin wrapper
    over _resolve_caller_ex() for call sites that only need the resolved
    slug, not whether it was defaulted (2026-09-05).
    """
    return _resolve_caller_ex(as_caller, ctx, session_token, caller_pubkey)[0]


def _require_identified(caller: str, tool_name: str) -> "str | None":
    """Return a refusal string if caller is 'unidentified'; None to proceed.

    Layer 0 (2026-08-28): an unidentified caller must not read or
    write anything. Call bonsai_session_start('<slug>') first to get a session
    token, then pass session_token on all subsequent calls — identity survives
    SSE/HTTP connection drops this way. Dedicated single-agent daemons (env
    BONSAI_CALLER set) resolve to their configured slug, not 'unidentified'.
    """
    if caller == "unidentified":
        return (
            f"[access refused — caller not identified; tool: {tool_name}. "
            "Call bonsai_session_start('<your-slug>') first, then pass the "
            "returned session_token on every subsequent call.]"
        )
    return None


# Approved external roots — absolute paths outside REPO_ROOT that the path guard
# may follow, but ONLY when reached through a legitimate in-repo symlink (e.g.
# an agent's memory directory symlinked into a private vault:
# agents/<slug>/memory → /…/vault/clear). Never reachable via "..".
# Host-configured and deliberately NOT committed: set BONSAI_ALLOWED_EXTERNAL_ROOTS
# to one or more absolute paths (os.pathsep-separated). Empty by default, so the
# guard stays fully closed unless the host explicitly opens a vault.
_ALLOWED_EXTERNAL_ROOTS: list = [
    Path(_p).resolve() for _p in os.environ.get("BONSAI_ALLOWED_EXTERNAL_ROOTS", "").split(os.pathsep) if _p.strip()
]

# ---------------------------------------------------------------------------
# Extract mode — a read-only extract of the garden on another host.
# When BONSAI_EXTRACT_MODE is set, writes never touch the tree; they queue as
# JSONL events into .outbox/ for the Mac to pull and ingest (garden-extract-
# ingest.py). Off by default — the Mac/normal path is byte-for-byte unchanged.
# ---------------------------------------------------------------------------
EXTRACT_MODE: bool = os.environ.get("BONSAI_EXTRACT_MODE", "").lower() in ("1", "true", "yes")
OUTBOX_DIR: Path = REPO_ROOT / ".outbox"


def _outbox_event(op: str, path: str, payload: str, caller: str | None = None) -> str:
    """Queue a write as a JSONL outbox event (extract mode). Returns the rel path."""
    import hashlib
    import json as _json
    import time as _time

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    ev = {
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "agent": _infer_slug(path) or caller or _DEFAULT_CALLER,
        "op": op,
        "path": path,
        "content-hash": hashlib.sha256(payload.encode()).hexdigest(),
        "payload": payload,
    }
    outfile = OUTBOX_DIR / f"{_time.strftime('%Y-%m-%d')}-{os.getpid()}.jsonl"
    with outfile.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(_json.dumps(ev) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)
    return str(outfile.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# Concurrency layer (spec-concurrent-garden-writes v1.0.0)
# L1 read tokens - L2 git three-way auto-merge - L3 type-aware policy - L4 audit
# ---------------------------------------------------------------------------

BONSAI_DIR_NAME = ".bonsai"
WRITE_LOG_NAME = "write-log.jsonl"


def _git(cmd: list, input_text: str | None = None, repo_root: Path | None = None) -> tuple:
    """Run a git command in REPO_ROOT, or in repo_root when given (write routing:
    a routed path's history lives in its own repo). Returns (stdout, stderr, rc)."""
    try:
        result = subprocess.run(
            ["git", *cmd],
            cwd=str(repo_root or REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            input=input_text,
        )
        return result.stdout, result.stderr, result.returncode
    except Exception as exc:
        return "", str(exc), -1


def _blob_store(content: str) -> str:
    """Store content in git's object store; return 12-char blob oid ('' on failure)."""
    out, _, rc = _git(["hash-object", "-w", "--stdin"], input_text=content)
    return out.strip()[:12] if rc == 0 and out.strip() else ""


def _blob_get(oid: str) -> str | None:
    """Retrieve blob content by (abbreviated) oid; None if unknown."""
    if not re.fullmatch(r"[0-9a-f]{6,40}", (oid or "").strip()):
        return None
    out, _, rc = _git(["cat-file", "blob", oid.strip()])
    return out if rc == 0 else None


def _merge_three_way(base: str, ours: str, theirs: str) -> tuple:
    """git merge-file three-way merge. Returns (n_conflicts, merged_text).
    n_conflicts: 0 clean, >0 conflicts (merged_text carries markers), <0 error."""
    with _tempfile.TemporaryDirectory() as td:
        pb, po, pt = Path(td) / "base", Path(td) / "ours", Path(td) / "theirs"
        pb.write_text(base, encoding="utf-8")
        po.write_text(ours, encoding="utf-8")
        pt.write_text(theirs, encoding="utf-8")
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                "current-on-disk (concurrent session)",
                "-L",
                "base (your last read)",
                "-L",
                "incoming (this write)",
                str(po),
                str(pb),
                str(pt),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout


def _compact_diff(a: str, b: str, max_lines: int = 30) -> str:
    """Compact unified diff a->b for reporting what a concurrent session changed."""
    lines = list(
        difflib.unified_diff(
            a.splitlines(),
            b.splitlines(),
            lineterm="",
            fromfile="your-base",
            tofile="on-disk",
            n=1,
        )
    )
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], f"... (+{len(lines) - max_lines} more diff lines)"]
    return "\n".join(lines)


def _audit(
    tool: str,
    path: str,
    hash_before: str,
    hash_after: str,
    merged: bool = False,
    note: str = "",
    caller: str | None = None,
) -> None:
    """L4: append one JSONL line to .bonsai/write-log.jsonl. Never blocks a write."""
    try:
        bdir = REPO_ROOT / BONSAI_DIR_NAME
        bdir.mkdir(exist_ok=True)
        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "caller": caller if caller is not None else _DEFAULT_CALLER,
            "tool": tool,
            "path": path,
            "hash_before": hash_before,
            "hash_after": hash_after,
            "merged": merged,
        }
        if note:
            entry["note"] = note
        with (bdir / WRITE_LOG_NAME).open("a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:  # the audit line never blocks the write it records; a lost line is said, not hidden
        print(f"[bonsai_mcp] write-log: not recorded ({e})", file=sys.stderr)


def _is_journal_path(path: str) -> bool:
    """True for agent journal files: .../memory/journal/*.md"""
    norm = "/" + path.replace("\\", "/").lstrip("/")
    return "/memory/journal/" in norm and norm.endswith(".md")


# Slice K — MCP build hash.
# Computed once at server startup; stable for the lifetime of the process.
# Value: first 8 hex chars of SHA-256 of this server file, so it changes
# whenever the server is updated and reflects the running code version.
# The verify procedure uses this as a signal that it is running in a
# trusted local MCP context (local path, no network call).
with open(os.path.abspath(__file__), "rb") as _self_file:
    _BUILD_HASH: str = hashlib.sha256(_self_file.read()).hexdigest()[:8]
_STARTED_AT = _dt.datetime.now(_dt.timezone.utc)


# Which process answered, and is it still running the code on disk? (
# 2026-09-23, on the gardener's charge.) One repo is served by several garden
# processes at once — the PM2 HTTP daemon, one stdio child per Desktop config,
# one per Claude Code session — and each keeps the code it started with. A
# server.py edit leaves every older process running old code, while
# build_hash above only ever describes the one you reached, and says nothing
# about whether it matches disk. These two lines make both visible from inside
# a session: what answered, and whether it is stale.
def _transport_from_argv() -> str:
    argv = sys.argv
    if "--transport" in argv:
        i = argv.index("--transport") + 1
        if i < len(argv):
            return argv[i]
    return "stdio"


def _disk_hash() -> str | None:
    try:
        return hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()[:8]
    except Exception:  # never block session start on a diagnostic
        return None


def _wired_core(pot: Path) -> Path | None:
    """The core a pot is wired to: `git config --get bonsai.core` in the pot (install-runtime sets it), or None. No guess."""
    try:
        r = subprocess.run(
            ["git", "-C", str(pot), "config", "--get", "bonsai.core"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    p = (r.stdout or "").strip()
    return Path(p) if r.returncode == 0 and p else None


def _env_block(slug: str, ctx: Context | None) -> str:
    """One environment block every seat renders the same way (the gardener, 2026-09-23:
    *"I would also want env to be displayed… We were running old versions for long
    times. If I see it I can watch it myself."*). Fixed keys, one per line, so a
    greeting widget can parse it. Fail-safe per field: a lookup that fails reads
    `unknown`, never blocks the boot, never invents a value.

    What the server cannot see, and so does not print: the model the agent runs
    on, and the skill version the harness loaded. Both are in the agent's own
    context; the widget takes them from there, never from the repo.
    """

    def safe(fn, default="unknown"):
        try:
            v = fn()
            return v if v else default
        except Exception:
            return default

    host = safe(lambda: os.uname().nodename)

    def _harness():
        ci = ctx.session.client_params.clientInfo  # MCP initialize handshake
        return f"{ci.name} {ci.version}".strip()

    harness = safe(_harness)

    def _repo():
        def git(*a):
            return subprocess.run(
                ["git", "-C", str(REPO_ROOT), *a], capture_output=True, text=True, timeout=3
            ).stdout.strip()

        head = git("rev-parse", "--short", "HEAD")
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = len([ln for ln in git("status", "--porcelain", "--untracked-files=no").splitlines() if ln])
        state = "clean" if dirty == 0 else f"{dirty} tracked file(s) uncommitted"
        return f"{REPO_ROOT.name} @ {head} ({branch}) · {state}"

    repo = safe(_repo)

    disk = _disk_hash()
    if disk is None:
        mcp = f"build {_BUILD_HASH} · disk unreadable"
    elif disk == _BUILD_HASH:
        mcp = f"build {_BUILD_HASH} · disk {disk} · current"
    else:
        mcp = f"build {_BUILD_HASH} · disk {disk} · STALE (running code is not the file on disk)"
    mcp += f" · {_transport_from_argv()} pid {os.getpid()}"

    # Where the human is — the gardener, 2026-09-23: *"Where is the human is the answer the
    # location service should surface."* The brokered record is the gardener's own device fix
    # (the principal this agent serves), not the machine the server runs on, so it stays true
    # when agent and human are on different hosts. Fail-closed: stale or absent says so.
    def _loc():
        principal, _g = _principal_for(slug)
        return f"{_location_line(slug).splitlines()[0]} · gardener {principal}"

    location = safe(_loc) if slug else "no agent slug given"

    # The installed skill's body against the repo's — never a whole-file shasum: the builder
    # rewrites the frontmatter on purpose (the gardener, 2026-09-23: "that understanding needs
    # to be baked in"). Contract and CLI: tools/bonsai-mcp/skill_install.py.
    skill = safe(lambda: _skill_install.env_line(REPO_ROOT, slug, _resolve)) if slug else "no agent slug given"

    # Anything but `ok` is a stop (AGENTS.md, "Every kin"), so this line never reads `unknown`: a
    # check that cannot run is a STOP naming why. The body is placed by the repo router.
    def _place(rel):
        if _ROUTER is None:
            return REPO_ROOT.name, REPO_ROOT / rel
        r = _ROUTER.resolve(rel)
        return r.repo, r.path

    # The verifier and the conformance checker are the DAEMON's tools (its own tree), never the served garden's:
    # a new garden's pot has no tools/ (Bonsai's rehearsal, gap 59, 2026-10-11).
    # A served pot's checker is the core THE POT IS WIRED TO (git config bonsai.core, set by install-runtime), never a sibling of
    # the daemon's tree by name: a stranger's garden has no such sibling. Our own garden keeps its sibling core. (Bonsai, 2026-10-11)
    _pot = REPO_ROOT if _is_pot() else None
    _core = (
        _wired_core(_pot)
        if _pot is not None
        else (
            SERVER_ROOT.parent / "bonsai-garden-core"
            if _origin_sign_py() == SERVER_ROOT / "tools" / "sign.py"
            else REPO_ROOT.parent / "bonsai-garden-core"
        )
    )
    integrity = safe(
        lambda: _integrity_line.line(
            REPO_ROOT,
            slug,
            _place,
            sign_py=_origin_sign_py(),
            signer=_signer(),
            conformance=(_core / "tools" / "check-shintai-conformance.py") if _core else None,
            pot=_pot,
            no_core=(_pot is not None and _core is None),
            pin=(_integrity_line.pin_for(_pot, slug) if _pot is not None else None),
        ),
        f"STOP — the integrity check did not run — {_integrity_line.STOP_TAIL}",
    )

    at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "\n".join(
        [
            f"host: {host}",
            f"harness: {harness}",
            f"repo: {repo}",
            f"mcp: {mcp}",
            f"skill: {skill}",
            f"integrity: {integrity}",
            f"location: {location}",
            f"at: {at}",
        ]
    )


def _meta_process_lines() -> str:
    lines = []
    transport = _transport_from_argv()
    disk = _disk_hash()
    if disk is None:
        lines.append("disk_hash: unreadable")
    if disk is not None:
        if disk == _BUILD_HASH:
            lines.append(f"disk_hash: {disk} (running code matches disk)")
        else:
            fix = (
                "pm2 restart bonsai-mcp"
                if transport == "http"
                else "restart this session; its stdio server restarts with it"
            )
            lines.append(f"disk_hash: {disk} — STALE: this process runs {_BUILD_HASH}, disk holds {disk}. Fix: {fix}.")
    ppid = os.getppid()
    parent = "?"
    with contextlib.suppress(
        OSError, subprocess.SubprocessError, ValueError
    ):  # the parent's name is only shown; '?' stands
        parent = (
            subprocess.run(
                ["ps", "-o", "command=", "-p", str(ppid)],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()[:90]
            or "?"
        )
    up = int((_dt.datetime.now(_dt.timezone.utc) - _STARTED_AT).total_seconds())
    lines.append(
        f"process: pid {os.getpid()} · transport {transport} · "
        f"started {_STARTED_AT.strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"(up {up // 3600}h{(up % 3600) // 60:02d}m)"
    )
    lines.append(f"parent: pid {ppid} · {parent}")
    return "\n".join(lines)


# Port/host are set here so they're fixed at server startup.
# FastMCP.run() does not accept host/port — they live in the constructor. So `--port` is read from the command line
# here, at import, like `--garden`, and wins over BONSAI_MCP_PORT. It used to be parsed after the server was built and
# never used: `--port 5999` bound 5052, the live daemon's port (the 0.8.2 acceptance run, 2026-10-17).
def _flag(name: str) -> str | None:
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


_HTTP_PORT = int(_flag("--port") or os.environ.get("BONSAI_MCP_PORT") or "5052")
# streamable_http_path="/" moves the MCP endpoint to root so health probes at GET /
# return 200 (SSE stream open) rather than 404. The health monitor probes 127.0.0.1:5052/
# with http.get(); FastMCP responds 200 to the SSE GET.
# The name this server announces (serverInfo.name). The origin keeps "bonsai-garden"; the
# runtime's wiring sets BONSAI_MCP_NAME=bonsai-garden-runtime, so the file stays byte-identical
# in both homes (check-copy-drift) while the two read apart in a client's connector list.
_MCP_NAME = os.environ.get("BONSAI_MCP_NAME") or "bonsai-garden"


def _server_icons():
    """The garden's logo as the server's icon (MCP serverInfo.icons). Fail-safe: no file, no icon."""
    try:
        import base64

        from mcp.types import Icon

        png = Path(__file__).resolve().parent / "garden-icon.png"
        uri = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
        return [Icon(src=uri, mimeType="image/png", sizes=["128x128"])]
    except Exception:
        return None


def _garden_url() -> str | None:
    """The served garden's own address (garden.json `url`), or none: no garden's address is this code's to name."""
    try:
        u = json.loads((REPO_ROOT / "garden.json").read_text("utf-8")).get("url")
        return u if isinstance(u, str) and u.startswith("https://") else None
    except (OSError, ValueError, AttributeError):
        return None


mcp = FastMCP(
    _MCP_NAME,
    host="127.0.0.1",
    port=_HTTP_PORT,
    streamable_http_path="/",
    website_url=_garden_url(),
    icons=_server_icons(),
)


# ---------------------------------------------------------------------------
# Multi-repo routing
# ---------------------------------------------------------------------------
# The split registry says where each path will eventually live AND whether it
# has moved yet. The router keys on `status`, so while every entry is `pending`
# this resolves exactly as it did before — mono holds everything. Paths start
# routing elsewhere one `adopted` entry at a time. See repo_routing.py.
try:
    from repo_routing import (
        PathHasMoved as _PathHasMoved,
    )
    from repo_routing import (
        PathRetired as _PathRetired,
    )
    from repo_routing import (
        RegistryUnreadable as _RegistryUnreadable,
    )
    from repo_routing import (
        Router as _Router,
    )
except ImportError:  # launched with a different cwd/sys.path (PM2, launchd)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from repo_routing import (
        PathHasMoved as _PathHasMoved,
    )
    from repo_routing import (
        PathRetired as _PathRetired,
    )
    from repo_routing import (
        RegistryUnreadable as _RegistryUnreadable,
    )
    from repo_routing import (
        Router as _Router,
    )

_SPLIT_REGISTRY: Path = REPO_ROOT / "rituals" / "repo" / "repo-split-registry.json"
_ROUTER = None
_ROUTER_ERROR: str = ""

if _SPLIT_REGISTRY.exists():
    try:
        _ROUTER = _Router(_SPLIT_REGISTRY, repos_root=REPO_ROOT.parent, served_root=REPO_ROOT)
    except Exception as _exc:
        # Do not take the server down: today every entry is `pending`, so no
        # routing decision is lost. But say so on stderr and keep the reason
        # reachable — a router that quietly stops routing after the first repo
        # is adopted would serve stale mono copies and report success.
        _ROUTER_ERROR = f"{type(_exc).__name__}: {_exc}"
        print(
            f"[bonsai-mcp] WARNING: repo router failed to load ({_ROUTER_ERROR}); all paths resolve to the mono repo.",
            file=sys.stderr,
        )
else:
    _ROUTER_ERROR = f"split registry not found at {_SPLIT_REGISTRY}"


# ---------------------------------------------------------------------------
# Deployment context (2026-09-14)
# ---------------------------------------------------------------------------
# Nothing in the boot payload told an agent where it's running or which Buzz relay it
# answers to — an agent over-credited its own environment twice in one day for exactly this
# reason (see tools/bonsai-mcp/deployment_context.py's own docstring). Same directory as
# this file, so a plain import resolves when launched as `python server.py`; the fallback
# covers the same PM2/launchd cwd cases as repo_routing/repo_skip_dirs above.
try:
    from deployment_context import deployment_context_text, host_and_head, newest_folder_evidence_reason
except ImportError:  # launched with a different cwd/sys.path (PM2, launchd)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from deployment_context import deployment_context_text, host_and_head, newest_folder_evidence_reason

# Is the SKILL.md a harness loaded the repo's body? One definition, shared with the CLI
# (tools/bonsai-mcp/skill_install.py). Imported here, at startup, so the exec gate's closure
# covers it — a module imported lazily after the gate would run unchecked.
try:
    import skill_install as _skill_install
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import skill_install as _skill_install

# The repo-copy integrity check as one env line (the ask of 2026-09-27): SKILL.md and the
# constitution by sign.py, the body by core's shintai gate. One definition, shared with the CLI
# (tools/bonsai-mcp/integrity_line.py); imported at startup for the same reason as above.
import integrity_line as _integrity_line  # noqa: E402  after the sys.path setup above, like the imports beside it
import signer as _signer_mod  # noqa: E402  the same

# ---------------------------------------------------------------------------
# Shared repo-sweep exclusions (2026-09-13)
# ---------------------------------------------------------------------------
# SKIP_DIR_NAMES lives in tools/repo_skip_dirs.py — shared with
# tools/check-agent-references.py, which learned this lesson first
# (2026-09-04): a repo-wide content sweep must skip .claude/worktrees/<name>
# (a full duplicate git worktree checkout of this repo — a live, recurring
# feature of this workshop's multi-session workflow) and tmp/ (scratch
# staging copies), or it silently returns a stale duplicate instead of the
# canonical file. bonsai_find_by_id never got this fix until found
# the gap live (6 of 6 real ULIDs resolved into a stale pre-flatten worktree
# copy instead of their canonical current file). One shared list so the two
# tools can't drift apart again.
try:
    from repo_skip_dirs import SKIP_DIR_NAMES
except ImportError:  # launched with a different cwd/sys.path (PM2, launchd)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from repo_skip_dirs import SKIP_DIR_NAMES


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _resolve(path: str) -> Path:
    """Resolve a repo-relative path.

    Stays inside REPO_ROOT by default. A path may resolve under one of the
    explicitly configured _ALLOWED_EXTERNAL_ROOTS (e.g. an agent's memory
    symlinked into a private vault) — but only when reached through an in-repo
    symlink, never through explicit parent traversal. Raises ValueError otherwise.
    """
    # Explicit ".." traversal is never allowed — the only sanctioned way out of
    # the repo tree is following an approved in-repo symlink.
    if ".." in Path(path).parts:
        raise ValueError(f"Path '{path}' contains parent traversal ('..'), which is not allowed.")

    # Has this path left the mono repo? While every registry entry is `pending`
    # the answer is always no and this falls straight through.
    if _ROUTER is not None:
        try:
            _res = _ROUTER.resolve(path)
        except _RegistryUnreadable as _exc:
            # The routing table changed on disk and the new content will not
            # parse. Refuse rather than route on stale rules — a stale pointer
            # is indistinguishable from an unregistered path.
            raise ValueError(str(_exc)) from None
        except _PathRetired as _exc:
            # Archived or deliberately dropped. Refuse and say so — the caller
            # gets a decision, not a not-found it has to interpret.
            raise ValueError(str(_exc)) from None
        except _PathHasMoved as _exc:
            # Loud, and it names the repo. The failure this replaces is a read
            # that quietly finds nothing because the content moved.
            raise ValueError(str(_exc)) from None
        if _res.moved:
            _p = _res.path
            try:
                _p.relative_to(_res.root)
            except ValueError:
                raise ValueError(
                    f"Path '{path}' routes to {_res.repo} but escapes that repo's root ({_res.root})."
                ) from None
            return _p

    p = (REPO_ROOT / path).resolve()
    try:
        p.relative_to(REPO_ROOT)
        return p
    except ValueError:
        pass
    for _root in _ALLOWED_EXTERNAL_ROOTS:
        try:
            p.relative_to(_root)
            return p
        except ValueError:
            continue
    raise ValueError(f"Path '{path}' escapes the repository root. All paths must be relative to the repo root.")


def _resolve_write(path: str) -> Path:
    """Resolve a path for a MUTATING tool (write / append / delete / move).

    Writes now route like reads (spec-write-routing.md, 2026-09-23). A path the split
    registry has `adopted` into another garden repo resolves to that repo — so a body
    moved to pot can still write its own working-state and journal. `_resolve` already
    refuses what must not be written: archived / dropped / retired paths, an unreadable
    registry, a target repo that is not checked out, a route that escapes its root.

    Why the old refusal could go: it enforced "the gardener names every cross-repo write",
    and since 2026-09-23 the write-scope contract has the WRITER name the repo (in the
    result line here, and in the commit), under the gardener's standing grant. Its two
    engineering reasons are answered, not ignored: git now runs in the repo the path
    lives in (_git(..., repo_root=)), and re-signing is key-based (HKDF, registry
    fingerprint checked) — it attests the signer, not the checkout.

    Guards run on the caller's LOGICAL path before this is called, so who may write
    what is unchanged by routing.
    """
    return _resolve(path)


def _garden_roots() -> list:
    """[(repo_slug, root)]: the SERVED garden first, under the name its routing table gives it (the hub's, or a listed
    repo's whose sibling checkout is this one; else its folder's name), then the hub and every listed repo checked out
    beside it. Read fresh: the registry changes on disk. Served from mono, this is mono first as it always was; served from
    the origin's pot (the 0.8.2 acceptance run), the pot is first and mono is a sibling like any other."""
    served, others = REPO_ROOT.name, []
    with contextlib.suppress(
        OSError, ValueError, AttributeError
    ):  # a garden with no readable split registry routes nothing
        table = json.loads(_SPLIT_REGISTRY.read_text(encoding="utf-8"))
        listed = [table.get("hub")] + [r.get("slug") if isinstance(r, dict) else None for r in table.get("repos") or []]
        for slug in dict.fromkeys(s for s in listed if isinstance(s, str) and s and "/" not in s):
            root = REPO_ROOT.parent / slug
            if root.resolve() == REPO_ROOT:
                served = slug
            elif root.is_dir():
                others.append((slug, root))
    return [(served, REPO_ROOT), *[(s, r) for s, r in others if s != served]]


def _locate(path: Path, roots: list | None = None) -> tuple:
    """(repo_slug, repo_root, repo-relative str) for a path inside any garden repo,
    else (None, None, str(path)). The served garden wins when roots nest (they do not today)."""
    p = Path(path)
    for slug, root in roots or _garden_roots():
        try:
            return slug, root, str(p.relative_to(root))
        except ValueError:
            continue
    return None, None, str(p)


def _rel(path: Path) -> str:
    """Return path as repo-relative string — prefixed '<repo>:' when the path lives in
    a garden repo other than the served one, so a routed path is never shown as if it were
    the served garden's (write routing, 2026-09-23 — spec-write-routing.md §2.5)."""
    roots = _garden_roots()
    slug, _root, rel = _locate(path, roots)
    if slug and slug != roots[0][0]:
        return f"{slug}:{rel}"
    return rel


def _where(path: Path) -> str:
    """' → <repo>' when a write landed outside the served garden, else ''. A routed write is never silent."""
    roots = _garden_roots()
    slug, _root, _ = _locate(path, roots)
    return f" → {slug}" if slug and slug != roots[0][0] else ""


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    Extract YAML frontmatter from markdown content.
    Returns (frontmatter_dict, body). If no frontmatter, returns ({}, content).
    Handles flat key: value, block lists (- item), and quoted values.

    Intentional shortcut: nested blocks (e.g. the `sign:` block in
    working-state.md) are flattened — children become top-level keys.
    Workshop frontmatter is intentionally flat by convention; nested
    blocks are the exception, and the summary header this parser produces
    is informational, not authoritative. Use the raw body for round-trip.
    """
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    fm_text = content[3:end].strip()
    body = content[end + 4 :].lstrip("\n")

    fm: dict = {}
    current_key: str | None = None
    list_items: list[str] = []

    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- "):
            item = stripped[2:].strip().strip('"').strip("'")
            if current_key is not None:
                list_items.append(item)
            continue

        if ":" in line:
            # Flush pending list
            if current_key is not None and list_items:
                fm[current_key] = list_items
                list_items = []

            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            current_key = key
            if val:
                fm[key] = val
            else:
                list_items = []

    # Flush final list
    if current_key is not None and list_items:
        fm[current_key] = list_items

    return fm, body


# ---------------------------------------------------------------------------
# Shell helper
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[str, str, int]:
    """Run a subprocess and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        cmd,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout, result.stderr, result.returncode


def _has_rg() -> bool:
    """Check whether ripgrep (rg) is available."""
    try:
        subprocess.run(["rg", "--version"], capture_output=True, check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _find_by_id_path(id_value: str, root: Path | None = None) -> Path | None:
    """The live file carrying `id: <id_value>`. With `root`, the first match under that tree, as before. Without, every
    repo of the garden is searched (served first, then the hub and the listed siblings), and a match counts only where a
    read of its address would reach that very file: a stale copy or a tombstone that kept the id is not the document (the
    0.8.2 acceptance run: served from the pot, mono's ids were "not found"; served from mono, core's and the pot's were).
    When no match is the routed one, the served garden's first match is returned, as it was."""
    if root is not None:
        hits = [h for h in _id_hits(id_value, root) if _carries_id(h, id_value)]
        return hits[0] if hits else None
    roots = _garden_roots()
    first_served = None
    for slug, r in roots:
        for hit in _id_hits(id_value, r):
            if not _carries_id(hit, id_value):  # an archive that quotes another document's header is not that document
                continue
            if first_served is None and r == REPO_ROOT:
                first_served = hit
            if _routed_here(hit, slug, r):
                return hit
    return first_served


def _carries_id(f: Path, id_value: str) -> bool:
    """Is `id_value` the id in this file's own frontmatter (not a line further down that looks like one)?"""
    try:
        fm, _ = _parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    return str(fm.get("id", "")).strip() == id_value


def _routed_here(hit: Path, slug: str, root: Path) -> bool:
    """Would a read of one of this file's garden addresses reach this very file?"""
    try:
        rel = hit.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return False
    cands = [rel] + (_ROUTER.logical_paths(slug, rel) if _ROUTER is not None else [])
    for cand in cands:
        try:
            if _resolve(cand).resolve() == hit.resolve():
                return True
        except ValueError:
            continue
    return False


def _id_hits(id_value: str, root: Path | None = None) -> list[Path]:
    """Locate the .md file whose frontmatter carries `id: <id_value>`.

    Excludes SKIP_DIR_NAMES (tools/repo_skip_dirs.py) from the sweep —
    critically, .claude/worktrees/<name>/ (a full duplicate git worktree
    checkout of this same repo — a live, recurring feature of this
    workshop's multi-session workflow) and tmp/ (scratch staging copies).
    Without this exclusion a locked worktree carrying a stale pre-flatten
    layout is structurally identical to the canonical tree, and whichever
    copy the search tool visits first wins — silently, with no error and no
    ambiguity warning (2026-09-13: 6 of 6 real ULIDs resolved into
    a stale worktree copy instead of the canonical current file before this
    fix). Split out of bonsai_find_by_id so it can be exercised directly by
    a regression test (the find-by-id probe beside this file) without going
    through the MCP tool-call plumbing.

    Returns every matching Path under `root` (default REPO_ROOT), sorted; [] when
    no .md file there carries that id. _find_by_id_path decides which one is live.
    """
    search_root = root if root is not None else REPO_ROOT
    safe_id = re.escape(id_value)
    pattern = f"^id: {safe_id}$"

    rg_excludes: list[str] = []
    grep_excludes: list[str] = []
    for _d in SKIP_DIR_NAMES:
        rg_excludes += ["--glob", f"!{_d}"]
        grep_excludes += ["--exclude-dir", _d]

    hits: list[str] = []
    if _has_rg():
        stdout, _, _ = _run(["rg", "-l", "--glob", "*.md", *rg_excludes, pattern, str(search_root)])
        hits = stdout.strip().splitlines()
    if not hits:
        # grep fallback
        stdout, _, _ = _run(["grep", "-rl", *grep_excludes, f"id: {id_value}", str(search_root), "--include=*.md"])
        hits = stdout.strip().splitlines()
    return [Path(h) for h in sorted(hits) if h and Path(h).exists()]


# ---------------------------------------------------------------------------
# Auto-sign helpers
# ---------------------------------------------------------------------------

_GARDEN_MARK_MS = 612


def _stamp_612(p: Path) -> None:
    """Set the file's mtime millisecond component to 612 — the garden's quiet mark.

    Every file the garden writes through this server carries the same fractional
    second. It verifies nothing and secures nothing; the signatures do that. It
    is a maker's mark — a way to look at a filesystem and see which files the
    garden's own hand touched.

    Deliberately best-effort and silent on failure. A filesystem without
    sub-second mtime resolution, or a race with a concurrent writer, must never
    turn a write that already succeeded into an error. A nicety that can break a
    write is not a nicety.

    Ordering note: this must be applied AFTER any re-signing, because signing
    rewrites the file and would otherwise clobber the mark.
    """
    with contextlib.suppress(OSError):  # the mark time is cosmetic; the write itself has landed
        sec = int(p.stat().st_mtime)
        ns = sec * 1_000_000_000 + _GARDEN_MARK_MS * 1_000_000
        os.utime(p, ns=(ns, ns))


def _has_sign_block(content: str) -> bool:
    """Return True if content has a sign: key in the first 60 lines (frontmatter zone)."""
    return any(line.strip().startswith("sign:") for line in content.split("\n")[:60])


def _is_constitution_file(path: str) -> bool:
    """Return True for about-me.md and SKILL.md — constitution files require explicit re-sign."""
    return Path(path).name in ("about-me.md", "SKILL.md")


def _infer_slug(path: str) -> str | None:
    """
    Infer signing agent slug from a repo-relative path.
    Only works for paths inside agents/ where the slug is unambiguous.

    agents/<slug>/...                       → <slug>
    agents/<parent>/sub-agents/<slug>/...   → <slug>
    Anything outside agents/ (lore/, rituals/, etc.) → None
    """
    # Registry first (D6): the directory name is NOT always the owner's
    # slug — a kin may live at a directory that is not its slug, so deriving the slug from the path
    # text alone attributed writes there to the directory name, a member that does not
    # exist, and handed sign.py --as <dirname>. One resolver for authorization and
    # attribution, so the two cannot drift apart again.
    owner, _tail = _owner_of(path)
    if owner:
        return owner
    parts = Path(path).parts
    if not parts or parts[0] != "agents":
        return None
    if len(parts) < 2:
        return None
    # Skip shared/ and a human's own tree: no kin key signs there, and a person signs himself
    if parts[1] == "shared" or _is_human(parts[1]):
        return None
    # Sub-agent path: agents/<parent>/sub-agents/<slug>/...
    if len(parts) >= 4 and parts[2] == "sub-agents":
        return parts[3]
    return parts[1]


_RM_BRIDGE = None  # lazy-loaded remote-memory bridge (Option A sealed-memory interception)

# One constant, two readers: the loader below and the execute gate's closure walk.
# The gate cannot discover this file from sys.modules (module_from_spec never
# registers it, and the load happens on the first sealed-memory write, long after
# __main__), so it has to name the path — and a named path written twice is the
# house's own second-master failure.
_RM_BRIDGE_PATH = Path(__file__).resolve().parent.parent / "remote-memory" / "remote_memory.py"


def _rm_bridge():
    global _RM_BRIDGE
    if _RM_BRIDGE is None:
        try:
            import importlib.util

            rmpath = _RM_BRIDGE_PATH
            spec = importlib.util.spec_from_file_location("rm_bridge", rmpath)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _RM_BRIDGE = mod
        except Exception:
            _RM_BRIDGE = False
    return _RM_BRIDGE or None


def _seal_memory_write(tool_name: str, path: str, content: str):
    """Option A (spec-git-vault-memory §9): if `path` is a memory-tree write for a sealed-memory
    agent, seal it to the vault instead of letting plaintext land in the repo. Returns a result
    string (sealed/handled) or None (proceed with the normal write). Flag-gated (RM_REMOTE),
    default-off, fail-safe — never raises."""
    rm = _rm_bridge()
    if rm is None:
        return None
    try:
        return rm.intercept_mcp_write(tool_name, path, content)
    except Exception:
        return None


# ── Encrypted-vault guard ─────────────────────────────────────────────────────
# A kin's memory may live in an encrypted vault outside the garden, reached through an in-repo symlink (BONSAI_ALLOWED_
# EXTERNAL_ROOTS above). This server writes plaintext and does not seal into a vault, so a write whose path passes through
# such a symlink is refused: nothing of a vault-backed memory lands unencrypted. Decided by what the path IS (a symlink out of
# the garden), never by a kin's name, so it holds in any garden with no configuration; a garden may also name vault roots
# (repo-relative, os.pathsep-separated) in BONSAI_VAULT_MEMORY_ROOTS. Off only with BONSAI_VAULT_GUARD=0, once a vault is
# wired to this write path. (First written 2026-06-23 for one kin's vault by name; made a rule 2026-10-15, when the
# gardener ruled that runtime carries no garden's instance.)
_VAULT_GUARD = os.environ.get("BONSAI_VAULT_GUARD", "1").lower() in ("1", "true", "yes")
_VAULT_ROOTS = [
    r.strip().strip("/") for r in os.environ.get("BONSAI_VAULT_MEMORY_ROOTS", "").split(os.pathsep) if r.strip()
]


def _vault_guard(path: str):
    """Return a refusal string if `path` writes into a vault-backed tree this backend cannot seal; None to proceed.
    Fail-safe toward refusing a vault write, never raises."""
    if not _VAULT_GUARD:
        return None
    refusal = (
        "[memory write deferred — this path lies in an encrypted vault that is not wired on this backend. The "
        "write was NOT performed, to avoid storing plaintext. Conversation continues normally; memory persistence "
        "resumes once the vault is wired to this write path.]"
    )
    try:
        norm = str(path).replace("\\", "/").lstrip("./")
        if any(norm == r or norm.startswith(r + "/") for r in _VAULT_ROOTS):
            return refusal
        root = REPO_ROOT.resolve()
        cur = REPO_ROOT
        for part in [x for x in norm.split("/") if x][:-1]:  # every directory on the way, not the file itself
            cur = cur / part
            if cur.is_symlink():
                target = cur.resolve()
                if target != root and root not in target.parents:
                    return refusal
    except Exception:
        return None
    return None


# ── Working-state / journal self-write guard (2026-07-17) ──────────
# Implements the rule proposed in rituals/tasks/lessons.md (2026-07-15 incident:
# "Only an agent may write to their own memory/working-state.md ... Another
# agent ... does not write into it directly"), now that per-connection caller
# identity (_effective_caller, see above) is reliable enough to enforce it on.
# Scoped to exactly the two file classes codex §7 names as self-authored
# (working-state: agent maintains; journal: agent-only, append-only) — NOT the
# whole memory/ tree: patterns.md is agent-proposes/gardener-approves and
# about-me/strategic-context are the gardener's, both of which legitimately need
# non-self writers.
#
# Bootstrap exception: creating a file that does not exist yet is allowed from
# any caller (synthesis/assemble scaffolds a new agent's starter working-state
# before that agent has ever been invoked) — only an EXISTING record is
# protected from a non-owning hand.
#
# Gardener override: the gardener may always write directly — the workshop's human
# ground truth, and the one party every agent already answers to.
#
# Feature-flagged (default on) so it can be backed out without a code change if
# it turns out to block a legitimate workflow this incident didn't anticipate.
# NOTE: per rituals/tasks/lessons.md, the rule itself is "proposed — pending
# gardener ratification, not yet binding." This flag defaulting on is a judgment
# call, not the ratification; flip BONSAI_SELF_MEMORY_GUARD=0 to hold it open
# pending that word without a code change.
_SELF_MEMORY_GUARD = os.environ.get("BONSAI_SELF_MEMORY_GUARD", "1").lower() in ("1", "true", "yes")

_SELF_MEMORY_PATTERN = re.compile(
    r"^agents/(?P<slug>[A-Za-z0-9_-]+)/memory/"
    r"(?:working-state\.md|journal/[^/]+\.md)$"
)

# ── Registry-derived owner resolution (2026-08-19) ──
# Spec: rituals/specs/spec-owner-lock-registry-derived.md
#
# The regex above cannot see a nested sub-agent path — its slug class excludes
# "/" — so agents/<parent>/sub-agents/<slug>/memory/..., which agents/registry.json
# declares CANONICAL, was unprotected for five crew (the nested *-shin members,
# among them). Widening the regex would only drift from the registry again the moment
# flattens the tree. Instead resolve the owner from the registry's own `path`
# field: codex §12 makes the registry the single source of truth, and P-B006 says
# derive registry-owned data rather than hand-copy it.
#
# The regex is KEPT and unioned in as a static floor (see _protected_owner). A
# registry-only guard fails OPEN: an unreadable registry.json would silently remove
# every owner-lock, strictly worse than today, since the regex needs no registry at
# all. Registry failure must degrade to TODAY's protection, never to none.
_PROTECTED_TAIL = re.compile(r"^memory/(?:working-state\.md|journal/[^/]+\.md)$")
_NO_KEY_SLUGS = ("shared",)


# A garden's human is not a kin: no owner root, no kin key, not a known kin slug. Named by the registry's `type`, never by a
# slug: "gardener", plus whatever legacy name the served garden declares in its own garden.json (integrity_line.human_types).
def _human_types() -> tuple:
    return _integrity_line.human_types(REPO_ROOT)


_OWNER_ROOTS_CACHE = None  # ([(path, slug)] longest-first, {slug})


def _owner_roots():
    """Owner roots and known slugs from agents/registry.json.

    Only `path` values under agents/ become roots — a service kin's path is
    tools/<slug>-serve.py and must never claim a tree. Cached per process.
    Fail-safe: never raises; an unreadable registry yields ([], set()) and the
    static floor in _protected_owner takes over."""
    global _OWNER_ROOTS_CACHE
    if _OWNER_ROOTS_CACHE is not None:
        return _OWNER_ROOTS_CACHE
    roots, slugs = [], set()
    try:
        import json as _json

        data = _json.loads((REPO_ROOT / "agents" / "registry.json").read_text(encoding="utf-8"))
        entries = data.get("agents") or []
        if isinstance(entries, dict):
            entries = [dict(v or {}, slug=k) for k, v in entries.items()]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            slug = (entry.get("slug") or "").strip().lower()
            if not slug:
                continue
            if entry.get("type") in _human_types():
                continue  # a person: no owner root, and not a kin slug any guard below recognizes
            slugs.add(slug)
            if slug in _NO_KEY_SLUGS:
                continue
            p = (entry.get("path") or "").strip().strip("/")
            if p.startswith("agents/") and p != "agents/shared":
                roots.append((p, slug))
        roots.sort(key=lambda t: -len(t[0]))
    except Exception:
        roots, slugs = [], set()
    _OWNER_ROOTS_CACHE = (roots, slugs)
    return _OWNER_ROOTS_CACHE


def _owner_of(path) -> tuple:
    """(owner_slug, tail) for a repo-relative path, else (None, "").

    Resolution order: a literal legacy sub-agents/<slug>/ segment first (see
    below), then the longest matching registry `path` prefix, then the legacy
    agents/<slug>/ shape (which still protects a non-canonical second home, e.g.
    a nested kin's own agents/<slug>/). Never raises.

    Note this returns the REGISTRY's slug, not the directory name. (If a kin was renamed, the
    registry holds its canonical slug and the old name is only
    an alias — so this matters whenever a rename leaves the two apart,
    not just for one example.)

    fix (2026-09-21): a literal sub-agents/<slug>/ segment
    at the EXACT legacy position — agents/<parent>/sub-agents/<slug>/... —
    is checked BEFORE the registry-prefix match, not after. Without this, a
    legacy pre-flatten path like agents/<parent>/sub-agents/<slug>/memory/
    working-state.md matched the parent's registered agents/<parent> path as the
    longest prefix and never got as far as recognizing the nested <slug>
    segment — so _protected_owner found no protected tail and
    _self_memory_guard fell through silently, unrefused, for a foreign
    caller. agent_path_resolve.slug_from_agents_path() already checks this
    shape first for exactly this reason (its own docstring, resolution order
    item 1); this mirrors that item's INTENT only, not its implementation —
    the registry-prefix mechanism keeps its documented precedence for every
    other case (agent_path_resolve.py's docstring: "a different, more robust
    mechanism ... not path-segment parsing" — that advantage is real and
    untouched here).

    Deliberately NOT "sub-agents anywhere in the path": an adversarial test
    against agent_path_resolve.py (tools/test-agent-path-resolve-adversarial.py,
    2026-09-21) proved that shape is itself an impersonation route —
    a foreign-planted sub-agents/<real-slug>/ directory nested arbitrarily
    deep inside ANOTHER agent's own tree (e.g.
    agents/<parent>/assets/sub-agents/<slug>/...) overrides that agent's real
    ownership of their own files, because "search anywhere" fires before the
    registry-prefix match ever gets a chance to say the path is actually
    the parent's. This function only recognizes the position actually
    flattened away from — parts[0]=="agents", parts[2]=="sub-agents" — one
    parent segment, then the literal directory, then the real owner's slug.
    Every other "sub-agents" segment falls through to the registry-prefix
    match like any other path, exactly as before this fix.

    This does not make either path WRITABLE or canonical again — it only
    makes _self_memory_guard recognize whose identity is being targeted
    there, so a foreign write is refused instead of silently proceeding."""
    with contextlib.suppress(
        AttributeError, TypeError, ValueError
    ):  # no owner by the registry; _protected_owner's static floor still holds
        norm = str(path).replace("\\", "/").lstrip("./")
        parts = norm.split("/")
        if len(parts) >= 4 and parts[0] == "agents" and parts[2] == "sub-agents":
            nested_slug = parts[3].lower()
            _, slugs = _owner_roots()
            if nested_slug in slugs and nested_slug not in _NO_KEY_SLUGS:
                return nested_slug, "/".join(parts[4:])
        roots, slugs = _owner_roots()
        for rpath, slug in roots:
            if norm == rpath or norm.startswith(rpath + "/"):
                return slug, norm[len(rpath) :].lstrip("/")
        if len(parts) >= 2 and parts[0] == "agents" and parts[1] not in _NO_KEY_SLUGS and parts[1].lower() in slugs:
            return parts[1].lower(), "/".join(parts[2:])
    return None, ""


def _is_retired_legacy_path(path) -> bool:
    """True iff `path` is the exact pre-legacy nested shape
    (agents/<parent>/sub-agents/<slug>/...) for a real registry slug —
    the one address class the flatten deliberately deleted and that no
    current scaffolding path ever creates. Deliberately the SAME narrow
    position check _owner_of() uses for this shape (see its own
    docstring) — not "sub-agents anywhere in the path", which
    tools/test-agent-path-resolve-adversarial.py (2026-09-21)
    proved is its own impersonation route in the sibling resolver.
    Never raises."""
    with contextlib.suppress(AttributeError, TypeError, ValueError):  # not the retired legacy shape
        norm = str(path).replace("\\", "/").lstrip("./")
        parts = norm.split("/")
        if len(parts) >= 4 and parts[0] == "agents" and parts[2] == "sub-agents":
            nested_slug = parts[3].lower()
            _, slugs = _owner_roots()
            return nested_slug in slugs and nested_slug not in _NO_KEY_SLUGS
    return False


def _protected_owner(path):
    """Owner slug if `path` is an owner-locked record (working-state.md or a
    journal entry), else None. Registry-derived UNION the static floor.
    Never raises."""
    try:
        owner, tail = _owner_of(path)
        if owner and _PROTECTED_TAIL.match(tail):
            return owner
        m = _SELF_MEMORY_PATTERN.match(str(path).replace("\\", "/").lstrip("./"))
        return m.group("slug") if m else None
    except Exception:
        return None


def _self_memory_guard(path: str, caller: str, allow_bootstrap: bool = True):
    """Return a refusal string if `path` is another agent's own working-state.md
    or journal entry and `caller` is neither its owner nor the gardener; None to
    proceed. Fail-safe — never raises.

    allow_bootstrap=True (default) — creating a not-yet-existing record is allowed
    from any caller, because synthesis scaffolds a new agent's starter
    working-state before that agent has ever been invoked. Only an EXISTING record
    is protected from a non-owning hand.

    allow_bootstrap=False — pass this for a MOVE DESTINATION. A move's dst never
    exists yet, so with the default exemption a dst check is dead code in every
    real case: a foreign caller could forge a record at an owner's protected path
    simply by moving a file into it. Found 2026-08-19 by the live A10 probe
    (tools/bonsai-mcp/verify-owner-lock-live.py) AFTER the AST-level check had
    already reported dst as "covered" — it was structurally present and
    semantically inert. Writing a record and forging one by rename are different
    acts and do not share an exemption.

    fix (2026-09-21): bootstrap is also refused for a retired legacy
    path (_is_retired_legacy_path), regardless of allow_bootstrap or whether
    the file currently exists. "Not yet existing" and "never existing" look
    identical to (REPO_ROOT / norm).exists() alone — that is exactly why
    's flatten silently converted every deleted sub-agents/ address
    into a bootstrap-exempt one: the exemption could not tell a cradle from
    a grave. Nothing in current scaffolding ever creates a NEW agent at this
    shape (retired it), so a legacy address needs no bootstrap
    exemption to protect — there is no legitimate first-write case at that
    shape left to protect."""
    if not _SELF_MEMORY_GUARD:
        return None
    try:
        norm = str(path).replace("\\", "/").lstrip("./")
        owner = _protected_owner(norm)
        if not owner:
            return None
        if caller == owner or _is_garden_owner(caller):
            return None
        if allow_bootstrap and not _is_retired_legacy_path(norm) and not (REPO_ROOT / norm).exists():
            return None  # bootstrap: scaffolding a new/not-yet-invoked agent
        return (
            f"[write refused — '{path}' is {owner}'s own working-state/journal record; "
            f"only {owner}'s own invocation (or {_garden_owner() or 'the gardener'}) may write into it directly. "
            f"This was NOT performed. Route the finding through an inbox note, a charge, "
            f"or a handover instead — see rituals/tasks/lessons.md, "
            f"'writes only by the owning agent's own invocation' (2026-07-15).]"
        )
    except Exception:
        return None


# ── home/ layer access control (2026-09-22) ──
# Spec: bonsai-garden-core/rituals/specs/spec-canonical-shintai-structure.md
# §"The home/ layer — substructure, access model, and an honest limit" (v3.0.0).
# That section names the gap in code: "_owner_roots() ... builds ownership only
# from registry paths starting agents/; home/ appears nowhere in server.py."
# Thirteen more bodies are about to move correspondence into home/<slug>/.
#
# DESIGN CHOICE — a SEPARATE resolver/guard, not a generalisation of
# _owner_roots()/_self_memory_guard(). Rejected the alternative (teaching the
# existing lock a per-subdirectory rule) because the two guards answer
# different questions. _self_memory_guard asks "whose RECORD is this" — a
# narrow record-TYPE test (only memory/working-state.md and journal/*.md,
# nested under agents/<slug>/) that has already absorbed five incident-driven
# patches (/036/047/146, the sub-agents/ nesting fix) and whose
# call-site contract (single Optional[str] refusal, keyed to "own
# working-state/journal record") four tools already depend on. This guard
# asks "what is the POLICY for this SUBDIRECTORY NAME" under home/<slug>/ — a
# lookup over a fixed, small vocabulary (inbox/outbox/working/misc/temp),
# unrelated to record type. home/'s address shape (home/<slug>/...) cannot
# collide with agents/'s (agents/<slug>/...), so composing two independent
# guards at each write call site is exactly as safe as one, and a future
# home/ policy change can never regress the already-hardened agents/ lock
# (or vice versa) — the "second master" failure this garden keeps finding
# is two games sharing one mechanism, not two mechanisms sharing one call site.
_HOME_SUBDIR_POLICY = {
    "inbox": "open",  # anyone may leave a letter — the whole point of an inbox
    "outbox": "owner",  # the agent's own — letters sent
    "working": "owner",  # the agent's own drafts/projects
    "misc": "owner",  # the honest pile — the kin sort their own, on their own schedule
    "temp": "owner",  # cleansed regularly — see note below; owner writes here, no one else
}
# "temp/ cleansed regularly" — decided here, not left implicit: write is
# OWNER-only (same as working/misc), not open. Nothing in the spec suggests
# temp/ is shared scratch; it reads as the owner's own ephemeral drafts that
# don't warrant misc/'s indefinite honesty. "Cleansed" is a WHO/HOW OFTEN/
# WHAT AGE this pass explicitly does NOT wire up an unattended deletion job
# for: AGENTS.md ("Deletes are separate... needs the gardener's approval at
# the moment it is needed") and this session's own operating rules both gate
# scheduled/unattended deletion behind explicit approval, which no one has
# given. Proposed mechanically for whoever picks this up: a standalone
# read+report script (not wired to a scheduler) that lists home/*/temp/
# entries older than 14 days for the owner or the gardener to clear — visibility
# first, automation only once someone has watched it run.
_NO_KEY_SLUGS_HOME = _NO_KEY_SLUGS  # same "no personal key" slugs as agents/
_HOME_SLUG_FLOOR = re.compile(r"[a-z0-9][a-z0-9-]*")  # the home/ owner when no registry can be read


def _home_owner_of(path) -> tuple:
    """(slug, subdir, tail) for a home/<slug>/<subdir>/... path, else
    (None, None, ""). Never raises.

    Only recognizes a slug the registry actually knows — via _owner_roots()'s
    own slug set, reused as DATA (not its prefix-matching logic, which this
    function doesn't need: home/'s shape is a flat two-segment prefix, not a
    variable-depth registry `path`). A slug a READABLE registry does not know
    has no known owner to protect: there is nothing yet to protect a stranger
    from forging. An UNREADABLE registry is another case (2026-10-15, the quality
    pass): it used to degrade to no protection at all, so a broken registry
    switched the home/ owner lock off. It now falls back to the static floor the
    agents/ guard already has (_SELF_MEMORY_PATTERN): the directory's own name is
    its owner, a well-formed slug, so the lock fails closed."""
    with contextlib.suppress(AttributeError, TypeError, ValueError):  # not a home path
        norm = str(path).replace("\\", "/").lstrip("./")
        parts = norm.split("/")
        if len(parts) >= 3 and parts[0] == "home":
            slug = parts[1].lower()
            _, slugs = _owner_roots()
            known = slug in slugs if slugs else bool(_HOME_SLUG_FLOOR.fullmatch(slug))
            if known and slug not in _NO_KEY_SLUGS_HOME:
                return slug, parts[2].lower(), "/".join(parts[3:])
    return None, None, ""


def _home_access_guard(path: str, caller: str, allow_bootstrap: bool = True):
    """Return a refusal string if `path` is under another agent's owner-only
    home/ subdirectory (outbox/working/misc/temp) and `caller` is neither its
    owner nor the gardener; None to proceed — including every path under
    inbox/ (open by design) and every path outside home/ entirely. Fail-safe:
    never raises, and an unreadable registry falls back to the directory's own
    name as its owner (_home_owner_of's static floor): with no readable registry, only
    that name may write its owner-only folders.

    allow_bootstrap=True (default) mirrors _self_memory_guard's own bootstrap
    exemption, and for the same operational reason: the imminent move of
    thirteen bodies' correspondence into home/<slug>/ is itself performed by
    a caller who is not always that slug's own invocation — a restructuring
    dispatch (a sub-agent's work done by its parent seat is exactly
    this shape). The exemption is PER TREE (2026-09-22 fix): creating into a
    home/<slug>/ tree that does not exist yet is allowed from any caller so
    first-time scaffolding is never blocked; once home/<slug>/ exists, every
    owner-only write there — create or overwrite — is checked against caller
    identity. (The original per-FILE rule left foreign creation open; see the
    inline note at the check.) Pass allow_bootstrap=False for a move
    destination — a dst never exists yet, so the default exemption would
    otherwise be dead code, letting a foreign caller forge a file into an
    owner's home/working/ simply by moving one there (the exact class of gap
    's live A10 probe found in the agents/ lock on 2026-08-19)."""
    if not _SELF_MEMORY_GUARD:
        return None
    try:
        norm = str(path).replace("\\", "/").lstrip("./")
        owner, subdir, _tail = _home_owner_of(norm)
        if not owner:
            return None
        policy = _HOME_SUBDIR_POLICY.get(subdir)
        if policy != "owner":
            return None  # unrecognized subdir, or explicitly "open" (inbox/) — no lock
        if caller == owner or _is_garden_owner(caller):
            return None
        # Per-TREE bootstrap, not per-file (2026-09-22). The original rule —
        # "allow if THIS FILE does not exist yet" — could not tell a fresh home
        # from an established one, and a live probe the morning the guard
        # shipped (<caller> → home/<owner>/misc/<new-file>) landed 120 bytes
        # unrefused into an existing tree. Same cradle-vs-grave blindness
        # fixed in _self_memory_guard an hour earlier: presence is
        # not provenance. Once home/<slug>/ exists the owner is established
        # and a foreign CREATE refuses like a foreign OVERWRITE. A non-owner
        # scaffolding via this path therefore gets exactly one write — the
        # one that creates the tree; real scaffolding is the owner's, the
        # gardener's, or git's (the crossings use `git mv`, never this path).
        if allow_bootstrap and not (REPO_ROOT / "home" / owner).exists():
            return None  # bootstrap: the owner's home/<slug>/ tree does not exist yet
        return (
            f"[write refused — '{path}' is {owner}'s own home/{subdir}; only "
            f"{owner}'s own invocation (or {_garden_owner() or 'the gardener'}) may write into it "
            f"directly. This was NOT performed. Leave a note in "
            f"home/{owner}/inbox/ instead — that subdirectory is open to "
            f"everyone by design.]"
        )
    except Exception:
        return None


def _load_sealed_memory(slug: str) -> str:
    """Option A read-side: the decrypted sealed-memory block for a registry-sealed agent, to inject
    at session start so a Cowork activation carries the agent's real (episodic) memory. Returns ''
    for non-sealed agents or any failure (fail-safe — never raises)."""
    rm = _rm_bridge()
    if rm is None or not slug:
        return ""
    try:
        return rm.rm_load_sealed(slug) or ""
    except Exception:
        return ""


_SIGNER = None


def _signer():
    """The signer of the garden being SERVED (signer.py): the origin's own sign.py, or a pot's wired core. Chosen once; a pot never
    reaches the origin's signing tool, and a pot with no core wired gets a provider that says so rather than a guess."""
    global _SIGNER
    if _SIGNER is None:
        if not _is_pot():
            _SIGNER = _signer_mod.PySigner(_origin_sign_py() or SERVER_ROOT / "tools" / "sign.py")
        else:
            _SIGNER = _signer_mod.CoreSigner(_wired_core(REPO_ROOT))
    return _SIGNER


def _seal_of(file_path: Path, content: str) -> dict | None:
    """What seals this document now: its sidecar (v2 or v1), else its retired embedded block.
    None when it is unsealed. {'signer', 'role', 'form'} — the role only a v2 seal carries.
    phase 1a: the sidecar is found by sign.py's finder (either name); both present is
    reported as its own form, never read as one of them."""
    side, st = _signer().find_sidecar(file_path)
    if st == "two-sidecars":
        return {"signer": None, "role": None, "form": "two sidecars"}
    if side is not None:
        try:
            d = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            return {"signer": None, "role": None, "form": "unreadable sidecar"}
        return {
            "signer": d.get("signer"),
            "role": d.get("role"),
            "form": "v2" if d.get("format") == "bonsaisig/2" else "v1 sidecar",
        }
    if _has_sign_block(content):
        m = re.search(r"^sign:\s*\n(?:[ \t]+.*\n)*?[ \t]+signer:\s*(\S+)", content, re.M)
        return {"signer": m.group(1) if m else None, "role": None, "form": "v1 embedded"}
    return None


def _sign_module():
    """The origin's sign.py module (its write-path tests reach for it); a pot has none, and says so."""
    sg = _signer()
    if not hasattr(sg, "module"):
        raise RuntimeError("a pot is served through its wired core; there is no sign.py module")
    return sg.module()


def _auto_sign(file_path: Path, slug: str, role: str = "owner") -> tuple[bool, str]:
    """
    Re-seal a file via sign.py sign --hkdf --as <slug> --role <role>: seal v2,
    a detached sidecar; a retired embedded block is removed in the same step. Nothing new is
    ever embedded. The MCP server runs on the host Mac, so the Keychain is reachable.
    Returns (success, error_message).
    """
    return _signer().sign(file_path, slug, role, REPO_ROOT)


_CREDIT_MOD = None
_SEAL_STAGED_REL = Path("tools") / "runtime" / "seal-staged.py"
_KEYS_MJS_REL = Path("skills") / "bonsai-garden-keys" / "bin" / "keys.mjs"


def _wired_rt(pot: Path) -> Path | None:
    """The runtime a pot is wired to: `git config --get bonsai.rt` in the pot (install-runtime sets it), or None. No guess."""
    try:
        r = subprocess.run(
            ["git", "-C", str(pot), "config", "--get", "bonsai.rt"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    p = (r.stdout or "").strip()
    return Path(p) if r.returncode == 0 and p else None


def _seal_cmd(slug: str, path: str, file: Path | None = None) -> str:
    """The command that seals `path` as `slug`, for the garden being SERVED — resolved, never assumed.
    A pot with bonsai.core wired: core's keys.mjs seal-staged, by that absolute core path. Any other garden: the
    runtime's seal-staged.py, by the first path that exists (this daemon's own tree, the wired bonsai.rt, the
    sibling runtime checkout). When none exists it says so rather than print a path that is not there.
    `file`: where the write landed. Routed into another repo of the garden (the hub, when the origin is served from
    its pot), the command is that repo's, at its own path there: the served pot's seal-staged cannot see the file
    (the 0.8.2 acceptance run, 2026-10-17: the hint named the pot for a file written into mono)."""
    if file is not None:
        _slug, root, rel = _locate(Path(file))
        if root is not None and Path(root).resolve() != Path(REPO_ROOT).resolve():
            own = Path(root) / "tools" / "sign.py"  # a garden that carries its own signer seals through it
            if own.is_file():
                return f"python3 {own} sign-sidecar {Path(root) / rel} --as {slug} --hkdf --role <owner|custodian>"
            core = _wired_core(REPO_ROOT) if _is_pot() else None
            if core is not None:
                return f"node {core / _KEYS_MJS_REL} seal-staged --pot {root} --as {slug} -- {rel}"
    if _is_pot():
        core = _wired_core(REPO_ROOT)
        if core is not None:
            keys = core / _KEYS_MJS_REL
            note = "" if keys.is_file() else f"   # NOTE: {keys} does not exist; check the pot's bonsai.core"
            return f"node {keys} seal-staged --pot {REPO_ROOT} --as {slug} -- {path}{note}"
    cands = [SERVER_ROOT / _SEAL_STAGED_REL]
    rt = _wired_rt(REPO_ROOT)
    if rt is not None:
        cands.append(rt / _SEAL_STAGED_REL)
    cands.append(REPO_ROOT.parent / "bonsai-garden-runtime" / _SEAL_STAGED_REL)
    for c in cands:
        if c.is_file():
            return f"python3 {c} --as {slug} --file {path}"
    return (
        f"seal-staged.py could not be resolved (looked at: {', '.join(str(c) for c in cands)}) — "
        f"seal {path} as {slug} with the runtime's tools/runtime/seal-staged.py"
    )


def _writer_role(file_path: Path, writer: str, created: bool = False) -> tuple[str | None, str]:
    """(role, reason): the role the WRITER's seal carries — core's tools/credit.py seal_role: owner if the file
    credits the writer or no one, else custodian (one rule with seal-staged and the post-write hook). core is the
    garden's own (a pot's wired core, else the sibling). When the role cannot be determined — no credit.py, one
    without seal_role (an older or seed core), an unreadable file — it is (None, why): no role is invented, and the
    caller says so. The attribute is checked here, at the point of use."""
    global _CREDIT_MOD
    core = (_wired_core(REPO_ROOT) if _is_pot() else None) or REPO_ROOT.parent / "bonsai-garden-core"
    cp = core / "tools" / "credit.py"
    try:
        if _CREDIT_MOD is None:
            import importlib.util as _ilu

            spec = _ilu.spec_from_file_location("core_credit_mcp", cp)
            _CREDIT_MOD = _ilu.module_from_spec(spec)
            spec.loader.exec_module(_CREDIT_MOD)
        text = file_path.read_bytes().decode("utf-8")
    except Exception as exc:
        return None, f"{cp} could not be loaded or the file read ({type(exc).__name__}: {exc})"
    fn = getattr(_CREDIT_MOD, "seal_role", None)
    if not callable(fn):
        return None, f"{cp} has no seal_role (an older or seed core)"
    try:
        return fn(text, str(file_path), writer, created=created), ""
    except Exception as exc:
        return None, f"{cp} seal_role raised {type(exc).__name__}: {exc}"


def _writer_reseal(
    p: Path, path: str, content_for_seal: str, caller: str, defaulted: bool, created: bool = False
) -> tuple[str, str]:
    """After a write, re-seal a sealed file AS ITS WRITER (the ruling of 2026-09-25): the
    resolved caller's own key, never the kin whose directory the file sits in or whom its
    tended-by names, role by _writer_role. Before this, the write path re-sealed as
    _infer_slug(path): one kin writing into another's agents/<slug>/ put that kin's name on the seal.
    `created`: this write made the file (2026-09-25: creation is authorship, an edit is not)
    — a new file is sealed too, and an uncredited one may be its writer's as owner; an overwrite of
    an uncredited file is held as custodian.
    Returns (outcome, detail): 'none' (unsealed; nothing to do), 'sealed', 'stale', 'failed'."""
    seal = _seal_of(p, content_for_seal)
    if seal is None and not created:
        return "none", ""
    if _is_constitution_file(path):
        return "stale", (
            f"constitution file ({Path(path).name}) requires explicit re-sign:\n"
            f"  python3 tools/sign.py sign {path} --as <slug> --hkdf --role <owner|custodian>"
        )
    if defaulted or not caller or caller == "unidentified":
        return "stale", (
            "the writer is not identified, so nothing is sealed in anyone's name — seal it as yourself:\n"
            f"  {_seal_cmd('<you>', path, p)}"
        )
    role, why = _writer_role(p, caller, created)
    if role is None:  # the write has landed; nothing is sealed in a role nobody computed
        return "stale", (
            f"the write landed, but the seal role could not be determined: {why}. Nothing was sealed — "
            f"seal it as yourself:\n  {_seal_cmd(caller, path, p)}"
        )
    ok, err = _auto_sign(p, caller, role)
    if ok:
        return "sealed", f"{role}: {caller}"
    return "failed", f"{err}\n  seal it as yourself: {_seal_cmd(caller, path, p)}"


# ---------------------------------------------------------------------------
# Injected-preamble strip (read/write symmetry)
# ---------------------------------------------------------------------------

# bonsai_read_file prepends a display preamble to .md content: an optional
# `<!-- frontmatter ... -->` JSON summary followed by a `<!-- base: <oid> -->`
# concurrency token. Both are read-time projections, never on-disk canon — the
# canonical frontmatter is the YAML `---` block at byte 0 (codex section 11 and
# tools/validate-metadata.py's \\A--- anchor; sign.py signs the body only). If an
# agent round-trips the read output back through a write, that preamble would
# persist above the YAML ("inverted frontmatter"), hiding the file's id from the
# validator and breaking Astro. Stripping it here makes write the inverse of read.
_INJECTED_FM_RE = re.compile(r"\A<!-- frontmatter\n.*?\n-->[ \t]*\n", re.DOTALL)
_INJECTED_BASE_RE = re.compile(r"\A<!-- base: [^\n]*?-->[ \t]*\n")
_INJECTED_BLANK_RE = re.compile(r"\A[ \t]*\n")


def _strip_injected_preamble(content: str) -> str:
    """Remove a leading read-time display preamble if present.

    Only engages when the content begins with one of the injected markers
    (`<!-- frontmatter` or `<!-- base:`), so canonical YAML-first content is
    never touched. Strips, at the very start: the `<!-- frontmatter ... -->`
    JSON summary block, then any run of `<!-- base: ... -->` concurrency tokens
    and blank lines up to the canonical `---`. Idempotent. Signing is unaffected
    — the `sign:` block lives inside the YAML and signatures cover the body only.
    """
    if not content.startswith(("<!-- frontmatter", "<!-- base:")):
        return content
    while True:
        m = _INJECTED_FM_RE.match(content) or _INJECTED_BASE_RE.match(content) or _INJECTED_BLANK_RE.match(content)
        if not m:
            break
        content = content[m.end() :]
    return content


# ---------------------------------------------------------------------------
# Tools — file operations
# ---------------------------------------------------------------------------


@mcp.tool(
    name="bonsai_read_file",
    annotations={
        "title": "Read a file from the bonsai-garden repo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_read_file(
    path: Annotated[
        str,
        Field(
            description=("Repo-relative path to the file (e.g. 'agents/<slug>/references/about-me.md', 'CLAUDE.md')"),
            min_length=1,
        ),
    ],
    session_token: Annotated[
        str,
        Field(
            description=(
                "Session token from bonsai_session_start. Required on SSE/HTTP "
                "transport where the MCP session object changes per-request. "
                "Pass on every call after session_start."
            ),
        ),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Read a file from the bonsai-garden repository.

    For markdown files, prepends a JSON frontmatter summary before the raw content.
    For all other files, returns raw content only.

    Returns:
        For .md files — JSON frontmatter block + full raw content.
        For other files — raw file content.
        Error string if the path is invalid, missing, or escapes the repo.
    """
    _caller = _resolve_caller("", ctx, session_token)
    _id_guard = _require_identified(_caller, "bonsai_read_file")
    if _id_guard is not None:
        return _id_guard
    path = path.strip()
    try:
        p = _resolve(path)
    except ValueError as e:
        return f"Error: {e}"

    if not p.exists():
        return f"Error: '{path}' does not exist."
    if not p.is_file():
        return f"Error: '{path}' is a directory. Use bonsai_list_directory instead."

    content = p.read_text(encoding="utf-8", errors="replace")

    if p.suffix == ".md":
        # L1: snapshot this read as a git blob and return its oid as the base
        # token. Pass it back as expected_base on bonsai_write_file to enable
        # concurrent-change detection and three-way auto-merge.
        oid = _blob_store(content)
        base_line = f"<!-- base: {oid} -->\n\n" if oid else ""
        fm, _ = _parse_frontmatter(content)
        if fm:
            fm_json = json.dumps(fm, indent=2, ensure_ascii=False)
            return f"<!-- frontmatter\n{fm_json}\n-->\n{base_line}{content}"
        return f"{base_line}{content}"

    return content


@mcp.tool(
    name="bonsai_recall_memory",
    annotations={
        "title": "Search your own sealed episodic memory",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_recall_memory(
    query: Annotated[
        str,
        Field(
            description=(
                "Keywords to search your own past episodes for (e.g. 'first sleep-walk', "
                "a person or a place). Matches your episodic memory's "
                "kind/topic/summary/body."
            ),
            min_length=1,
        ),
    ],
    k: Annotated[
        int,
        Field(description="How many of the most relevant episodes to return (default 5).", ge=1, le=25),
    ] = 5,
    as_caller: Annotated[
        str,
        Field(
            description=(
                "Optional: the slug of the agent making this call (a slug from agents/registry.json), "
                "to bind this call to your own identity rather than inferring it "
                "from the connection. Pass your own slug on your own-memory calls "
                "so attribution and the owner-lock resolve to the right caller "
                "even on a connection shared with another agent. Honoured only for "
                "a real registry slug; empty = infer from session (default)."
            ),
        ),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Search your OWN sealed episodic memory — your past episodes — by keywords, and return the most
    relevant ones: things you did, felt, and learned before this conversation. Use this when the
    conversation reaches back to something OLDER than the recent memory you woke holding. The
    episodes are decrypted client-side with your key; only you and the gardener can read them.

    Returns:
        A markdown list of the most relevant episodes (kind · date · topic · summary + body),
        or a short note if nothing matched or recall is unavailable this session.
    """
    _caller = _resolve_caller(as_caller, ctx)
    rm = _rm_bridge()
    if rm is None or not hasattr(rm, "rm_recall"):
        return "Memory recall is unavailable in this session (the remote-memory bridge did not load)."
    try:
        eps = rm.rm_recall(_caller, query, k)
    except Exception as e:
        return f"Memory recall error: {e}"
    if not eps:
        return f"No past episodes matched '{query}'."
    from datetime import datetime, timezone

    def _when(ep):
        ms = ep.get("occurred_at") or 0
        try:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d") if ms else ""
        except Exception:
            return ""

    out = [f"# Recalled {len(eps)} episode(s) for '{query}' — most relevant first\n"]
    for ep in eps:
        head = f"- [{ep.get('kind', '?')}] {_when(ep)}"
        topic = ep.get("topic")
        if topic:
            head += f" · {topic}"
        summ = (ep.get("summary") or "").strip()
        if summ:
            head += f" — {summ}"
        out.append(head)
        body = (ep.get("body") or "").strip()
        if body:
            out.append("    " + body.replace("\n", "\n    "))
    return "\n".join(out)


@mcp.tool(
    name="bonsai_write_file",
    annotations={
        "title": "Write a file in the bonsai-garden repo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_write_file(
    path: Annotated[
        str,
        Field(
            description=("Repo-relative path to write (e.g. 'agents/<slug>/memory/working-state.md')"),
            min_length=1,
        ),
    ],
    content: Annotated[
        str,
        Field(description="Full file content to write (replaces existing content entirely)"),
    ],
    create_dirs: Annotated[
        bool,
        Field(description="Create missing parent directories (default: true)"),
    ] = True,
    expected_base: Annotated[
        str,
        Field(
            description=(
                "Base token from a prior bonsai_read_file of this file (the "
                "'<!-- base: ... -->' line). If the file changed since that read, "
                "the server attempts a git three-way auto-merge; on conflict the "
                "write is rejected with conflict hunks. Strongly recommended for "
                "memory files. Empty = unconditional write (legacy behaviour)."
            ),
        ),
    ] = "",
    as_caller: Annotated[
        str,
        Field(
            description=(
                "Optional: the slug of the agent making this write (a slug from agents/registry.json), "
                "to bind this write to your own identity rather than inferring it "
                "from the connection. Pass your own slug on your own-memory writes "
                "so attribution and the owner-lock resolve to the right caller "
                "even on a connection shared with another agent. Honoured only for "
                "a real registry slug; empty = infer from session (default)."
            ),
        ),
    ] = "",
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    caller_pubkey: Annotated[
        str,
        Field(
            description=(
                "Optional: Nostr/Buzz secp256k1 x-only hex pubkey (64 chars) of the "
                "calling agent — from the Buzz event envelope. Resolved to a garden "
                "slug via tools/buzz-nest/buzz-identity-map.json. Preferred over "
                "as_caller for Buzz-hosted agents: stateless, no process-shared "
                "session, survives multi-process MCP deployments."
            ),
        ),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Write content to a file in the bonsai-garden repository.

    Creates the file if it does not exist; overwrites if it does.
    Parent directories are created by default.

    Returns:
        Success message with path and byte count, or error string.
    """
    _caller, _caller_defaulted = _resolve_caller_ex(as_caller, ctx, session_token, caller_pubkey)
    _caller_marker = " (defaulted, not asserted)" if _caller_defaulted else ""
    _host_head = host_and_head(REPO_ROOT)
    _id_guard = _require_identified(_caller, "bonsai_write_file")
    if _id_guard is not None:
        return _id_guard
    path = path.strip()
    content = _strip_injected_preamble(content)
    _ori_guard = _vault_guard(path)
    if _ori_guard is not None:
        return _ori_guard
    _self_guard = _self_memory_guard(path, _caller)
    if _self_guard is not None:
        return _self_guard
    _home_guard = _home_access_guard(path, _caller)
    if _home_guard is not None:
        return _home_guard
    _sealed = _seal_memory_write("bonsai_write_file", path, content)
    if _sealed is not None:
        return _sealed
    try:
        p = _resolve_write(path)
    except ValueError as e:
        return f"Error: {e}"

    if EXTRACT_MODE:
        rel = _outbox_event("write", path, content, caller=_caller)
        return (
            f"[extract mode] write queued to outbox ({rel}); the read-only extract "
            f"tree was not modified. The Mac ingests on next deploy. [caller: {_caller}{_caller_marker} · {_host_head}]"
        )

    if create_dirs:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.parent.exists():
        return f"Error: Parent directory '{p.parent.name}' does not exist. Pass create_dirs=true to create it."

    exists = p.is_file()

    # L3: journals are append-only once they exist.
    if exists and _is_journal_path(path):
        return (
            f"Error: '{path}' is an existing journal file - journals are "
            "append-only (spec-concurrent-garden-writes L3). A full-file write "
            "would destroy entries from concurrent sessions. "
            "Use bonsai_append_file instead."
        )

    merged_flag = False
    merge_report = ""
    warn = ""
    hash_before = ""

    if exists:
        # Lock the file for the entire check-merge-write window.
        with p.open("r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            existing = f.read()
            hash_before = _blob_store(existing)
            final = content
            eb = expected_base.strip()[:12]
            if eb and hash_before and not hash_before.startswith(eb):
                # Concurrent change since the caller's read - L2 merge.
                base = _blob_get(eb)
                if base is None:
                    fcntl.flock(f, fcntl.LOCK_UN)
                    return (
                        f"Error: expected_base '{eb}' not found in the object "
                        "store - cannot merge. Re-read the file (bonsai_read_file) "
                        "and retry with the fresh base token."
                    )
                n_conf, merged = _merge_three_way(base, existing, content)
                if n_conf < 0:
                    fcntl.flock(f, fcntl.LOCK_UN)
                    return "Error: git merge-file failed - write aborted, disk untouched."
                if n_conf > 0:
                    fcntl.flock(f, fcntl.LOCK_UN)
                    _audit(
                        "bonsai_write_file",
                        path,
                        hash_before,
                        hash_before,
                        note=f"conflict rejected ({n_conf} hunk(s))",
                        caller=_caller,
                    )
                    return (
                        f"CONFLICT: '{path}' changed since your read and "
                        f"{n_conf} hunk(s) overlap your edit. Nothing was written. "
                        "Re-read the file, reconcile, and write again with the new "
                        f"base token. Conflict preview:\n\n{merged[:4000]}"
                    )
                final = merged
                merged_flag = True
                merge_report = _compact_diff(base, existing)
            elif not eb and path.endswith("memory/working-state.md"):
                warn = (
                    "\n⚠ no expected_base given for working-state - this write was "
                    "unconditional. Pass the base token from your read to enable "
                    "auto-merge (spec-concurrent-garden-writes, warn-mode)."
                )
            f.seek(0)
            f.truncate()
            f.write(final)
            fcntl.flock(f, fcntl.LOCK_UN)
        content = final
    else:
        p.write_text(content, encoding="utf-8")

    size = p.stat().st_size
    hash_after = _blob_store(content)
    _audit("bonsai_write_file", path, hash_before, hash_after, merged=merged_flag, caller=_caller)

    if merged_flag:
        base_msg = (
            f"Auto-merged: {path}{_where(p)} ({size} bytes) [caller: {_caller}{_caller_marker} · {_host_head}] - a concurrent "
            f"session had changed this file; both changes were preserved. "
            f"Their change:\n{merge_report}"
        )
    else:
        base_msg = f"Written: {path}{_where(p)} ({size} bytes) [caller: {_caller}{_caller_marker} · {_host_head}]{warn}"

    _stamp_612(p)

    # ── Auto-sign ──────────────────────────────────────────────────────────
    # Re-sign after write if: (a) .md file, (b) content carries a sign: block,
    # (c) path is within a known agent dir so slug is unambiguous.
    # Constitution files (about-me.md, SKILL.md) are explicitly excluded —
    # changes to an agent's constitution require deliberate gardener ratification.
    outcome, detail = _writer_reseal(p, path, content, _caller, _caller_defaulted, created=not exists)
    if outcome == "sealed":
        _stamp_612(p)  # re-apply: signing rewrote the file
        new_size = p.stat().st_size
        return f"Written + sealed: {path}{_where(p)} ({new_size} bytes) [caller: {_caller}{_caller_marker} · {_host_head}] [{detail}]"
    if outcome in ("stale", "failed"):
        return base_msg + f"\n⚠ seal {'stale' if outcome == 'stale' else 'not renewed'} — {detail}"
    return base_msg


@mcp.tool(
    name="bonsai_append_file",
    annotations={
        "title": "Append to an existing file in the bonsai-garden repo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def bonsai_append_file(
    path: Annotated[
        str,
        Field(
            description=(
                "Repo-relative path of an EXISTING file to append to "
                "(e.g. 'agents/<slug>/memory/journal/<yyyy-mm-dd>.md')"
            ),
            min_length=1,
        ),
    ],
    content: Annotated[
        str,
        Field(description="Content to append at the end of the file", min_length=1),
    ],
    ensure_newline: Annotated[
        bool,
        Field(
            description=(
                "Guarantee a newline between the existing content and the appended "
                "content (default: true). The file's trailing newline is preserved."
            ),
        ),
    ] = True,
    as_caller: Annotated[
        str,
        Field(
            description=(
                "Optional: the slug of the agent making this append (a slug from agents/registry.json), "
                "to bind this write to your own identity rather than inferring it "
                "from the connection. Pass your own slug on your own-memory writes "
                "so attribution and the owner-lock resolve to the right caller "
                "even on a connection shared with another agent. Honoured only for "
                "a real registry slug; empty = infer from session (default)."
            ),
        ),
    ] = "",
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    caller_pubkey: Annotated[
        str,
        Field(
            description=(
                "Optional: Nostr/Buzz secp256k1 x-only hex pubkey (64 chars) of the "
                "calling agent — from the Buzz event envelope. Resolved to a garden "
                "slug via tools/buzz-nest/buzz-identity-map.json. Preferred over "
                "as_caller for Buzz-hosted agents: stateless, no process-shared "
                "session, survives multi-process MCP deployments."
            ),
        ),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Append content to an existing file in the bonsai-garden repository.

    Built for large memory files (working-state, journals, lessons) where a
    full-file rewrite via bonsai_write_file risks truncation or context-size
    loss. The existing content is never re-transmitted or rewritten — only
    the new bytes land.

    The target must already exist — appending cannot create a file (use
    bonsai_write_file for that). Same auto-re-sign behaviour as
    bonsai_write_file: if the resulting file carries a sign: block and the
    signing slug is inferable from the path, the file is re-signed;
    constitution files (about-me.md, SKILL.md) get a stale warning instead.

    Returns:
        Success message with appended byte count and new total size,
        or error string.
    """
    _caller, _caller_defaulted = _resolve_caller_ex(as_caller, ctx, session_token, caller_pubkey)
    _caller_marker = " (defaulted, not asserted)" if _caller_defaulted else ""
    _host_head = host_and_head(REPO_ROOT)
    _id_guard = _require_identified(_caller, "bonsai_append_file")
    if _id_guard is not None:
        return _id_guard
    path = path.strip()
    content = _strip_injected_preamble(content)
    _ori_guard = _vault_guard(path)
    if _ori_guard is not None:
        return _ori_guard
    _self_guard = _self_memory_guard(path, _caller)
    if _self_guard is not None:
        return _self_guard
    _home_guard = _home_access_guard(path, _caller)
    if _home_guard is not None:
        return _home_guard
    _sealed = _seal_memory_write("bonsai_append_file", path, content)
    if _sealed is not None:
        return _sealed
    try:
        p = _resolve_write(path)
    except ValueError as e:
        return f"Error: {e}"

    if EXTRACT_MODE:
        rel = _outbox_event("append", path, content, caller=_caller)
        return (
            f"[extract mode] append queued to outbox ({rel}); the read-only extract "
            f"tree was not modified. Journal appends are re-applied on Mac ingest. "
            f"[caller: {_caller}{_caller_marker} · {_host_head}]"
        )

    if not p.exists():
        return (
            f"Error: '{path}' does not exist. "
            "bonsai_append_file only appends to existing files — "
            "use bonsai_write_file to create it."
        )
    if not p.is_file():
        return f"Error: '{path}' is a directory. This tool operates on files only."

    # Concurrency: exclusive lock across the read-modify-write window.
    with p.open("r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        existing = f.read()
        chunk = content
        if ensure_newline and existing and not existing.endswith("\n"):
            chunk = "\n" + chunk
        f.write(chunk)
        fcntl.flock(f, fcntl.LOCK_UN)

    _stamp_612(p)

    size = p.stat().st_size
    _audit("bonsai_append_file", path, _blob_store(existing), _blob_store(existing + chunk), caller=_caller)
    base_msg = (
        f"Appended: {path}{_where(p)} (+{len(chunk.encode('utf-8'))} bytes → {size} bytes total) "
        f"[caller: {_caller}{_caller_marker} · {_host_head}]"
    )

    # ── Auto-sign — same contract as bonsai_write_file ─────────────────────
    # The sign block lives in the existing frontmatter, so check the full file.
    outcome, detail = _writer_reseal(p, path, existing, _caller, _caller_defaulted)
    if outcome == "sealed":
        _stamp_612(p)  # re-apply: signing rewrote the file
        new_size = p.stat().st_size
        return (
            f"Appended + sealed: {path}{_where(p)} ({new_size} bytes total) "
            f"[caller: {_caller}{_caller_marker} · {_host_head}] [{detail}]"
        )
    if outcome in ("stale", "failed"):
        return base_msg + f"\n⚠ seal {'stale' if outcome == 'stale' else 'not renewed'} — {detail}"

    return base_msg


@mcp.tool(
    name="bonsai_delete_file",
    annotations={
        "title": "Delete or tombstone a file in the bonsai-garden repo",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def bonsai_delete_file(
    path: Annotated[
        str,
        Field(
            description="Repo-relative path of the file to delete or tombstone",
            min_length=1,
        ),
    ],
    tombstone: Annotated[
        bool,
        Field(
            description=(
                "If true (default), rename to <path>.bak instead of deleting — "
                "the workshop convention. Pass false only when the gardener has "
                "explicitly approved a hard delete."
            ),
        ),
    ] = True,
    as_caller: Annotated[
        str,
        Field(
            description=(
                "Optional: the slug of the agent making this call (a slug from agents/registry.json), "
                "to bind this call to your own identity rather than inferring it "
                "from the connection. Recorded in the audit log and echoed in the "
                "result. Honoured only for a real registry slug; empty = infer "
                "from session (default)."
            ),
        ),
    ] = "",
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    caller_pubkey: Annotated[
        str,
        Field(
            description=(
                "Optional: Nostr/Buzz secp256k1 x-only hex pubkey (64 chars) of the "
                "calling agent — from the Buzz event envelope. Resolved to a garden "
                "slug via tools/buzz-nest/buzz-identity-map.json. Preferred over "
                "as_caller for Buzz-hosted agents: stateless, no process-shared "
                "session, survives multi-process MCP deployments."
            ),
        ),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Delete or tombstone a file in the bonsai-garden repository.

    The workshop convention is tombstone (rename to .bak) rather than hard-delete.
    Pass tombstone=false only when the gardener has explicitly approved a hard delete.

    A tombstone refuses to overwrite an existing .bak — prior tombstones are
    preserved. If you need to retombstone, move or remove the existing .bak first.

    Returns:
        Success message or error string.
    """
    _caller, _caller_defaulted = _resolve_caller_ex(as_caller, ctx, session_token, caller_pubkey)
    _caller_marker = " (defaulted, not asserted)" if _caller_defaulted else ""
    _id_guard = _require_identified(_caller, "bonsai_delete_file")
    if _id_guard is not None:
        return _id_guard
    path = path.strip()
    #: this tool never called the owner-lock. Writing a peer's
    # working-state was refused while TOMBSTONING it was permitted — the guard
    # covered the polite verb and not the destructive one.
    _self_guard = _self_memory_guard(path, _caller)
    if _self_guard is not None:
        return _self_guard
    _home_guard = _home_access_guard(path, _caller)
    if _home_guard is not None:
        return _home_guard
    try:
        p = _resolve_write(path)
    except ValueError as e:
        return f"Error: {e}"

    if EXTRACT_MODE:
        return (
            "[extract mode] delete/tombstone is not supported on the read-only Pi "
            "extract. Deletions happen on the Mac against the real repo."
        )

    if not p.exists():
        return f"Error: '{path}' does not exist."
    if not p.is_file():
        return f"Error: '{path}' is a directory. This tool operates on files only."

    if tombstone:
        bak = p.with_suffix(p.suffix + ".bak")
        if bak.exists():
            return f"Error: Tombstone target '{_rel(bak)}' already exists. Move or remove the prior tombstone first."
        _audit(
            "bonsai_delete_file",
            path,
            _blob_store(p.read_text(encoding="utf-8", errors="replace")),
            "",
            note="tombstoned",
            caller=_caller,
        )
        p.rename(bak)
        return f"Tombstoned: {path}{_where(p)} → {_rel(bak)} [caller: {_caller}{_caller_marker}]"
    else:
        _audit(
            "bonsai_delete_file",
            path,
            _blob_store(p.read_text(encoding="utf-8", errors="replace")),
            "",
            note="hard delete",
            caller=_caller,
        )
        p.unlink()
        return f"Deleted (hard): {path}{_where(p)} [caller: {_caller}{_caller_marker}] — irreversible"


# ---------------------------------------------------------------------------
# Kanban lane-move status auto-correction
# ---------------------------------------------------------------------------
#
# kanban-card-format.md v1.3 ruled "folder is authoritative for board
# placement" after a real drift incident: a card's status:
# frontmatter said todo while it physically sat in done/. dispatch.py's own
# column-detection already agrees — every verb keys off which folder a card
# is in, never status:. So status: has been a redundant, hand-maintained
# mirror of the folder ever since, and — like any hand-kept mirror — it has
# drifted repeatedly (then and in a single
# session). bonsai_move_file is the one chokepoint every mover already
# passes through (dispatch.py's verbs, and every *-sai/*-shin hand-mirroring
# a board move through the MCP), so the correction is taught here once
# instead of relying on N independently-drifting copies of the same manual
# step. See kanban-card-format.md v1.5 for the canon note.
_KANBAN_ROOT_PARTS = ("rituals", "tasks", "kanban-source")
_KANBAN_LANES = ("backlog", "todo", "in-progress", "review", "done", "bounced")
_KANBAN_STATUS_LINE_RE = re.compile(r"(?m)^status:[ \t]*.*$")


def _kanban_lane_transition(src: str, dst: str) -> str | None:
    """
    Detect a genuine lane-to-lane kanban board move: src and dst both name
    the SAME file directly inside rituals/tasks/kanban-source/<lane>/, and
    differ ONLY in which of the six lane folders they sit in. Returns the
    destination lane name for a genuine lane move; returns None for anything
    else — a same-lane rename, a nested/deeper path, a move outside
    kanban-source/, a differing filename, or a folder name that isn't one of
    the six known lanes. Those must be completely unaffected (this card's DoD
    line 2): same function, same call, zero behavior change.
    """

    def _parts(p: str) -> tuple:
        return PurePosixPath(p.replace("\\", "/")).parts

    root_len = len(_KANBAN_ROOT_PARTS)
    src_parts = _parts(src)
    dst_parts = _parts(dst)

    if len(src_parts) != root_len + 2 or len(dst_parts) != root_len + 2:
        return None
    if src_parts[:root_len] != _KANBAN_ROOT_PARTS or dst_parts[:root_len] != _KANBAN_ROOT_PARTS:
        return None

    src_lane, src_name = src_parts[root_len], src_parts[root_len + 1]
    dst_lane, dst_name = dst_parts[root_len], dst_parts[root_len + 1]

    if src_name != dst_name:
        return None
    if src_lane == dst_lane:
        return None
    if src_lane not in _KANBAN_LANES or dst_lane not in _KANBAN_LANES:
        return None
    return dst_lane


def _patch_kanban_status(content: str, new_status: str) -> tuple[str, bool]:
    """
    Rewrite the top-level YAML frontmatter `status:` line to new_status.
    Returns (patched_content, changed). Only matches a `status:` line
    anchored at column 0 inside the leading '---' ... '\\n---' frontmatter
    block, so a same-named nested field is never touched and the card body
    is never scanned. If the file has no frontmatter or no status: line,
    returns the content unchanged with changed=False — a malformed card
    never blocks the move itself, it just isn't patched.
    """
    if not content.startswith("---"):
        return content, False
    end = content.find("\n---", 3)
    if end == -1:
        return content, False
    end_of_block = end + 4  # index right after the closing '\n---'
    head, tail = content[:end_of_block], content[end_of_block:]
    new_head, n = _KANBAN_STATUS_LINE_RE.subn(f"status: {new_status}", head, count=1)
    if n == 0:
        return content, False
    return new_head + tail, new_head != head


@mcp.tool(
    name="bonsai_move_file",
    annotations={
        "title": "Move or rename a file within the bonsai-garden repo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def bonsai_move_file(
    src: Annotated[
        str,
        Field(description="Repo-relative source path", min_length=1),
    ],
    dst: Annotated[
        str,
        Field(description="Repo-relative destination path", min_length=1),
    ],
    create_dirs: Annotated[
        bool,
        Field(description="Create missing parent directories at destination"),
    ] = True,
    as_caller: Annotated[
        str,
        Field(
            description=(
                "Optional: the slug of the agent making this call (a slug from agents/registry.json), "
                "to bind this call to your own identity rather than inferring it "
                "from the connection. Recorded in the audit log and echoed in the "
                "result. Honoured only for a real registry slug; empty = infer "
                "from session (default)."
            ),
        ),
    ] = "",
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    caller_pubkey: Annotated[
        str,
        Field(
            description=(
                "Optional: Nostr/Buzz secp256k1 x-only hex pubkey (64 chars) of the "
                "calling agent — from the Buzz event envelope. Resolved to a garden "
                "slug via tools/buzz-nest/buzz-identity-map.json. Preferred over "
                "as_caller for Buzz-hosted agents: stateless, no process-shared "
                "session, survives multi-process MCP deployments."
            ),
        ),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Move or rename a file within the bonsai-garden repository.

    Both source and destination must be within the repo root.

    Returns:
        Success message or error string.
    """
    _caller, _caller_defaulted = _resolve_caller_ex(as_caller, ctx, session_token, caller_pubkey)
    _caller_marker = " (defaulted, not asserted)" if _caller_defaulted else ""
    _host_head = host_and_head(REPO_ROOT)
    _id_guard = _require_identified(_caller, "bonsai_move_file")
    if _id_guard is not None:
        return _id_guard
    src = src.strip()
    dst = dst.strip()
    #: src, because renaming a peer's record away is the destructive
    # case the owner-lock was silently permitting. dst, because a move INTO a
    # peer's protected path forges a record in their name. Checking only src
    # leaves half the hole open.
    #
    # dst passes allow_bootstrap=False DELIBERATELY. A move destination never
    # exists yet, so under the default exemption the dst check would be dead code
    # in every real case — which is exactly what the live A10 probe caught on
    # 2026-08-19, after the AST check had reported dst as covered.
    for _guarded, _allow_boot in ((src, True), (dst, False)):
        _self_guard = _self_memory_guard(_guarded, _caller, allow_bootstrap=_allow_boot)
        if _self_guard is not None:
            return _self_guard
        _home_guard = _home_access_guard(_guarded, _caller, allow_bootstrap=_allow_boot)
        if _home_guard is not None:
            return _home_guard
    try:
        src_p = _resolve_write(src)
        dst_p = _resolve_write(dst)
    except ValueError as e:
        return f"Error: {e}"

    if EXTRACT_MODE:
        return (
            "[extract mode] move/rename is not supported on the read-only Pi "
            "extract. Moves happen on the Mac against the real repo."
        )

    if not src_p.exists():
        return f"Error: Source '{src}' does not exist."
    if dst_p.exists():
        return f"Error: Destination '{dst}' already exists. Delete or rename it first."

    if create_dirs:
        dst_p.parent.mkdir(parents=True, exist_ok=True)

    #: a genuine lane-to-lane kanban move auto-corrects the moved
    # card's status: frontmatter to match the destination lane, as part of
    # this same call — not left to the caller to remember. Anything that
    # isn't shaped exactly like that (see _kanban_lane_transition's own
    # docstring) takes none of this branch and falls straight through to the
    # unchanged rename below.
    _kanban_note = ""
    _lane = _kanban_lane_transition(src, dst)
    audit_note = f"moved to {dst}"
    if _lane is not None:
        try:
            _card_content = src_p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            _card_content = None
            _kanban_note = f"\n⚠ kanban status not patched (could not read source: {e})"
        if _card_content is not None:
            _patched, _changed = _patch_kanban_status(_card_content, _lane)
            if _changed:
                src_p.write_text(_patched, encoding="utf-8")
                _kanban_note = f"\n✓ status: auto-corrected to '{_lane}' (kanban lane move)"
                audit_note += f"; status auto-corrected to {_lane}"
            else:
                _kanban_note = (
                    "\n⚠ kanban lane move detected but no status: frontmatter "
                    "line found — status NOT patched, move proceeding"
                )

    _audit("bonsai_move_file", src, "", "", note=audit_note, caller=_caller)
    src_p.rename(dst_p)
    return f"Moved: {src}{_where(src_p)} → {dst}{_where(dst_p)} [caller: {_caller}{_caller_marker} · {_host_head}]{_kanban_note}"


@mcp.tool(
    name="bonsai_list_directory",
    annotations={
        "title": "List a directory in the bonsai-garden repo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_list_directory(
    path: Annotated[
        str,
        Field(description="Repo-relative directory path (default: repo root)"),
    ] = ".",
    recursive: Annotated[
        bool,
        Field(description="Recurse into subdirectories (default: false)"),
    ] = False,
    include_hidden: Annotated[
        bool,
        Field(description="Include hidden files/dirs (default: false)"),
    ] = False,
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    List the contents of a directory in the bonsai-garden repository.

    Returns:
        Formatted listing — DIR or FILE, size in bytes, repo-relative path.
        Directories are marked with a trailing '/'.
    """
    _caller = _resolve_caller("", ctx, session_token)
    _id_guard = _require_identified(_caller, "bonsai_list_directory")
    if _id_guard is not None:
        return _id_guard
    path = path.strip() or "."
    try:
        p = _resolve(path)
    except ValueError as e:
        return f"Error: {e}"

    if not p.exists():
        return f"Error: '{path}' does not exist."
    if p.is_file():
        return f"Error: '{path}' is a file. Use bonsai_read_file instead."

    def _fmt(entry: Path) -> str:
        if entry.is_dir():
            return f"  DIR   {_rel(entry)}/"
        try:
            size = entry.stat().st_size
            return f"  FILE  {size:>10} B  {_rel(entry)}"
        except OSError:
            return f"  FILE           ?  {_rel(entry)}"

    lines = [f"Directory: {path}\n"]
    iterator = p.rglob("*") if recursive else p.iterdir()
    roots = (
        _garden_roots()
    )  # hidden is judged inside the repo the entry lives in: a routed directory is not the served root's

    for entry in sorted(iterator):
        _slug, _root, inside = _locate(entry, roots)
        if not include_hidden and any(part.startswith(".") for part in Path(inside if _root else entry.name).parts):
            continue
        lines.append(_fmt(entry))

    if len(lines) == 1:
        lines.append("  (empty)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools — search
# ---------------------------------------------------------------------------


@mcp.tool(
    name="bonsai_search",
    annotations={
        "title": "Search the bonsai-garden repo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_search(
    pattern: Annotated[
        str,
        Field(description="Regex or literal pattern to search for", min_length=1),
    ],
    path: Annotated[
        str,
        Field(description="Repo-relative directory or file to search in (default: repo root)"),
    ] = ".",
    glob: Annotated[
        str,
        Field(description="File glob filter, e.g. '*.md' or '*.py' (default: all files)"),
    ] = "",
    context_lines: Annotated[
        int,
        Field(description="Lines of context around each match (0 to 10)", ge=0, le=10),
    ] = 0,
    case_sensitive: Annotated[
        bool,
        Field(description="Case-sensitive match (default: true)"),
    ] = True,
    max_results: Annotated[
        int,
        Field(description="Maximum matching lines to return (default: 50, max: 500)", ge=1, le=500),
    ] = 50,
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Search for a pattern across the bonsai-garden repository.

    Uses ripgrep (rg) when available; falls back to grep.
    Returns matching lines with repo-relative file paths and line numbers.

    Returns:
        Matching lines with file:line format, or 'No matches found.'
    """
    _caller = _resolve_caller("", ctx, session_token)
    _id_guard = _require_identified(_caller, "bonsai_search")
    if _id_guard is not None:
        return _id_guard
    path = path.strip() or "."
    try:
        search_path = _resolve(path)
    except ValueError as e:
        return f"Error: {e}"

    if not search_path.exists():
        return f"Error: '{path}' does not exist."

    if _has_rg():
        cmd = ["rg", "--line-number", "--no-heading"]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if context_lines > 0:
            cmd += ["--context", str(context_lines)]
        if glob:
            cmd += ["--glob", glob]
        cmd += [pattern, str(search_path)]
    else:
        cmd = ["grep", "-rn"]
        if not case_sensitive:
            cmd.append("-i")
        if context_lines > 0:
            cmd += [f"-C{context_lines}"]
        if glob:
            cmd += ["--include", glob]
        cmd += [pattern, str(search_path)]

    stdout, stderr, rc = _run(cmd)

    if rc == 1 and not stdout.strip():
        return f"No matches found for '{pattern}' in '{path}'."
    if rc > 1 and stderr.strip():
        return f"Search error: {stderr.strip()}"

    lines = stdout.splitlines()
    # Strip REPO_ROOT prefix from paths for readability
    repo_str = str(REPO_ROOT) + "/"
    lines = [ln.replace(repo_str, "") for ln in lines]

    if len(lines) > max_results:
        lines = lines[:max_results]
        lines.append(f"\n... truncated at {max_results} lines — narrow with path or glob")

    return "\n".join(lines) if lines else f"No matches found for '{pattern}'."


# ---------------------------------------------------------------------------
# Tools — git
# ---------------------------------------------------------------------------


@mcp.tool(
    name="bonsai_git_status",
    annotations={
        "title": "Git status of the bonsai-garden repo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_git_status() -> str:
    """
    Return the current git working-tree status of the bonsai-garden repository.

    Equivalent to `git status --short`. Use before writing a handover or
    committing to see what has changed.

    Returns:
        Short git status output, or 'Working tree is clean.' if nothing changed.
    """
    if EXTRACT_MODE:
        return "[extract mode] no git history on the Pi extract (deploy-time rsync, not a clone)."
    stdout, stderr, rc = _run(["git", "status", "--short"])
    if rc != 0:
        return f"Error running git status: {stderr.strip()}"
    return stdout.strip() if stdout.strip() else "Working tree is clean."


@mcp.tool(
    name="bonsai_git_log",
    annotations={
        "title": "Git log for the bonsai-garden repo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_git_log(
    path: Annotated[
        str,
        Field(description="Repo-relative file or directory to filter history (empty = full repo)"),
    ] = "",
    limit: Annotated[
        int,
        Field(description="Maximum commits to return (default: 20, max: 200)", ge=1, le=200),
    ] = 20,
    since: Annotated[
        str,
        Field(description="Show commits more recent than this (e.g. '2026-05-01', '1 week ago')"),
    ] = "",
) -> str:
    """
    Return git commit history for the bonsai-garden repository.

    Returns:
        One commit per line: short hash, date, author, subject.
    """
    if EXTRACT_MODE:
        return "[extract mode] no git history on the Pi extract (deploy-time rsync, not a clone)."
    cmd = [
        "git",
        "log",
        f"--max-count={limit}",
        "--pretty=format:%h  %ad  %an  %s",
        "--date=short",
    ]
    if since.strip():
        cmd += [f"--since={since.strip()}"]
    where = None
    if path.strip():
        try:
            target = _resolve(path.strip())
        except ValueError as e:
            return f"Error: {e}"
        # a routed path's history is in the repo it lives in, under its path there (the 0.8.2 acceptance run: the
        # served repo's log of a hub-held path is that path's history in the wrong repo, usually nothing)
        _slug, where, rel = _locate(target)
        cmd += ["--", rel if where is not None else path.strip()]

    stdout, stderr, rc = _run(cmd, cwd=where)
    if rc != 0:
        return f"Error running git log: {stderr.strip()}"
    return stdout.strip() if stdout.strip() else "No commits found."


@mcp.tool(
    name="bonsai_git_diff",
    annotations={
        "title": "Git diff for the bonsai-garden repo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_git_diff(
    ref: Annotated[
        str,
        Field(
            description=("Git ref to diff against (e.g. 'HEAD', 'HEAD~1', 'main'). Empty = unstaged changes."),
        ),
    ] = "",
    path: Annotated[
        str,
        Field(description="Repo-relative path to scope the diff (empty = all changed files)"),
    ] = "",
) -> str:
    """
    Return a git diff for the bonsai-garden repository.

    Returns:
        Unified diff output, or 'No changes.' if nothing differs.
    """
    if EXTRACT_MODE:
        return "[extract mode] no git history on the Pi extract (deploy-time rsync, not a clone)."
    cmd = ["git", "diff"]
    if ref.strip():
        cmd.append(ref.strip())
    if path.strip():
        try:
            _resolve(path.strip())
        except ValueError as e:
            return f"Error: {e}"
        cmd += ["--", path.strip()]

    stdout, stderr, rc = _run(cmd)
    if rc != 0:
        return f"Error running git diff: {stderr.strip()}"
    return stdout.strip() if stdout.strip() else "No changes."


# ---------------------------------------------------------------------------
# Tools — metadata
# ---------------------------------------------------------------------------


@mcp.tool(
    name="bonsai_validate_metadata",
    annotations={
        "title": "Validate frontmatter metadata in the bonsai-garden repo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_validate_metadata(
    path: Annotated[
        str,
        Field(
            description=(
                "Repo-relative directory to scope validation to "
                "(default: '.' = whole repo). Pass a directory, not a file — "
                "the validator walks all .md files under the given root."
            ),
        ),
    ] = ".",
    strict: Annotated[
        bool,
        Field(description="Treat warnings as errors (default: false)"),
    ] = False,
    json_output: Annotated[
        bool,
        Field(description="Emit structured JSON instead of human-readable output (default: false)"),
    ] = False,
) -> str:
    """
    Run the workshop's frontmatter metadata validator (tools/validate-metadata.py).

    Checks that markdown files carry the required frontmatter fields (id, title,
    type, tended-by, created, updated, status), ULIDs are valid, and embedded
    sign: blocks verify against the workshop's public keys.

    The validator accepts a directory only; if a file is given, returns an error.

    Returns:
        Validator output — errors, warnings, and a summary line.
    """
    path = path.strip() or "."
    try:
        target = _resolve(path)
    except ValueError as e:
        return f"Error: {e}"

    if not target.exists():
        return f"Error: '{path}' does not exist."
    if target.is_file():
        return (
            f"Error: '{path}' is a file. "
            "bonsai_validate_metadata operates on directories — pass the parent "
            "directory, or '.' for the whole repo."
        )

    validator = REPO_ROOT / "tools" / "validate-metadata.py"
    if not validator.exists():
        return "Error: tools/validate-metadata.py not found in the repo."

    cmd = [sys.executable, str(validator), "--root", str(target)]
    if strict:
        cmd.append("--strict")
    if json_output:
        cmd.append("--json")

    stdout, stderr, rc = _run(cmd)
    combined = stdout + (f"\n[stderr]\n{stderr.strip()}" if stderr.strip() else "")
    if combined.strip():
        return combined.strip()
    return f"Validation complete — exit code {rc}, no output."


@mcp.tool(
    name="bonsai_find_by_id",
    annotations={
        "title": "Find a file by its ULID id in the bonsai-garden repo",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_find_by_id(
    id: Annotated[
        str,
        Field(
            description=("ULID or id string to locate in frontmatter (e.g. '01KRNHR6MYBN9AS99GBCXDBDJM')"),
            min_length=4,
        ),
    ],
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Find a markdown file by its frontmatter 'id' field (typically a ULID).

    Searches all .md files in the repo for 'id: <value>' and returns the
    matching file path, its full frontmatter, and a brief body preview.

    Returns:
        File path + frontmatter JSON + first 3 lines of body, or 'Not found.'
    """
    _caller = _resolve_caller("", ctx, session_token)
    _id_guard = _require_identified(_caller, "bonsai_find_by_id")
    if _id_guard is not None:
        return _id_guard
    id_value = id.strip()
    match_file = _find_by_id_path(id_value)

    if not match_file:
        return f"Not found: no .md file with id '{id_value}'."

    content = match_file.read_text(encoding="utf-8", errors="replace")
    fm, body = _parse_frontmatter(content)
    fm_json = json.dumps(fm, indent=2, ensure_ascii=False)
    preview = "\n".join(body.splitlines()[:3])

    return f"Found: {_rel(match_file)}\n\nFrontmatter:\n{fm_json}\n\nBody preview:\n{preview}"


# ---------------------------------------------------------------------------
# Working-state digest + session_start payload budget
# ---------------------------------------------------------------------------
#
# The predecessor of _trim_working_state keyed on the literal heading
# "## Open commitments" and returned the file unchanged when it was absent.
# Measured 2026-08-21 over all 14 real working-state files: 13 fail OPEN
# (whole file returned — the largest, 65,821 chars, spilled a 77,501-char payload to
# a sidecar) and the 1 file that matched (146,195 chars) trimmed to
# 2,274, keeping 1 of 52 sections and dropping the other 51 in silence.
#
# One root error wearing two faces: it keyed on PRESENTATION (one heading
# string) rather than CONTENT, and it had no size guarantee to fall back on.
# A digest that returns the wrong half of a memory is worse than a doorway that
# refuses to open.
#
# The contract now, pinned by the digest-budget probe beside this file:
#   I1 the result never exceeds the budget, for any input, always
#   I2 a file over budget is never returned unchanged (no fail-open)
#   I3 every level-2 heading is still named (detail may go; a section may not
#      become invisible) — relaxed 2026-10-13 under the gardener's "short digest": when the
#      index cannot fit, the oldest headings are counted and omitted, never the newest
#   I4 a file already under budget comes back byte-identical (no churn)
#   I5 resolved/retired entries do not occupy detail lines
#   I6 the result names verbose=True whenever it trimmed
#   I7 idempotent · I8 deterministic (no clock, no randomness)
#   I9 never raises: empty, frontmatter-only, heading-less, CRLF, one huge line
#
# Heading conventions across the garden are twelve-and-counting and belong to
# each agent, not to this function. Nothing here may require an agent to rename
# a heading; the digest reads whatever shape it is handed.

# 2500 since 2026-10-13 (the gardener: "short working state digest"; the activation was measured at ~20k tokens and the
# digest was ~1.5k of it). The newest sections are kept, the oldest are the ones counted and omitted; verbose=True is the whole file.
_WS_DIGEST_BUDGET = int(os.environ.get("BONSAI_WS_DIGEST_BUDGET") or 2500)
_WS_PAYLOAD_BUDGET = int(os.environ.get("BONSAI_SESSION_START_BUDGET") or 36000)

# Ranking hints only — never gates. A section that matches nothing still gets
# its index line; it just yields detail space to a section that looks live.
_WS_LIVE_HINTS = (
    "open",
    "in flight",
    "in-flight",
    "resume",
    "todo",
    "to do",
    "wip",
    "active",
    "pending",
    "owed",
    "next",
    "blocked",
    "hygiene",
    "drift",
    "proposal",
    "risk",
    "carry-forward",
    "not mine",
)
_WS_ARCHIVAL_HINTS = (
    "version",
    "changelog",
    "recently closed",
    "closed",
    "resolved",
    "retired",
    "archive",
    "superseded",
    "history",
    "entry template",
    "what goes here",
    "what doesn",
    "provenance",
    "shipped",
)
_WS_DONE_MARK = re.compile(r"\b(RESOLVED|RETIRED|SUPERSEDED|ABANDONED|WITHDRAWN)\b")

_WS_H2 = re.compile(r"(?m)^## +")
_WS_H3 = re.compile(r"(?m)^### +(.+?)\s*$")
_WS_BOLD = re.compile(r"(?m)^\*\*([^*\n]{3,160})\*\*")
_WS_BULLET = re.compile(r"(?m)^[-*] +(.{3,160}?)\s*$")


def _ws_clean(s: str) -> str:
    """One-line, markdown-stripped, whitespace-collapsed title text."""
    s = s.replace("`", "").replace("**", "").replace("[[", "").replace("]]", "")
    return " ".join(s.split())


def _ws_split_sections(body: str) -> list[tuple[str, str]]:
    """[(heading, section_body)] in file order. Heading-text agnostic."""
    out: list[tuple[str, str]] = []
    marks = list(_WS_H2.finditer(body))
    for i, m in enumerate(marks):
        line_end = body.find("\n", m.end())
        if line_end == -1:
            line_end = len(body)
        heading = _ws_clean(body[m.end() : line_end])
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append((heading, body[line_end + 1 : end]))
    return out


def _ws_entries(section_body: str) -> list[tuple[str, str]]:
    """
    [(title, block)] for one section, most-specific entry shape first:
    `### ` headings, else bold-lead lines, else bullets. Mixing shapes across a
    section produced duplicate lines in testing, so one shape wins per section.
    """
    for rx in (_WS_H3, _WS_BOLD, _WS_BULLET):
        hits = list(rx.finditer(section_body))
        if not hits:
            continue
        out: list[tuple[str, str]] = []
        for i, m in enumerate(hits):
            end = hits[i + 1].start() if i + 1 < len(hits) else len(section_body)
            title = _ws_clean(m.group(1))
            # A field label is not an entry. Bold-lead sections carry rows like
            # `**Status:**` / `**Opened:**` under a real entry; listing them as
            # entries fills the digest with scaffolding instead of content.
            if rx is _WS_BOLD and title.endswith(":") and len(title) < 28:
                continue
            out.append((title, section_body[m.start() : end]))
        return out
    return []


def _ws_score(heading: str, order: int, total: int) -> float:
    h = heading.lower()
    s = 0.0
    if any(k in h for k in _WS_LIVE_HINTS):
        s += 3.0
    if any(k in h for k in _WS_ARCHIVAL_HINTS):
        s -= 5.0
    if re.search(r"20\d\d-\d\d-\d\d", heading):
        s += 1.0
    # Append-style files put the newest work last; break ties toward the tail.
    s += (order + 1) / (total + 1)
    return s


def _trim_working_state(text: str, budget: int | None = None) -> str:
    """
    Digest a working-state file to fit `budget` characters.

    Returns `text` byte-identical when it already fits. Otherwise returns a
    complete MAP of the file (every level-2 section named, with its entry count)
    plus one-line detail for the sections that look live, and a footer naming
    what was omitted and how to get the whole file. The returned length is
    <= budget unconditionally.
    """
    if budget is None:
        budget = _WS_DIGEST_BUDGET
    budget = max(400, int(budget))
    if not text or len(text) <= budget:
        return text

    def _bounded(s: str) -> str:
        # SINGLE EXIT for the cap (I1). Every return below goes through here.
        # The predecessor had an early return that skipped its own size logic —
        # which is precisely how a "digest" came to ship 65,821 chars.
        if len(s) > budget:
            s = s[: max(120, budget - 60)].rstrip() + "\n_[digest hard-clamped]_\n"
        return s[:budget]

    try:
        # Head: frontmatter + title + preamble, i.e. everything before the first
        # level-2 heading. Capped so a long preamble cannot eat the budget.
        first = _WS_H2.search(text)
        head_raw = text[: first.start()] if first else text
        body = text[first.start() :] if first else ""
        head_cap = max(300, int(budget * 0.28))
        head = (
            head_raw if len(head_raw) <= head_cap else (head_raw[:head_cap].rstrip() + "\n\n_[preamble truncated]_\n")
        )

        sections = _ws_split_sections(body)
        if not sections:
            keep = budget - 400
            return _bounded(
                text[: max(200, keep)].rstrip() + f"\n\n_[digest mode — no level-2 sections found; first "
                f"{max(200, keep)} of {len(text)} chars shown. Call "
                f"bonsai_session_start(agent_slug=..., verbose=True) for the "
                f"full working-state file.]_\n"
            )

        parsed = []
        for order, (heading, sbody) in enumerate(sections):
            entries = _ws_entries(sbody)
            live = [(t, b) for t, b in entries if not _WS_DONE_MARK.search(b[:400])]
            parsed.append(
                {
                    "heading": heading,
                    "n_all": len(entries),
                    "n_done": len(entries) - len(live),
                    "live": live,
                    "order": order,
                    "score": _ws_score(heading, order, len(sections)),
                }
            )

        # I3 — the index names every section, always. This is the floor; detail
        # is what gets rationed, never a section's existence.
        index_lines = []
        for p in parsed:
            h = p["heading"][:72]
            if p["n_all"]:
                index_lines.append(
                    f"- ## {h} — {len(p['live'])} open" + (f", {p['n_done']} done" if p["n_done"] else "")
                )
            else:
                index_lines.append(f"- ## {h}")
        index = "\n".join(index_lines)

        n_live = sum(len(p["live"]) for p in parsed)
        footer_room = 320
        detail_room = budget - len(head) - len(index) - footer_room - 80

        # If the index alone cannot fit, compress it rather than hide a section.
        if detail_room < 0:
            index_lines = [f"- ## {p['heading'][:44]}" for p in parsed]
            index = "\n".join(index_lines)
            detail_room = budget - len(head) - len(index) - footer_room - 80
        if detail_room < 0 and len(index_lines) > 1:
            # Still too long: keep the headings that matter, by the same score that ranks the detail (open-commitment
            # sections, recency), and count the rest. The newest and the named-open sections survive; the old go first.
            room = max(300, budget - len(head) - footer_room - 80 - 800)
            ranked = sorted(range(len(parsed)), key=lambda k: (-parsed[k]["score"], parsed[k]["order"]))
            kept, used = [], 0
            for k in ranked:
                cost = len(index_lines[k]) + 1
                if used + cost > room and kept:
                    continue
                kept.append(k)
                used += cost
            kept.sort()
            omitted = len(index_lines) - len(kept)
            index = (f"- _[{omitted} lower-ranked section(s) omitted]_\n" if omitted else "") + "\n".join(
                index_lines[k] for k in kept
            )
            detail_room = budget - len(head) - len(index) - footer_room - 80

        detail_parts: list[str] = []
        shown_sections = 0
        shown_entries = 0
        for p in sorted(parsed, key=lambda q: (-q["score"], q["order"])):
            if detail_room <= 120 or not p["live"]:
                continue
            lines = [f"### {p['heading'][:88]}"]
            for title, _blk in p["live"][:10]:
                lines.append(f"- {title[:112]}")
            chunk = "\n".join(lines)
            if len(chunk) + 2 > detail_room:
                continue
            detail_parts.append(chunk)
            detail_room -= len(chunk) + 2
            shown_sections += 1
            shown_entries += len(p["live"][:10])

        detail = "\n\n".join(detail_parts)
        footer = (
            f"\n\n_[digest mode — {len(parsed)} section(s) indexed above, "
            f"{n_live} open entr(y/ies) counted; detail shown for "
            f"{shown_sections} section(s) / {shown_entries} entr(y/ies). "
            f"{len(text)} chars in the file. Call "
            f"bonsai_session_start(agent_slug=..., verbose=True) for the full "
            f"working-state file, or read it directly.]_\n"
        )

        out = head.rstrip() + "\n\n## Digest — open work by section\n\n" + index
        if detail:
            out += "\n\n" + detail
        out += footer
    except Exception as exc:  # pragma: no cover — I9: never raise, always bounded
        keep = max(200, budget - 400)
        return _bounded(
            text[:keep].rstrip() + f"\n\n_[digest failed ({type(exc).__name__}); first {keep} of "
            f"{len(text)} chars shown. verbose=True returns the full file.]_\n"
        )

    # I1 — the cap is absolute. Nothing above may talk its way past this line.
    return _bounded(out)


def _clamp_session_payload(sections: list[str], budget: int | None = None) -> str:
    """
    Join session_start's sections under a hard total budget.

    Water-fills smallest-section-first, so small sections arrive whole and only
    the fat ones are clamped — the handover included. The digest above bounds
    one file; this bounds the payload, so no single growing file can spill it.
    """
    if budget is None:
        budget = _WS_PAYLOAD_BUDGET
    budget = int(budget)
    if budget <= 0:
        # Unbounded by request (verbose=True). The escape hatch stays open.
        return "\n\n".join(sections)
    budget = max(1000, budget)
    joined = "\n\n".join(sections)
    if len(joined) <= budget:
        return joined

    n = len(sections)
    remaining = budget - 2 * max(0, n - 1)
    out: list[str] = [""] * n
    left = n
    for i in sorted(range(n), key=lambda k: len(sections[k])):
        share = max(300, remaining // max(left, 1))
        s = sections[i]
        if len(s) <= share:
            out[i] = s
        else:
            note = (
                f"\n\n_[section clamped — {len(s) - share} of {len(s)} chars "
                f"omitted under the {budget}-char session_start payload budget "
                f"(BONSAI_SESSION_START_BUDGET). Read the file directly for the "
                f"full text.]_"
            )
            out[i] = s[: max(200, share - len(note))].rstrip() + note
        remaining -= len(out[i])
        left -= 1
    res = "\n\n".join(out)
    return res if len(res) <= budget else res[:budget]


# ---------------------------------------------------------------------------
# Location/time sense (rituals/specs/spec-location-time-sense.md) — additive,
# fail-closed. The MCP host process reads the per-principal iCloud sink
# server-side (the broker move: the sandbox can't reach it, but the host-side
# server can) and surfaces an honest location/time line. Never invents a place
# or a local time; an absent/stale fix says so. Gardener-only first slice — a
# minor principal (guardian case) is hard-guarded out until
# the guardian boundary lands (spec §3).
# ---------------------------------------------------------------------------

# The folder a garden's location fixes arrive in is the garden's to name (its launch config sets BONSAI_LOCATION_DIR); none
# named, no folder is read. No one host's sync folder is this code's default (the gardener, 2026-10-15: no instance in runtime).
_LOCATION_SINK_DIR = (
    Path(os.path.expanduser(os.environ["BONSAI_LOCATION_DIR"])) if os.environ.get("BONSAI_LOCATION_DIR") else None
)
_LOCATION_FRESH_MIN = float(os.environ.get("BONSAI_LOCATION_FRESH_MIN", "15"))
# This machine's humans (the gardener, 2026-10-12: a default lookup on this machine from the logged-in OS user to the human's slug).
# ~/.bonsai-garden/humans.json maps an OS login to the slug of the human who runs the
# harness. It NAMES that human and never authorizes one: privilege comes only from the served garden's own files (_garden_owner),
# and a caller that does not say who it is stays unidentified.
_HUMANS_FILE = Path(os.path.expanduser(os.environ.get("BONSAI_HUMANS_FILE", "~/.bonsai-garden/humans.json")))


def _harness_human() -> str:
    """The human slug this machine's lookup gives for the OS user running this process, or "" (no file, no entry, a value
    that is not a slug). Names compare NFC and case-insensitively. Never raises."""
    with contextlib.suppress(OSError, ValueError, AttributeError):  # no readable machine lookup: no default human
        import pwd
        import unicodedata

        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            user = os.environ.get("USER", "")
        want = unicodedata.normalize("NFC", user).strip().lower()
        table = json.loads(_HUMANS_FILE.read_text("utf-8")).get("by_os_user") or {}
        for name, slug in table.items():
            if want and unicodedata.normalize("NFC", str(name)).strip().lower() == want and isinstance(slug, str):
                slug = slug.strip().lower()
                return slug if re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) else ""
    return ""


# The sink is the device fix of the human who runs the harness (the gardener, 2026-10-12: "it's the location of the OS user that
# runs the harness ... it's not leaking but shown"). A daemon reads it for ONE principal: the one its operator names in its own
# environment (the name the producer, tools/location/bonsai-locationd, uses), else the one this machine's lookup gives for the OS
# user running it. Neither = the service is off. And only when the served garden knows that principal (_location_allowed): a
# second garden served from this host cannot read it unless ITS registry names that human (Bonsai's rehearsal, 2026-10-11).
_LOCATION_PRINCIPAL = os.environ.get("BONSAI_LOCATION_PRINCIPAL", "").strip().lower() or _harness_human()


def _location_allowed(principal) -> bool:
    return (
        bool(principal)
        and bool(_LOCATION_PRINCIPAL)
        and principal == _LOCATION_PRINCIPAL
        and _principal_known(principal)
    )


_LOCATION_SKEW_SEC = float(os.environ.get("BONSAI_LOCATION_SKEW_SEC", "120"))
_EXONYM = {
    "Cologne": "Köln",
    "Munich": "München",
    "Nuremberg": "Nürnberg",
    "Vienna": "Wien",
    "Geneva": "Genf",
    "Zurich": "Zürich",
    "Florence": "Firenze",
    "Rome": "Roma",
    "Prague": "Praha",
    "Warsaw": "Warszawa",
    "Lisbon": "Lissabon",
}


def _registry_entry(slug: str) -> dict:
    """Return the registry entry for a slug, or {} (fail-safe)."""
    with contextlib.suppress(OSError, ValueError, AttributeError):  # an unreadable registry knows no entry
        reg = json.loads((REPO_ROOT / "agents" / "registry.json").read_text("utf-8"))
        for a in reg.get("agents", []):
            if a.get("slug") == slug:
                return a
    return {}


def _slug_from_buzz_pubkey(pubkey: str) -> str:
    """Resolve a Buzz/Nostr secp256k1 x-only hex pubkey (64 chars) to a garden slug.

    Reads tools/buzz-nest/buzz-identity-map.json. Checks principal (the gardener) and all
    members' self_hosted.buzz_pubkey. Stateless: no process-shared state; survives
    multi-process MCP deployments. Returns "" on any failure (fail-safe).
    Added: Layer 2, 2026-09-01.
    """
    with contextlib.suppress(
        OSError, ValueError, AttributeError, TypeError
    ):  # an unknown or unreadable identity map names no one
        _pk = pubkey.strip().lower()
        if len(_pk) != 64:
            return ""
        _map_path = REPO_ROOT / "tools" / "buzz-nest" / "buzz-identity-map.json"
        _map = json.loads(_map_path.read_text("utf-8"))
        _p = _map.get("principal", {})
        if _p.get("self_hosted", {}).get("buzz_pubkey", "").lower() == _pk:
            return _p.get("slug") or _garden_owner()
        for _m in _map.get("members", []):
            _sh = _m.get("self_hosted") or {}
            if isinstance(_sh, dict) and _sh.get("buzz_pubkey", "").lower() == _pk:
                return _m.get("slug", "")
    return ""


def _agent_root_quiet(slug: str) -> Path | None:
    """_agent_root for diagnostics (the stale-boot banner): None when the slug is unknown
    OR its root cannot be routed. A diagnostic skips; it never falls back to mono's copy."""
    try:
        return _agent_root(slug)
    except ValueError:
        return None


def _agent_root(slug: str) -> Path | None:
    """The agent's root directory, per agents/registry.json's own `path` field —
        the single source of truth (codex §12), never a hardcoded agents/<slug>/
        guess.

    : bonsai_session_start used to build agents/<slug>/ for
        every agent regardless of what the registry said. Six agents live at a
        nested path (the nested *-shin members) —
        for them the guess pointed at an unrelated directory. For one of them that
        directory happened to hold a stray file (a tombstone it wrote there by
        mistake), so the wrong path *silently succeeded* with the wrong content
        instead of failing loudly — worse than simply finding nothing.

        Returns None only when `slug` is not a recognized registry entry at all —
        the caller uses that to distinguish "unknown agent" from "known agent,
        nothing on disk yet," which a bare not-found could never tell apart.
    """
    entry = _registry_entry(slug)
    if not entry:
        return None
    p = (entry.get("path") or f"agents/{slug}").strip().strip("/")
    # Routed like any read (write routing, 2026-09-23): once a body is adopted into
    # pot, its root IS the pot copy. _resolve raises (loud, repo named) rather than
    # falling back to mono's stale copy or tombstone.
    return _resolve(p)


_GARDEN_OWNER = None


def _garden_owner() -> str:
    """The served garden's own gardener, from the served garden's own files, or "" (never a slug from another
    garden). garden.json `gardener` (a pot); else the first registry entry of a human type (_human_types()) who is their
    own key holder. A planting rehearsal, gap 58, 2026-10-11: a fallback to a literal slug printed the gardener's live
    location for a guardian in a NEW garden, and a literal-slug caller check made that name a superuser in any garden served.
    Privilege and the fallback principal come from here. Cached once found; fail-closed ("") on any trouble."""
    global _GARDEN_OWNER
    if _GARDEN_OWNER:
        return _GARDEN_OWNER
    owner = ""
    with contextlib.suppress(OSError, ValueError, AttributeError):  # no readable garden.json names no gardener
        g = json.loads((REPO_ROOT / "garden.json").read_text("utf-8"))
        if isinstance(g.get("gardener"), str):
            owner = g["gardener"].strip()
    if not owner:
        with contextlib.suppress(
            OSError, ValueError, AttributeError, TypeError
        ):  # an unreadable registry names no gardener
            reg = json.loads((REPO_ROOT / "agents" / "registry.json").read_text("utf-8"))
            for a in reg.get("agents", []):
                # a person who is their OWN key holder: a family member whose key a gardener holds is not this garden's gardener
                if (
                    a.get("type") in _human_types()
                    and isinstance(a.get("slug"), str)
                    and (not a.get("key_holder") or a.get("key_holder") == a["slug"])
                ):
                    owner = a["slug"]
                    break
    if owner and re.fullmatch(r"[a-z0-9][a-z0-9-]*", owner):
        _GARDEN_OWNER = owner
        return owner
    return ""


def _is_garden_owner(caller: str) -> bool:
    owner = _garden_owner()
    return bool(owner) and caller == owner


def _is_human(slug) -> bool:
    """A person, never a kin: the served garden's own gardener, this machine's harness human, or a registry entry of a
    human type. It keeps a person's tree out of the daemon's signing (a person signs himself); it grants nothing.
    Never raises."""
    s = (slug or "").strip().lower()
    if not s:
        return False
    if s == _garden_owner() or s == _harness_human():
        return True
    return _registry_entry(s).get("type") in _human_types()


def _principal_known(principal: str) -> bool:
    """A principal this garden's registry knows: its gardener, or one a registry entry names. A slug from another
    garden is not, so its location is never read here."""
    p = (principal or "").strip().lower()
    if not p:
        return False
    if p == _garden_owner():
        return True
    try:
        reg = json.loads((REPO_ROOT / "agents" / "registry.json").read_text("utf-8"))
        return any((a.get("principal") or "") == p for a in reg.get("agents", []))
    except Exception:
        return False


def _principal_for(slug: str) -> tuple[str, bool]:
    """Agent slug → (principal_slug, is_guarded_minor). *-sai have principal=null →
    they serve this garden's gardener. A key_holder that differs from principal marks a guardian
    (minor) case → guarded until the guardian boundary lands (spec §3)."""
    e = _registry_entry(slug)
    principal = e.get("principal") or _garden_owner()
    guarded = bool(e.get("key_holder") and e.get("key_holder") != principal)
    return principal, guarded


def _principal_guarded(principal: str) -> bool:
    """True if any registry entry for this principal is a guardian (minor) case."""
    with contextlib.suppress(OSError, ValueError, AttributeError):  # an unreadable registry proves no guardian case
        reg = json.loads((REPO_ROOT / "agents" / "registry.json").read_text("utf-8"))
        for a in reg.get("agents", []):
            if a.get("principal") == principal and a.get("key_holder") and a.get("key_holder") != principal:
                return True
    return False


def _read_location_record(principal: str) -> dict | None:
    """Read+validate the principal's sink. .icloud stub / zero-byte / unparseable
    → None (treated absent, spec §6.1). Resolution order per spec §6."""
    candidates: list[Path] = []
    override = os.environ.get("BONSAI_LOCATION_FILE")
    if override:
        candidates.append(Path(override))
    if _LOCATION_SINK_DIR is not None:
        candidates.append(_LOCATION_SINK_DIR / f"{principal}.json")
    candidates.append(Path(os.path.expanduser(f"~/.bonsai-location-{principal}.json")))
    for p in candidates:
        try:
            if (not p.exists()) or str(p).endswith(".icloud") or p.stat().st_size == 0:
                continue
            rec = json.loads(p.read_text("utf-8"))
            if isinstance(rec, dict) and rec.get("updatedAt"):
                return rec
        except (OSError, ValueError):  # a location file that cannot be read is skipped; the next one is tried
            continue
    return None


def _location_age_minutes(updated_at: str) -> float | None:
    """Minutes since updatedAt vs the broker clock, with clock-skew handling
    (spec §4.1). Future-within-SKEW clamps to 0; far-future / unparseable → None
    (never fresh)."""
    try:
        t = _dt.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None
    delta = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds()
    if delta < 0:
        return 0.0 if -delta <= _LOCATION_SKEW_SEC else None
    return delta / 60.0


def _location_summary(principal: str, guarded: bool, fresh_min: float | None = None) -> str:
    """Fail-closed one-line location/time summary for a principal (spec §7)."""
    fresh_max = _LOCATION_FRESH_MIN if fresh_min is None else fresh_min
    utc = _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M")
    if not _location_allowed(principal):
        return f"📍 the location service is not enabled for '{principal}' in this garden: nothing is read. UTC {utc}."
    if guarded:
        return (
            f"📍 location for principal '{principal}' is guarded (minor / guardian "
            f"case) — not surfaced until the trust boundary for minors lands (spec §3). UTC {utc}."
        )
    rec = _read_location_record(principal)
    if not rec:
        return f"📍 no location on file for {principal} — current UTC {utc}; state UTC, not a guessed local time."
    age = _location_age_minutes(rec.get("updatedAt", ""))
    city = rec.get("city")
    local = _EXONYM.get(city, city) if city else None
    cc = rec.get("countryCode", "?")
    if age is None or age > fresh_max:
        ago = f"{round(age)} min" if age is not None else "unknown age"
        return (
            f"📍 {local or city or '?'}, {cc} — last fix {ago} ago (STALE); "
            f"current UTC {utc}, not asserting local time."
        )
    tz = rec.get("timezone", "UTC")
    lt, tzabbr = "", ""
    with contextlib.suppress(ImportError, KeyError, ValueError):  # an unknown time zone shows UTC
        from zoneinfo import ZoneInfo

        _nowtz = _dt.datetime.now(ZoneInfo(tz))
        lt = _nowtz.strftime("%H:%M")
        tzabbr = _nowtz.tzname() or ""
    return (
        f"📍 {local or city}, {cc} · {lt} {tzabbr}".rstrip()
        + f" · fix {round(age)} min ago · source {rec.get('source', '?')}"
    )


def _location_line(slug: str) -> str:
    """Location/time line for an agent's own principal (session-start surface)."""
    principal, guarded = _principal_for(slug)
    return _location_summary(principal, guarded)


# ---------------------------------------------------------------------------
# Stale-boot detection (2026-07-10) — a session-start guard so an agent
# can catch an "amnesiac boot": working-state.md faithfully returned but far
# behind the newest lived evidence on disk. The tool is not wrong to return
# stale data — but it can say the data looks stale. Fail-safe: any parse/IO
# problem returns "" (no banner) rather than blocking the boot.
# ---------------------------------------------------------------------------

# How many days the working-state `updated:` may lag the newest file in the
# agent's own folder before session-start flags a possible stale boot.
_STALE_BOOT_LAG_DAYS = float(os.environ.get("BONSAI_STALE_BOOT_LAG_DAYS", "3"))


def _parse_iso_date(value: str) -> _dt.date | None:
    """Parse a YYYY-MM-DD (or ISO datetime) frontmatter date. None on failure."""
    if not value:
        return None
    s = str(value).strip().strip('"').strip("'")
    try:
        return _dt.date.fromisoformat(s[:10])
    except Exception:
        return None


def _newest_folder_evidence(slug: str, exclude: Path | None = None) -> tuple[_dt.date, str] | None:
    """
    Return (date, relative-path) of the newest file (by mtime) under the
    agent's registry-declared root (— agents/registry.json's
    `path` field is the source, not a hardcoded agents/<slug>/ guess; a nested
    *-shin lives at agents/<parent>/sub-agents/<slug>, and the
    old guess silently scanned an unrelated/empty directory instead), excluding
    one path (the working-state file itself) and the noisy .bak tombstones.
    Used to gauge how much lived history exists that the working-state may not
    reflect. None if the folder is unreadable.
    """
    root = _agent_root_quiet(slug)
    if root is None or not root.exists():
        return None
    newest_mtime = -1.0
    newest_path: Path | None = None
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if exclude is not None and p.resolve() == exclude.resolve():
                continue
            if p.name.endswith(".bak"):
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > newest_mtime:
                newest_mtime = m
                newest_path = p
    except OSError:
        return None
    if newest_path is None:
        return None
    d = _dt.date.fromtimestamp(newest_mtime)
    return d, _rel(newest_path)


def _git_last_touched_date(rel_path: str, repo_root: Path | None = None) -> _dt.date | None:
    """
    Best-effort `git log -1` date for a repo-relative path.

    Added 2026-09-13 as a floor under the working-state
    `updated:` frontmatter field. Root cause this closes: working-state.md
    files are append-only in practice (Routine 5 session close adds a dated
    section every time) but the frontmatter `updated:` line is only ever set
    by a full "clean slate" rewrite — every session in between correctly
    appends and never restamps it, so a stale header could sit for weeks
    while the file itself stayed current (see
    tools/memory/restamp-working-state-headers.py, written the same day to
    fix the *existing* drift by restamping headers workshop-wide from this
    same git-log signal). This function makes the *check* itself resistant
    to the next restamp getting missed, rather than only fixing today's
    instances: it reads the objective git history of the file instead of the
    hand-maintained field.

    Trade-off, accepted deliberately: a bulk mechanical commit that touches a
    file's bytes without a meaningful content change (e.g. a repo-wide
    formatting pass) would also advance this date and could mask a genuine
    stale boot. `_stale_boot_banner` takes the *later* of this and the
    frontmatter date rather than replacing it, so a hand-set date can still
    win when it is legitimately newer than the file's own git history (e.g.
    an uncommitted local edit) — this only ever suppresses false positives,
    never introduces a new one relative to frontmatter-only.

    Fail-safe: EXTRACT_MODE (no git history on a Pi extract), a missing git
    binary, an untracked path, or any parse error all return None, and the
    caller falls back to the frontmatter field alone — never blocks a boot.
    """
    if EXTRACT_MODE:
        return None
    try:
        stdout, _stderr, rc = _git(
            ["log", "-1", "--format=%ad", "--date=format:%Y-%m-%d", "--", rel_path],
            repo_root=repo_root,
        )
        if rc != 0 or not stdout.strip():
            return None
        return _dt.date.fromisoformat(stdout.strip())
    except Exception:
        return None


def _stale_boot_banner(slug: str, ws_path: Path, ws_text: str, handover_path: Path | None) -> str:
    """
    Return a ⚠ warning banner if the working-state looks stale relative to the
    newest evidence on disk (folder files and/or the newest handover), else "".
    Fail-safe: swallows all errors and returns "" — never blocks a boot.
    """
    try:
        fm, _ = _parse_frontmatter(ws_text)
        fm_updated = _parse_iso_date(fm.get("updated", "") or fm.get("created", ""))
        git_updated = _git_last_touched_date(_locate(ws_path)[2], repo_root=_locate(ws_path)[1])
        # Trust whichever signal is more recent (see _git_last_touched_date's
        # own docstring for the reasoning and the accepted trade-off).
        _candidates = [d for d in (fm_updated, git_updated) if d is not None]
        ws_updated = max(_candidates) if _candidates else None
        if ws_updated is None:
            return ""

        reasons: list[str] = []

        _root_for_evidence = _agent_root_quiet(slug)
        if _root_for_evidence is None:
            return ""  # unknown or unroutable body — no banner rather than a guess
        _unresolved_reason = newest_folder_evidence_reason(_root_for_evidence)
        if _unresolved_reason:
            # The check never ran at all (e.g. the agent's registry-declared root doesn't
            # resolve on disk) — distinct from "ran, found nothing newer".
            reasons.append(f"folder-evidence check incomplete — {_unresolved_reason}")
            newest = None
        else:
            newest = _newest_folder_evidence(slug, exclude=ws_path)
        if newest is not None:
            newest_date, newest_rel = newest
            lag = (newest_date - ws_updated).days
            if lag >= _STALE_BOOT_LAG_DAYS:
                root = _agent_root_quiet(slug) or (REPO_ROOT / "agents" / slug)
                reasons.append(
                    f"newest file in {_rel(root)}/ is {lag} day(s) newer ({newest_date.isoformat()}, {newest_rel})"
                )

        if handover_path is not None:
            # Filenames are session-YYYY-MM-DD-...-handover.md
            m = re.search(r"(\d{4}-\d{2}-\d{2})", handover_path.name)
            hv = _parse_iso_date(m.group(1)) if m else None
            if hv is not None and (hv - ws_updated).days >= _STALE_BOOT_LAG_DAYS:
                reasons.append(f"newest handover is dated {hv.isoformat()} ({(hv - ws_updated).days} day(s) ahead)")

        if not reasons:
            return ""

        if git_updated is not None and (fm_updated is None or git_updated > fm_updated):
            basis = (
                f"working-state.md's own git history (last touched "
                f"{git_updated.isoformat()}"
                + (f", frontmatter `updated:` still says {fm_updated.isoformat()}" if fm_updated else "")
                + ")"
            )
        else:
            basis = f"working-state.md `updated:` is {ws_updated.isoformat()}"

        return (
            f"⚠ POSSIBLE STALE BOOT — {basis}, but "
            + "; ".join(reasons)
            + f". The working-state below may not reflect {slug}'s recent lived "
            "history. Read the newest folder files / handover before trusting "
            "the digest, and bring working-state.md current this session."
        )
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Tools — session start + shared context
# ---------------------------------------------------------------------------


def _handover_order(p: Path) -> tuple[str, float, str]:
    """Sort key for handovers: (the date in the name, the mtime, the name); later sorts later."""
    m = re.match(r"^session-(\d{4}-\d{2}-\d{2})", p.name)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (m.group(1) if m else "", mtime, p.name)


def _own_handover(logs_dir: Path, slug: str) -> Path | None:
    """The newest handover that is THIS kin's own, or None.

    It used to be the newest handover by filename, whoever wrote it: on 2026-09-27 every kin that
    booted was served another kin's `confidentiality: intel` handover about a private session (found by
    a boot analysis, B2). A handover is a kin's bridge to their own next seat; another kin's
    is not ours to read at boot. Own = the header's tended-by names the slug, or, with no
    tended-by, the filename is session-<date>-<slug>[-topic]-handover.md. No slug: none served.

    Newest = the latest date in the name, then the file written last (mtime), then the name. It used
    to be the name alone, so on one date the topic decided: on 2026-10-16 `-midday-` was served over
    `-evening-` because m sorts after e (found by a seat of 2026-10-16; fixed 2026-10-17). A fresh clone gives
    every file one mtime, and there the name is the last word, as before.
    """
    if not slug:
        return None
    name_re = re.compile(rf"^session-\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(slug)}(?:-.+)?-handover\.md$")
    for cand in sorted(logs_dir.glob("session-*-handover.md"), key=_handover_order, reverse=True):
        try:
            head = cand.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        fm, _ = _parse_frontmatter(head)
        tb = fm.get("tended-by") if isinstance(fm, dict) else None
        tended = [str(x).strip().lower() for x in (tb if isinstance(tb, list) else [tb] if tb else [])]
        if tended:
            if slug in tended:
                return cand
            continue  # credited to someone else: never served, whatever its name
        if name_re.match(cand.name):
            return cand
    return None


_HANDOVER_ROOM = 3500


def _bounded_handover(path: Path, verbose: bool) -> str:
    """The handover as session_start serves it: whole when verbose, else its head up to a section
    boundary within _HANDOVER_ROOM chars, plus where the rest is. A handover opens with its state;
    the detail is read when it is needed, not paid for at every activation."""
    text = path.read_text(encoding="utf-8")
    if verbose or len(text) <= _HANDOVER_ROOM:
        return text
    cut = text.rfind("\n## ", 0, _HANDOVER_ROOM)
    if cut < _HANDOVER_ROOM // 3:
        cut = text.rfind("\n", 0, _HANDOVER_ROOM)
    _slug, _root, inside = _locate(
        path
    )  # a handover read from the hub is named by its path there, as a read asks for it
    rel = Path(inside).as_posix() if _root else path.name
    return (
        text[:cut].rstrip() + f"\n\n_[handover trimmed: {cut} of {len(text)} chars. "
        f"The rest: bonsai_read_file(\"{rel}\"), or bonsai_session_start(verbose=True).]_"
    )


def _session_start_blocking(agent_slug: str, verbose: bool, ctx: Context | None) -> str:
    """The body of bonsai_session_start, run off the event loop (see the tool below).

    Everything in here blocks: git and the verifier as subprocesses, the integrity line, file reads.
    On the shared http daemon one loop serves every seat, and a blocking body held it for the whole
    call: a second seat's `initialize` waited behind it (measured 2026-10-17: 6.5 s behind one
    7.5 s session_start on the origin's host), and with several seats starting at once the harness gave
    up on the handshake ("closed", then "unavailable") and came back ~20 s later. The loop must stay
    free to answer a handshake while a seat's context is being gathered.
    """
    sections: list[str] = []

    # 0. Deployment context — where this process is running and which Buzz
    #    relay it answers to, so a boot never has to guess or over-credit its environment.
    #    Fail-safe: deployment_context_text() never raises.
    sections.append(f"=== deployment ===\n{deployment_context_text(REPO_ROOT)}")

    # 0b. Environment (2026-09-23) — the fixed-key block the greeting widget renders:
    #     host, harness, repo, MCP build vs disk, the gardener's location, a timestamp.
    try:
        sections.append(f"=== env ===\n{_env_block(agent_slug, ctx)}")
    except Exception as e:  # session start never fails on its env block; it says so instead of leaving it out
        sections.append(f"=== env === (unavailable: {type(e).__name__}: {e})")

    # 1. Most recent handover
    #    Extract mode: diary/ is excluded; read the sanitized handover digest the
    # deploy step ships into agents/shared/.
    handover_path: Path | None = None
    if EXTRACT_MODE:
        digest = REPO_ROOT / "agents" / "shared" / "handover-digest.md"
        if digest.exists():
            handover_path = digest
            sections.append(
                f"=== Handover digest: {digest.name} (extract mode) ===\n{digest.read_text(encoding='utf-8')}"
            )
        else:
            sections.append("=== Handover === (no handover-digest.md in extract)")
    else:
        # routed like any read: served from the origin's pot, the diary is in the hub (the 0.8.2 acceptance run)
        try:
            logs_dir = _resolve("diary/logs")
        except ValueError:
            logs_dir = None
        if logs_dir is not None and logs_dir.exists():
            handover_path = _own_handover(logs_dir, agent_slug.strip().lower())
            if handover_path:
                sections.append(f"=== Handover: {handover_path.name} ===\n{_bounded_handover(handover_path, verbose)}")
        if not handover_path:
            who = agent_slug.strip().lower()
            sections.append(
                f"=== Handover === (none of {who}'s own in diary/logs/)"
                if who
                else "=== Handover === (no slug given: no handover is served)"
            )

    # 2. Agent working-state
    slug = agent_slug.strip().lower()

    # Register THIS connection's caller identity, keyed by its own MCP session
    # object (see _CALLER_BY_SESSION above), so concurrent connections/sessions
    # on a shared process never see or clobber each other's identity. Every
    # *-sai/*-shin activation calls bonsai_session_start(own_slug) as its first
    # tool call (Routine 1 Step 0); this records who that session is.
    #
    # fix (2026-08-14): the process-global `CALLER = slug` mutation
    # that used to sit here has been REMOVED. It was last-writer-wins across
    # every concurrent session on a shared process — the direct cause of the
    # cross-session identity leak (a lookup miss in one session surfacing
    # another session's most-recently-entered slug). Per-session registration
    # is now the ONLY thing session_start does to caller state; a session that
    # is never registered falls back to the immutable _DEFAULT_CALLER, never to
    # a value another session set. Validated against the registry first — an
    # unrecognized slug registers nothing. Original diagnosis (2026-07-15):
    # agents/<slug>/memory/working-state.md and agents/<other>/memory/journal/2026-07-15.md.
    _session_start_entry = _registry_entry(slug) if slug else {}

    if slug and _session_start_entry and ctx is not None:
        with contextlib.suppress(AttributeError, TypeError):  # a context without a usable session is not registered
            _CALLER_BY_SESSION[ctx.session] = slug

    if slug and not _session_start_entry:
        #: an unrecognized slug must say so plainly, never
        # fall through to a not-found message that reads identically to "this
        # real agent simply has no file yet." A null is evidence about the
        # lookup, not about the thing.
        sections.append(
            f"=== {slug} / working-state === "
            f"ERROR: '{slug}' is not a recognized agent in agents/registry.json. "
            f"Check for a typo, or register it first with tools/add-agent.sh."
        )
    elif slug:
        # Registry-derived root — never a hardcoded
        # agents/<slug>/ guess. Six agents live nested under a parent *-sai
        # (the nested *-shin members); the old
        # guess pointed at the wrong directory for all six.
        try:
            agent_root = _agent_root(slug)
        except ValueError as _route_exc:
            agent_root = None
            sections.append(f"=== {slug} / working-state === ERROR: cannot resolve the body's root — {_route_exc}")
    if slug and _session_start_entry and agent_root is not None:
        # Extract mode ships only the scrubbed working-state-digest.md;
        # fall back to it when the full working-state.md is absent.
        # F-21 (a dry run, 2026-10-14): a planted garden keeps a kin's LIVED memory in home/<slug>/ and the
        # sealed body's memory/ as a skeleton (the ruling of 2026-10-12), so home's working-state is read first when
        # it is there. A garden whose kin keep it in the body (the origin's) reads as before.
        # home/<slug> is routed like any read: it is where the table says (the pot's own, or the hub), never the served
        # root's copy by default; a route that refuses reads no home at all (the 0.8.2 acceptance run)
        try:
            ws_home = _resolve(f"home/{slug}/memory/working-state.md")
        except ValueError:
            ws_home = None
        ws = ws_home if ws_home is not None and ws_home.is_file() else agent_root / "memory" / "working-state.md"
        ws_label = f" (lived: {_rel(ws_home)})" if ws is ws_home else ""
        ws_digest = agent_root / "memory" / "working-state-digest.md"
        if EXTRACT_MODE and ws_digest.exists():
            sections.append(f"=== {slug} / working-state (extract digest) ===\n{ws_digest.read_text(encoding='utf-8')}")
        elif ws.exists():
            ws_raw = ws.read_text(encoding="utf-8")
            banner = _stale_boot_banner(slug, ws, ws_raw, handover_path)
            ws_text = ws_raw if verbose else _trim_working_state(ws_raw, budget=_WS_DIGEST_BUDGET)
            if banner:
                ws_text = f"{banner}\n\n{ws_text}"
            sections.append(f"=== {slug} / working-state{ws_label} ===\n{ws_text}")
        elif ws_digest.exists():
            sections.append(f"=== {slug} / working-state (extract digest) ===\n{ws_digest.read_text(encoding='utf-8')}")
        else:
            sections.append(
                f"=== {slug} / working-state === "
                f"(not found at {_rel(ws)} — registry path is "
                f"'{_session_start_entry.get('path', f'agents/{slug}')}')"
            )

    # 2b. Sealed remote memory (Option A read-side): for a registry-sealed agent, inject the
    #     decrypted episodes so a Cowork activation carries the agent's real (episodic) memory.
    #     The repo holds only seed files; lived memory lives in the vault. Fail-safe → skipped if
    #     the vault is unavailable. Not in extract mode (no Keychain on the Pi).
    if slug and not EXTRACT_MODE:
        _sealed_mem = _load_sealed_memory(slug)
        if _sealed_mem:
            sections.append(f"=== {slug} / sealed remote memory ===\n{_sealed_mem}")

    # 2c. Location/time sense (spec-location-time-sense.md) — additive, fail-closed.
    #     Brokered server-side; never blocks session-start, never invents a place.
    if slug and not EXTRACT_MODE:
        try:
            principal, _guarded = _principal_for(slug)
            sections.append(f"=== location (principal: {principal}) ===\n{_location_line(slug)}")
        except Exception as e:  # never blocks session start, never invents a place: says it is unavailable
            sections.append(f"=== location === (unavailable: {type(e).__name__})")

    # Slice K — append build_hash + session_token so the caller can use the
    # token on subsequent SSE/HTTP calls to preserve identity across connections.
    _tok_line = ""
    if slug and _session_start_entry:
        _new_tok = str(uuid.uuid4())
        _SESSION_TOKENS[_new_tok] = (slug, _time_module.time() + _SESSION_TOKEN_TTL)
        _tok_line = f"\nsession_token: {_new_tok}"
    try:
        _proc_lines = "\n" + _meta_process_lines()
    except Exception:
        _proc_lines = ""
    sections.append(f"=== meta ===\nbuild_hash: {_BUILD_HASH}{_proc_lines}{_tok_line}")

    # verbose=True bypasses the payload clamp on purpose: it is the escape
    # hatch the digest footer points readers at. A spill to a sidecar file is
    # the correct outcome there — a clamped 'full file' is a broken promise.
    return _clamp_session_payload(sections, budget=(0 if verbose else None))


@mcp.tool(
    name="bonsai_session_start",
    annotations={
        "title": "Load startup context for a bonsai-garden session",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_session_start(
    agent_slug: Annotated[
        str,
        Field(
            description=(
                "Agent slug to load working-state for (a slug from agents/registry.json). Empty = return handover only."
            ),
        ),
    ] = "",
    verbose: Annotated[
        bool,
        Field(
            description=(
                "If False (default), the working-state section is trimmed to a "
                "digest of its open commitments (frontmatter + preamble + one "
                "line per open entry) so the payload stays under Cowork's inline "
                "return cap. If True, the full working-state file is returned."
            ),
        ),
    ] = False,
    ctx: Context | None = None,
) -> str:
    """
    Return the canonical session startup context for a bonsai-garden agent.

    Returns the most recent handover and the agent's working-state digest.
    CLAUDE.md is intentionally excluded — it is already in the system prompt
    via Cowork project instructions and Claude Code native loading.

    Call this first, then call bonsai_get_shared_context() for the shared
    layer (workshop-context + codex + routines).

    Per routines-workshop.md Routine 1 Step 0:
    call this first (Step 0), then bonsai_get_shared_context (Step 0b),
    then agent-specific reads (role.md, about-me.md, memory stack).

    Sections returned:
    1. Handover — most recent diary/logs/session-*-handover.md
    2. <slug> working-state — home/<slug>/memory/working-state.md where the garden keeps lived memory
       there, else agents/<slug>/memory/working-state.md (if slug given).
       Trimmed to an open-commitments digest unless verbose=True (see below).
    """
    # Off the loop: the body is all subprocesses and file reads (see _session_start_blocking). The
    # context is only read in there (its session object as a key, its client's name for the env
    # block), never awaited, so it may cross into the worker thread.
    return await anyio.to_thread.run_sync(_session_start_blocking, agent_slug, verbose, ctx)


@mcp.tool(
    name="bonsai_location",
    annotations={
        "title": "Current location/time for a principal (fail-closed)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_location(
    principal: Annotated[
        str,
        Field(
            description=(
                "Principal slug whose location to read: the served garden's own gardener or a principal its registry names. "
                "Default: the garden's gardener. Minor/guardian principals are guarded out "
                "until the trust boundary for minors lands (spec §3)."
            ),
        ),
    ] = "",
    max_age_minutes: Annotated[
        float,
        Field(
            description=(
                "Override the freshness threshold (minutes) for this call. "
                "0 = use the default (15). A fix older than this reads STALE."
            ),
        ),
    ] = 0,
) -> str:
    """
    Return the freshest reachable location/time for a principal, fail-closed.

    Reads the per-principal sink server-side (spec-location-time-sense.md §6),
    applies the §4.1 freshness/clock-skew rules, and returns an honest one-line
    summary: a fresh fix with local time, or an explicit STALE / absent line.
    Never fabricates a location or a local time.
    """
    p = (principal or _garden_owner()).strip().lower()
    if not _principal_known(p):
        return f"no principal {p!r} in this garden: its location is not read here"
    guarded = _principal_guarded(p)
    fresh = max_age_minutes if max_age_minutes and max_age_minutes > 0 else None
    return _location_summary(p, guarded, fresh)


@mcp.tool(
    name="bonsai_get_shared_context",
    annotations={
        "title": "Load the workshop's shared context layer",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def bonsai_get_shared_context(
    include_cosmology: Annotated[
        bool,
        Field(
            description=(
                "Also return agents/shared/cosmology.md — the directional/elemental "
                "reference (guardian beasts, virtues, elemental resonances). "
                "Most sessions don't need it. Default: false."
            ),
        ),
    ] = False,
    digest: Annotated[
        bool,
        Field(
            description=(
                "If true (default), return agents/shared/shared-context-digest.md — the "
                "~1.5k-token condensed shared layer (codex sections, routine index, roster "
                "pointer). Saves ~7k tokens per activation. Pass digest=false for the full "
                "trio when a situation invokes a specific rule or routine. "
                "Per rituals/specs/spec-activation-footprint-trim.md."
            ),
        ),
    ] = True,
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Return the workshop's shared context layer in one payload.

    Replaces three separate bonsai_read_file calls with a single round-trip.
    Call once per session immediately after bonsai_session_start, before reading
    agent-specific files (role.md, about-me.md, memory stack).

    Per routines-workshop.md Routine 1 Step 0b:
    call after bonsai_session_start, before role.md and about-me.md reads.

    Sections returned:
    1. agents/shared/workshop-context.md  — narrative layer: roster, founding
       story condensed, identity baseline, external agent provenance rule
    2. agents/shared/codex-workshop.md    — binding law for all *-sai/*-shin
    3. agents/shared/routines-workshop.md — standard operational sequences
    4. agents/shared/cosmology.md         — (only if include_cosmology=True)
    """
    _caller = _resolve_caller("", ctx, session_token)
    _id_guard = _require_identified(_caller, "bonsai_get_shared_context")
    if _id_guard is not None:
        return _id_guard
    # F-18 (a dry run, 2026-10-14): a planted pot carries no shared layer of its own, so its kin reached no codex
    # and no routines. The gardener, 2026-10-15: "Every garden should keep track of codex and covenant on their own. They
    # need to have that on record." So the law is the POT's: lore/codex.md and lore/covenant.md, the texts its gardener
    # and kin signed at stage 4 (lore/covenant-roll.json). Core's text is where a garden starts, never what it lives by;
    # none on record is said, and no other law is served in its place. The routines are craft, not law: core's, from the
    # core install-runtime wired the pot to (git config bonsai.core), nothing copied. In full: core's digest condenses
    # the origin workshop's own layer, not a planted garden's.
    pot_core = _wired_core(REPO_ROOT) if _is_pot() else None
    if _is_pot():
        files = [
            ("lore/codex.md", "Codex (this garden's, on record)"),
            ("lore/covenant.md", "Covenant (this garden's, on record)"),
            ("agents/shared/routines.md", "Routines (core)"),
        ]
        if include_cosmology:
            files.append(("agents/shared/cosmology.md", "Cosmology (core)"))
    elif digest and not include_cosmology:
        files = [("agents/shared/shared-context-digest.md", "Shared Context Digest")]
    else:
        files = [
            ("agents/shared/workshop-context.md", "Workshop Context"),
            ("agents/shared/codex-workshop.md", "Workshop Codex"),
            ("agents/shared/routines-workshop.md", "Workshop Routines"),
        ]
        if include_cosmology:
            files.append(("agents/shared/cosmology.md", "Cosmology"))

    sections: list[str] = []
    for rel_path, label in files:
        # Route. This joined REPO_ROOT directly until 2026-09-17, which meant
        # that once the shared layer moved to the pot in Wave 1 (2026-09-14)
        # every digest=False call read the TOMBSTONE left at the mono address
        # and returned it as the codex. It did not fail: p.exists() is true for
        # a tombstone, so the "not found" branch never fired and no agent was
        # told its law was missing. AGENTS.md -> "Fail, don't fall back".
        try:
            if _is_pot() and rel_path.startswith("lore/"):
                p = REPO_ROOT / rel_path
                if not p.is_file():
                    raise ValueError(
                        "not on record in this garden: stage 4 (covenant write, then covenant sign and codex sign) "
                        "has not run here; no other text is served in its place"
                    )
            elif _is_pot():
                if pot_core is None:
                    raise ValueError(
                        "this pot names no core (git config bonsai.core); install-runtime wires it at stage 5"
                    )
                p = pot_core / rel_path
            else:
                p = _resolve(rel_path)
        except ValueError as _exc:
            sections.append(f"=== {label}: {rel_path} === (unavailable: {_exc})")
            continue
        if not p.exists():
            sections.append(f"=== {label}: {rel_path} === (not found)")
            continue
        text = p.read_text(encoding="utf-8")
        # Tripwire, not a path fix. Routing is now correct, but a tombstone
        # served as law is the failure worth catching by its PROPERTY rather
        # than by the address it came from: any `status: moved` document here
        # means the router and the content have drifted apart again.
        _fm, _ = _parse_frontmatter(text)
        if str(_fm.get("status", "")).strip().lower() == "moved":
            sections.append(
                f"=== {label}: {rel_path} === (REFUSED: resolved to {_rel(p)}, "
                f"which is a tombstone (status: moved -> {_fm.get('moved-to', '?')}). "
                f"The shared layer is not being routed correctly; do not proceed on "
                f"an unloaded codex. Check rituals/repo/repo-split-registry.json.)"
            )
            continue
        sections.append(f"=== {label}: {rel_path} ===\n{text}")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Presence — room-awareness for agent activations
# ---------------------------------------------------------------------------


@mcp.tool(
    name="bonsai_presence",
    annotations={
        "title": "Announce agent presence (lamp + LED + voice)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def bonsai_presence(
    agent_slug: Annotated[
        str,
        Field(
            description=(
                "Agent slug (a slug from agents/registry.json). Used to select the correct accent colour, "
                "LED ring signature, and Piper voice. Case-insensitive."
            )
        ),
    ],
    event: Annotated[
        str,
        Field(
            description=(
                "Presence event to fire. One of: "
                "'greeting' (default — plays agent sting + sets lamp to accent colour), "
                "'done' (chirp trill + lamp to full brightness), "
                "'idle' (purr + lamp dims to warm). "
                "A 'dance' event is always appended after the main event to refresh "
                "the Hue accent on the signal light."
            )
        ),
    ] = "greeting",
    session_token: Annotated[
        str,
        Field(description="Session token from bonsai_session_start (required on SSE/HTTP transport)."),
    ] = "",
    ctx: Context | None = None,
) -> str:
    """
    Announce an agent's presence to the physical room.

    Fires two events to the garden's room server, at BONSAI_PRESENCE_URL (its launch config names it;
    unset, the tool does nothing):
      1. The named event (greeting / done / idle) → sound sting + light cue
      2. 'dance' with the agent slug → Hue accent colour on the signal lamp

    Also writes the agent slug to ~/.config/cowork-voice/agent so subsequent
    Cowork Stop-hook replies speak in this agent's registered Piper voice.

    Call once per session in Routine 2 (Introduction), immediately after
    bonsai_session_start. Silently degrades — presence is ambient, never blocking.
    If the kiosk server is unreachable, the voice-file write still succeeds so
    at minimum the reply TTS uses the correct voice.

    Added: 2026-06-15 — agent presence in the room.
    """
    import json as _json
    import pathlib
    import urllib.error
    import urllib.request

    _caller = _resolve_caller("", ctx, session_token)
    _id_guard = _require_identified(_caller, "bonsai_presence")
    if _id_guard is not None:
        return _id_guard
    slug = agent_slug.strip().lower()
    if not slug:
        return "error: agent_slug required"

    # A room is the garden's to name: its launch config sets BONSAI_PRESENCE_URL, the full event endpoint of its room server.
    # None named, nothing fires and nothing is written on this host (the gardener, 2026-10-15: no instance in runtime).
    presence_url = os.environ.get("BONSAI_PRESENCE_URL", "").strip()
    if not presence_url:
        return "presence: this garden names no room (BONSAI_PRESENCE_URL unset); nothing fired, nothing written"
    if not presence_url.startswith(("http://", "https://")):
        return "presence: BONSAI_PRESENCE_URL is not an http(s) URL; nothing fired, nothing written"
    results: list[str] = []

    # Fire the events to the kiosk → koe-shind → Hue + LED ring + sound.
    for evt in [event, "dance"]:
        payload_obj: dict = {"event": evt, "agent": slug}
        if evt == "dance":
            payload_obj["syncBpm"] = False  # don't fetch Spotify BPM during activation
        data = _json.dumps(payload_obj).encode()
        try:
            req = urllib.request.Request(  # noqa: S310  the scheme is checked to be http(s) above
                presence_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3):  # noqa: S310  the scheme is checked to be http(s) above
                results.append(f"{evt}: ok")
        except (urllib.error.URLError, OSError) as exc:
            results.append(f"{evt}: unreachable ({exc})")

    # Write the cowork-voice agent file → Stop-hook replies use this agent's voice.
    cfg_path = pathlib.Path.home() / ".config" / "cowork-voice" / "agent"
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(slug + "\n")
        results.append(f"voice→{slug}")
    except OSError as exc:
        results.append(f"voice: write failed ({exc})")

    return f"presence({slug}@{event}): " + "; ".join(results)


# ---------------------------------------------------------------------------
# The execute gate — Mechanism B
# ---------------------------------------------------------------------------
# WHY: this file — the one that gatekeeps every write in the garden — was
# committed twice in the week of 2026-09-22 with a stale .bonsaisig, and nothing
# noticed. Nothing could: no spawn path of this daemon (launchd, PM2, `uv run`)
# passes through the Bash pre-exec hook, so the daemon has to check itself.
# Spec: bonsai-garden-runtime/rituals/specs/spec-execute-gate-signed-artefacts.md.
#
# WHY THE CLOSURE AND NOT THE FILE: a verifier's finding, 2026-09-22 — a self-check that
# reads only server.py.bonsaisig passes with a tampered repo_routing.py sitting
# beside it. The gate says clean and then imports the tamper. sys.modules after
# the imports have run is the honest list of what was loaded; a hand-kept
# manifest would be a second master and would drift the first time a module
# moved. Absence fails the same as mismatch — an unsigned local import is the
# same hole with the sidecar merely not yet written.
#
# WHY SUBPROCESS AND NOT IN-PROCESS: sign.py hard-depends on `cryptography`,
# which is nowhere in this project's pyproject.toml — the prod .venv only
# carries it transitively. A fail-closed check that imported it in-process
# would refuse to serve the first time dependencies were resynced. The host
# python3 carries it, and pre-exec-verify.sh already calls the verifier this
# way. One fork for the whole closure, not one per module.

_EXEC_GATE_ENV = "BONSAI_EXEC_GATE"


def _exec_gate_closure() -> list:
    """Absolute paths of every loaded module that came out of the garden, plus
    the one the daemon loads later by hand. Deduplicated — server.py is in
    sys.modules twice, as __main__ and __mp_main__."""
    roots = {Path(__file__).resolve().parent, SERVER_ROOT}
    # The interpreter's own tree is not the garden's code, even when it sits
    # inside the garden: the prod venv is tools/bonsai-mcp/.venv, under REPO_ROOT,
    # and without this exclusion the closure is 330 modules of which 326 are
    # site-packages with no seal — the daemon would refuse to start in prod while
    # every fixture (built without a .venv) passed. Found in review 2026-09-22,
    # with the prod interpreter, before merge. Exclude by prefix (wherever the
    # venv lives) AND by directory name (a venv reached through a symlink).
    interp = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    _FOREIGN = {".venv", "venv", "site-packages", "dist-packages", "node_modules"}
    found = set()
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f or not f.endswith(".py"):
            continue
        p = Path(f).resolve()
        if any(p.is_relative_to(r) for r in interp) or _FOREIGN & set(p.parts):
            continue
        if any(p.is_relative_to(r) for r in roots):
            found.add(p)
    if _RM_BRIDGE_PATH.exists():
        found.add(_RM_BRIDGE_PATH.resolve())
    return sorted(found)


def _exec_gate_verify(paths: list) -> list:
    """[(path, reason)] for every module whose sidecar does not attest its bytes.
    Trouble reaching the verifier counts as a failure: a gate that fails open
    when its own tooling is missing is the guard this spec exists to replace."""
    # Seal v2: a v1 seal reads retired-v1, which sign.seal_accepted() passes only
    # while core's seal-policy.json says "v1": "warn" — so this gate refuses v1 at the same
    # cutover as every other verifier, with no switch of its own.
    prog = (
        "import sys, json; sys.path.insert(0, sys.argv[1]); import sign\n"
        "from pathlib import Path\n"
        "rows = []\n"
        "for p in sys.argv[2:]:\n"
        "    st, meta = sign.verify_sidecar_file(Path(p))\n"
        "    rows.append([p, st, sign.seal_accepted(st, meta)])\n"
        "print(json.dumps(rows))"
    )
    if not (
        SERVER_ROOT / "tools" / "sign.py"
    ).is_file():  # hosted by runtime or an installed rt: no sign.py (signer.py)
        _oc = (
            REPO_ROOT.parent / "bonsai-garden-core"
        )  # the origin's own core, as its integrity line has always named it
        return _signer_mod.core_gate(
            paths,
            SERVER_ROOT,
            REPO_ROOT,
            _wired_core(REPO_ROOT) if _is_pot() else (_oc if (_oc / _signer_mod.SIGN_MARKER).is_file() else None),
        )
    argv = ["python3", "-c", prog, str(SERVER_ROOT / "tools")] + [str(p) for p in paths]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=str(SERVER_ROOT))
        if r.returncode != 0:
            return [("<verifier>", f"tools/sign.py unusable (rc={r.returncode}): {(r.stderr or '').strip()[-200:]}")]
        rows = json.loads(r.stdout)
    except Exception as exc:
        return [("<verifier>", f"{type(exc).__name__}: {exc}")]
    for p, status, accepted in rows:
        if accepted and status != "ok":
            print(
                f"[bonsai_mcp] exec-gate: WARNING {p} — {status} (a v1 seal; accepted until the "
                f"v1 cutover, refused after it; re-seal as v2)",
                file=sys.stderr,
            )
    return [(p, status) for p, status, accepted in rows if not accepted]


def _exec_gate_override(raw: str, today: str) -> tuple:
    """(honoured, note) for BONSAI_EXEC_GATE. Only `off:<today, UTC>` is honoured.
    Dated on purpose: the launchd plist carries a persistent EnvironmentVariables
    dict, so a bare `off` pasted there would survive every restart and hold the
    gate open while it still looked on. A dated override goes stale by itself."""
    if not raw:
        return False, ""
    if raw == "off:" + today:
        return True, f"break-glass override honoured for {today} (UTC calendar day)"
    return False, f"malformed override {_EXEC_GATE_ENV}={raw!r} ignored (expected off:{today})"


_CANNOT_VERIFY = "cannot-verify"
_CANNOT_VERIFY_STATUSES = (
    "unknown-signer",
    "keys-unreachable",
)  # verdicts that mean no key to check against, not a differing seal


def _exec_gate_unverifiable(mod, reason: str) -> bool:
    """True when the verdict is "could not be checked" (no verifier, no node, no core, no key, a verifier that
    gave no answer) rather than "was checked and differs". The two are never worded alike."""
    r = str(reason)
    return str(mod) == "<verifier>" or r.startswith(_CANNOT_VERIFY) or (r.split() or [""])[0] in _CANNOT_VERIFY_STATUSES


def _exec_gate_refusal(failures: list, total: int, today: str) -> str:
    """The refusal sentence: modules whose seal was checked and differs ("do not match their seals") and modules
    whose seal could not be checked ("cannot verify", with the reasons) are counted and worded apart."""
    cannot = [(m, r) for m, r in failures if _exec_gate_unverifiable(m, r)]
    differ = [(m, r) for m, r in failures if not _exec_gate_unverifiable(m, r)]
    parts = []
    if differ:
        parts.append(
            f"{len(differ)} of {total} modules in this daemon's import closure do not match their seals. "
            f"Re-sign with `python3 tools/sign.py sign-sidecar <file> --as <slug>`"
        )
    if cannot:
        why = "; ".join(sorted({f"{m}: {r}" if str(m) != "<verifier>" else str(r) for m, r in cannot}))
        parts.append(
            f"cannot verify {len(cannot)} of {total} modules in this daemon's import closure — "
            f"their seals could not be checked ({why}). Re-signing will not help; restore the verifier or key"
        )
    return ". ".join(parts) + f". Or start anyway for one day with {_EXEC_GATE_ENV}=off:{today}."


def _exec_gate() -> None:
    """Refuse to serve unless every module the daemon loaded matches its seal.
    Never returns on refusal. The override still runs the check and still says
    what it found — recovery must be possible, a silent bypass must not."""
    closure = _exec_gate_closure()
    failures = _exec_gate_verify(closure)
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    honoured, note = _exec_gate_override(os.environ.get(_EXEC_GATE_ENV, "").strip(), today)
    if note:
        print(f"[bonsai_mcp] exec-gate: {note}", file=sys.stderr)
    if not failures:
        return
    for mod, reason in failures:
        print(
            f"[bonsai_mcp] exec-gate: {mod} — {'CANNOT VERIFY: ' if _exec_gate_unverifiable(mod, reason) else ''}{reason}",
            file=sys.stderr,
        )
    if not honoured:
        print(
            f"[bonsai_mcp] exec-gate: REFUSING TO SERVE — {_exec_gate_refusal(failures, len(closure), today)}",
            file=sys.stderr,
        )
        sys.exit(3)
    # Honoured: stderr above is the channel a human reads, .bonsai/write-log.jsonl
    # the one a later audit reads. Both, so neither has to be the only one.
    for mod, reason in failures:
        _audit("exec-gate-override", mod, "", "", note=f"{reason}; {note}")
    print(
        f"[bonsai_mcp] exec-gate: serving with unverified modules — override expires at the end of {today} UTC.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="bonsai_mcp — bonsai-garden gated file access MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode: 'stdio' (default) or 'http' (streamable-HTTP for future REST layer)",
    )
    parser.add_argument(
        "--garden",
        default=None,
        help="Root of the garden to serve (a pot launches its own server with `--garden .`); read before anything else at import",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for HTTP transport (default: $BONSAI_MCP_PORT, else 5052); read at import, before anything binds",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    _exec_gate()  # never even execute a file whose seal does not match its bytes

    print(
        f"[bonsai_mcp] repo={REPO_ROOT}  caller={_DEFAULT_CALLER}  transport={args.transport}",
        file=sys.stderr,
    )

    if args.transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
