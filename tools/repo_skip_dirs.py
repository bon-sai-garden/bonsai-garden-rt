#!/usr/bin/env python3
# ---
# id: 01M3BT3Q6QKYCFK3DF2TGHQC5E
# ---
"""
repo_skip_dirs.py — shared directory-exclusion list for repo-wide sweeps.

Two tools sweep the whole repo tree for content matches and both need to
skip the same class of non-canonical directories:

  - tools/check-agent-references.py  (Gate 0 checker, 2026-09-04)
  - tools/bonsai-mcp/server.py's bonsai_find_by_id (2026-09-13)

Build/vendor noise (node_modules, .venv, __pycache__, dist, build, ...) is
excluded because it is never canonical workshop content. `.claude` and
`tmp` are excluded for a sharper reason: `.claude/worktrees/<name>/` is a
full duplicate git worktree checkout of this same repo — a live,
recurring feature of this workshop's multi-session workflow (concurrent
sessions each get their own locked checkout) — and `tmp/` holds scratch
staging copies (e.g. tmp/w-shintai-stage-*). Neither represents a
distinct thing that needs fixing; both are copies of content already
counted/found at its real, canonical path elsewhere in the tree. Without
this exclusion, a repo-wide sweep silently double-counts (the reference
checker's original failure mode) or — worse — a single-match tool like
bonsai_find_by_id returns whichever copy the search tool happens to visit
first, with no error and no ambiguity warning (the finding of 2026-09-13: 6 of 6
real ULIDs resolved into a stale pre-flatten worktree copy instead of the
canonical current file).

Extracted 2026-09-13 so the two tools read one list instead of
maintaining independent copies that can drift apart again — check-agent-
references.py learned this lesson on 2026-09-04; bonsai_find_by_id did not
get it until the gap was found live.
"""

SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".build",
    "dist",
    "build",
    ".obsidian",
    "bin",
    "target",
    ".cache",
    ".next",
    ".turbo",
    "coverage",
    ".claude",
    "tmp",
}
