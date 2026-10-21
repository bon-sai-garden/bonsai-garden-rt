#!/usr/bin/env python3
# ---
# id: 01M2DGQ4XKR7VWBN3E8HCFA2P6
# title: Repo Routing
# type: script
# tended-by: baisai
# created: 2026-09-13
# updated: 2026-10-21
# copyright: © 2026 Bonsai Garden · bon-sai.garden
# ---
"""repo_routing — resolve a repo-relative path to the repo that actually holds it.

The keystone of a garden that spans several repositories. Without it the MCP
resolves every path under a single REPO_ROOT; once `agents/` lives in the pot and
`lore/` in core, that assumption is wrong and every read goes blind.

DESIGN — routes on `status`, not on `target-repo`.

    The split registry says where each path will EVENTUALLY live. It also says
    whether it has moved yet. Routing on the destination would break the garden
    the moment this module went live, because nothing has moved. Routing on
    `status` means this can ship today — every `pending` entry resolves to the
    hub exactly as before — and each path starts routing elsewhere the
    moment its entry flips to `adopted`.

    No big-bang cutover, and the migration becomes reversible one entry at a
    time. That matters more than elegance here: a resolver that can only be
    switched on once is a resolver nobody dares switch on.

DESIGN — a moved path is never silently absent.

    Reading a path whose content has moved returns a Moved result naming the new
    repo, never an empty result or a bare not-found. Silence is the failure this
    garden keeps paying for: 325 dispatch ticks that failed closed unseen, a
    signing hook dead since the day it was written, a site generator printing
    "would write" and writing nothing. A read that quietly finds nothing is that
    same bug wearing another hat.

DESIGN — one namespace per repo (the registry's 0.6.0, `source-repo`).

    Every entry before 0.6.0 names a path in the hub (`current-path`). An entry that carries `source-repo`
    naming another repo names a path in THAT repo, in `source-path`, and carries no `current-path`: a router
    older than 0.6.0 reads every `current-path` in the hub's namespace, so an entry for runtime's
    `tools/finder` written with one would route the hub's `tools/finder` by it (longest prefix), silently. With
    none, an old router skips it by construction. `resolve(path)` is the hub's namespace, unchanged;
    `resolve_in(repo, path)` is another repo's. A path an entry has not moved resolves in its own repo and
    says where it is bound (`bound_for`); an adopted one goes to its target, and a target that is itself
    routed (the hub, or another repo's entry) is followed, never past a loop.

Spec: rituals/specs/spec-garden-registry-v1.md
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path


# Repo slug -> where that repo is checked out on this host. Instance data, so it
# is deliberately overridable by environment rather than baked in: the same
# routing rules must work on a server host, on a new gardener's laptop, and here.
def _default_repos_root() -> Path:
    """Where the sibling repos are checked out.

    Note the string check before Path(): `Path("")` is PosixPath('.'), which is
    TRUTHY, so an `or` chain on Path objects silently yields the current working
    directory when the variable is unset. Caught by this module's own self-test.
    """
    explicit = os.environ.get("BONSAI_REPOS_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    garden = os.environ.get("BONSAI_GARDEN", "").strip()
    return Path(garden).parent if garden else Path.home() / "dev/agentic"


_DEFAULT_SIBLING_ROOT = _default_repos_root()

MONO_SLUG = "bonsai-garden"


@dataclass(frozen=True)
class Resolution:
    """Where a path lives, and why the router thinks so."""

    repo: str  # repo slug, e.g. "bonsai-garden-core"
    root: Path  # that repo's checkout on this host
    path: Path  # the absolute resolved path
    rule: str | None  # the registry entry that matched, if any
    moved: bool  # True if the content is not in the served garden's own checkout (adopted elsewhere, or in a hub it is not)
    bound_for: str | None = None  # a pending entry's destination, "<repo>:<path>": where it goes, said before it moves


class PathRetired(Exception):
    """Base for 'this path deliberately holds nothing any more'.

    Distinct from a missing file. A not-found says "look again"; these say
    "someone decided this, here is where it went". The whole point of routing
    is that a read never has to guess which of the two it just hit.
    """


class PathArchived(PathRetired):
    """The content was archived. Readable there, but explicitly not current."""

    def __init__(self, path: str, repo: str):
        self.path, self.repo = path, repo
        super().__init__(
            f"'{path}' was archived to {repo} and is not current — not maintained, "
            f"not runnable. Read it there deliberately if you mean to; do not run it."
        )


class PathDropped(PathRetired):
    """The content was deliberately removed and archived nowhere."""

    def __init__(self, path: str, reason: str = ""):
        self.path = path
        super().__init__(
            f"'{path}' was deliberately removed and not archived"
            + (f" ({reason})" if reason else "")
            + ". Git history is the record: git log --follow -- "
            + path
        )


class PathHasMoved(Exception):
    """Raised when a path resolves to a repo that is not present on this host.

    Deliberately an exception rather than a None: the caller must decide what to
    do, and cannot accidentally treat it as "file absent".
    """

    def __init__(self, path: str, repo: str, expected_root: Path):
        self.path, self.repo, self.expected_root = path, repo, expected_root
        super().__init__(
            f"'{path}' now lives in {repo}, which is not checked out at "
            f"{expected_root}. Clone it, or set BONSAI_REPOS_ROOT."
        )


class RegistryUnreadable(Exception):
    """The split registry changed and the new content will not parse.

    Raised rather than quietly keeping the last good copy. A router serving
    stale routes is the failure this module exists to prevent, one level up:
    the POINTER goes stale instead of the file, and a stale pointer is
    indistinguishable from a path that was never registered.
    """


class RoutingLoop(Exception):
    """Adopted entries that send a path round in a circle: refused, never followed until something gives."""


class Router:
    """Routes on a split registry. The table's `hub` (its 0.5.0) names the repo that holds every path with no `adopted`
    entry. Without one, the old reading holds: mono is the hub, or `served_root`, when the caller names the garden it
    serves (a garden served from itself is its own hub, as it always was). The origin served from its pot names mono as
    its hub, and a path that has not moved resolves there, in a sibling checkout (the 0.8.2 acceptance run, 2026-10-17)."""

    def __init__(self, registry_path: Path, repos_root: Path = _DEFAULT_SIBLING_ROOT, served_root: Path | None = None):
        self.repos_root = Path(repos_root)
        self.served_root = Path(served_root).resolve() if served_root else None
        self.hub: str | None = None
        self.registry_path = Path(registry_path)
        self._rules: list[tuple[str, str, str, str]] = []  # (path, repo, target_path, status)
        self._foreign: dict[str, list[tuple[str, str, str, str]]] = {}  # source-repo -> its rules (0.6.0)
        self._stamp: tuple[int, int] | None = None  # (mtime_ns, size)
        self._load(self.registry_path)

    @property
    def mono_root(self) -> Path:
        """Where a path that has not moved lives: the hub's checkout (the name is from when mono was the only hub)."""
        if self.hub:
            return self.repos_root / self.hub
        return self.served_root or self.repos_root / MONO_SLUG

    def hub_is_served(self) -> bool:
        """Is the hub the garden being served? Then a path that has not moved is that garden's own, as before."""
        return self.served_root is None or self.mono_root.resolve() == self.served_root

    def _current_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.registry_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _maybe_reload(self) -> None:
        """Re-read the registry if it changed on disk since the last load.

        One stat() per resolve. That is cheap, and it buys the thing the
        garden kept paying for otherwise: the gap was found live when a
        registry edit recording Bonsai's move to core had no effect until the
        MCP restarted, so a read of the old address returned "does not exist"
        — exactly the silent miss the routing table was written to prevent.

        A partially-written registry is retried once before giving up, because
        json.dump() is not atomic and a reader can land mid-write. If it still
        will not parse, this raises rather than keeping the stale table.
        """
        stamp = self._current_stamp()
        if stamp is None or stamp == self._stamp:
            return
        for attempt in (0, 1):
            try:
                self._load(self.registry_path)
                return
            except (json.JSONDecodeError, OSError) as exc:
                if attempt == 0:
                    time.sleep(0.05)  # probably caught mid-write; look again
                    continue
                raise RegistryUnreadable(
                    f"{self.registry_path} changed but will not parse: {exc}. Refusing to route on a stale table."
                ) from None

    def _load(self, registry_path: Path) -> None:
        stamp = self._current_stamp()
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        hub = data.get("hub")
        if hub is not None and not _is_slug(hub):
            raise json.JSONDecodeError(f"`hub` must be one repo slug, not {hub!r}", "", 0)
        hub_slug = hub or MONO_SLUG
        rules: list[tuple[str, str, str, str]] = []
        foreign: dict[str, list[tuple[str, str, str, str]]] = {}
        for e in data.get("entries", []):
            repo = e.get("source-repo")
            if repo is not None and repo != hub_slug:
                # 0.6.0: a path in another repo's namespace. Malformed is refused whole, as a bad `hub` is: an entry the
                # router half-reads is a route nobody can see.
                where = e.get("id") or e.get("source-path") or "?"
                if not _is_slug(repo):
                    raise json.JSONDecodeError(
                        f"entry {where}: `source-repo` must be one repo slug, not {repo!r}", "", 0
                    )
                if e.get("current-path"):
                    raise json.JSONDecodeError(
                        f"entry {where}: from {repo}, it carries `current-path`, which every router older than 0.6.0 "
                        "reads as a path in the hub; its path is `source-path`",
                        "",
                        0,
                    )
                sp = (e.get("source-path") or "").strip("/")
                if not sp or ".." in Path(sp).parts:
                    raise json.JSONDecodeError(f"entry {where}: from {repo}, it names no usable `source-path`", "", 0)
                foreign.setdefault(repo, []).append(
                    (
                        sp,
                        e.get("target-repo") or "",
                        (e.get("target-path") or "").strip("/"),
                        e.get("status") or "pending",
                    )
                )
                continue
            src = e.get("current-path")
            if not src:
                continue
            # Statuses that mean something to routing:
            #   adopted     -> the target repo holds it now; route there
            #   archived    -> retired to the archive repo; refuse, and say where
            #   dropped     -> deliberately deleted, archived nowhere; refuse
            #   distributed -> a derived copy exists downstream, mono is still
            #                  authoritative; resolve to mono. This one matters:
            #                  core/site hold ADAPTED copies, not the originals
            #                  (core's README differs from mono's by 394 lines),
            #                  so routing there would serve different content
            #                  while reporting success.
            #   pending / skipped / anything else -> mono
            rules.append(
                (
                    src.rstrip("/"),
                    e.get("target-repo") or MONO_SLUG,
                    (e.get("target-path") or src).rstrip("/"),
                    (e.get("status") or "pending"),
                )
            )
        # Longest prefix first, so `agents/shared` beats `agents`.
        rules.sort(key=lambda r: len(r[0]), reverse=True)
        for rs in foreign.values():
            rs.sort(key=lambda r: len(r[0]), reverse=True)
        # Swap in only after a clean parse: a half-built table is worse than
        # the old one, and this way a failed reload cannot leave a torn state.
        self._rules = rules
        self._foreign = foreign
        self.hub = hub
        self._stamp = stamp

    def logical_paths(self, repo: str, rel: str) -> list[str]:
        """The garden paths an `adopted` entry maps onto `rel` inside `repo` (a moved file's old addresses), so a
        caller holding a file can ask the router whether that file is the one a read of that address would reach."""
        self._maybe_reload()
        rel = rel.strip("/")
        out = []
        for src, r, target, status in self._rules:
            if status == "adopted" and r == repo and (rel == target or rel.startswith(target + "/")):
                out.append((src + rel[len(target) :]).strip("/"))
        return out

    def _match(self, path: str) -> tuple[str, str, str, bool] | None:
        p = path.strip("/")
        for rule in self._rules:
            src = rule[0]
            if p == src or p.startswith(src + "/"):
                return rule
        return None

    def resolve(self, path: str) -> Resolution:
        """Resolve a repo-relative path to the repo that holds it today."""
        if ".." in Path(path).parts:
            raise ValueError(f"Path '{path}' contains parent traversal, which is not allowed.")

        self._maybe_reload()
        rule = self._match(path)
        status = rule[3] if rule else "pending"

        if status == "archived":
            raise PathArchived(path, rule[1])
        if status == "dropped":
            raise PathDropped(path)

        if rule is None or status != "adopted":
            # No rule, or not moved yet (pending / distributed / skipped): the hub still holds it.
            hub_root = self.mono_root
            bound = _bound(rule, path) if status == "pending" else None
            if self.hub_is_served():
                return Resolution(
                    self.hub or MONO_SLUG,
                    hub_root,
                    (hub_root / path).resolve(),
                    rule[0] if rule else None,
                    moved=False,
                    bound_for=bound,
                )
            # Served from elsewhere (the origin's pot): the hub is a sibling checkout, and a path in it is not the
            # served garden's own. Missing, it is a refusal that names it, never a read of the served garden instead.
            if not hub_root.is_dir():
                raise PathHasMoved(path, self.hub, hub_root)
            return Resolution(
                self.hub, hub_root, (hub_root / path).resolve(), rule[0] if rule else None, moved=True, bound_for=bound
            )

        src, repo, target, _ = rule
        suffix = path.strip("/")[len(src) :].lstrip("/")
        if repo in self._foreign:
            # the target repo has entries of its own (0.6.0): the path may have moved on from there; follow it
            res = self._resolve_foreign(repo, f"{target}/{suffix}" if suffix else target, hops=1)
            return Resolution(res.repo, res.root, res.path, src, moved=True, bound_for=res.bound_for)
        root = self.repos_root / repo
        if not root.is_dir():
            raise PathHasMoved(path, repo, root)
        return Resolution(
            repo, root, (root / target / suffix).resolve() if suffix else (root / target).resolve(), src, moved=True
        )

    def resolve_in(self, repo: str, path: str) -> Resolution:
        """Resolve a path in `repo`'s own namespace (0.6.0, `source-repo`). The hub's namespace is resolve()'s,
        unchanged: resolve_in(<the hub>, p) is resolve(p)."""
        if ".." in Path(path).parts:
            raise ValueError(f"Path '{path}' contains parent traversal, which is not allowed.")
        self._maybe_reload()
        if repo == (self.hub or MONO_SLUG):
            return self.resolve(path)
        if not _is_slug(repo):
            raise ValueError(f"'{repo}' is not a repo slug")
        return self._resolve_foreign(repo, path, hops=0)

    def _resolve_foreign(self, repo: str, path: str, hops: int) -> Resolution:
        p = path.strip("/")
        rule = next((r for r in self._foreign.get(repo, []) if p == r[0] or p.startswith(r[0] + "/")), None)
        status = rule[3] if rule else "pending"
        if status == "archived":
            raise PathArchived(f"{repo}:{p}", rule[1] or "the archive")
        if status == "dropped":
            succ = _bound(rule, p)
            raise PathDropped(p, f"in {repo}" + (f"; its successor: {succ}" if succ else ""))
        if rule is None or status != "adopted":
            # not moved yet (or no entry): the repo itself still holds it, and a pending entry says where it is bound
            root = self.repos_root / repo
            if not root.is_dir():
                raise PathHasMoved(p, repo, root)
            bound = _bound(rule, p) if status == "pending" else None
            return Resolution(repo, root, (root / p).resolve(), rule[0] if rule else None, moved=False, bound_for=bound)
        src, trepo, target, _ = rule
        if not trepo or not target:
            raise PathHasMoved(p, trepo or "(no target named)", self.repos_root / (trepo or "?"))
        suffix = p[len(src) :].lstrip("/")
        tpath = f"{target}/{suffix}" if suffix else target
        if hops >= 8:
            raise RoutingLoop(f"'{repo}:{p}' is still being routed after 8 hops: the adopted entries form a loop")
        if trepo == (self.hub or MONO_SLUG):
            res = self.resolve(tpath)
        elif trepo in self._foreign:
            res = self._resolve_foreign(trepo, tpath, hops + 1)
        else:
            root = self.repos_root / trepo
            if not root.is_dir():
                raise PathHasMoved(p, trepo, root)
            res = Resolution(trepo, root, (root / tpath).resolve(), src, moved=True)
        return Resolution(res.repo, res.root, res.path, src, moved=True, bound_for=res.bound_for)


def _is_slug(s) -> bool:
    return isinstance(s, str) and bool(s) and "/" not in s and s not in (".", "..")


def _bound(rule: tuple[str, str, str, str] | None, path: str) -> str | None:
    """`<repo>:<path>`: where a rule sends `path`, or None when the rule names no target."""
    if not rule or not rule[1] or not rule[2]:
        return None
    suffix = path.strip("/")[len(rule[0]) :].lstrip("/")
    return f"{rule[1]}:{rule[2]}/{suffix}" if suffix else f"{rule[1]}:{rule[2]}"


def _self_test() -> int:
    """Run against the real registry. Exits non-zero on any failure."""
    import tempfile

    garden = Path(os.environ.get("BONSAI_GARDEN", Path.home() / "dev/agentic/bonsai-garden"))
    reg = garden / "rituals/repo/repo-split-registry.json"
    r = Router(reg)
    fails = []

    def check(label, cond):
        print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
        if not cond:
            fails.append(label)

    res = r.resolve("agents/kin-a/about-me.md")
    check("pending entry still resolves to mono", res.repo == MONO_SLUG and not res.moved)
    check("mono path is under the mono root", str(res.path).startswith(str(r.mono_root)))

    res2 = r.resolve("some/path/nobody/registered.md")
    check("unregistered path falls back to mono", res2.repo == MONO_SLUG and res2.rule is None)

    try:
        r.resolve("../etc/passwd")
        check("traversal rejected", False)
    except ValueError:
        check("traversal rejected", True)

    # Longest-prefix, tested against a FIXTURE rather than the live registry.
    # The earlier version asserted that "agents/shared/codex-workshop.md" matched
    # the rule "agents/shared" — true until a per-file entry for that exact path
    # was added, at which point the test failed while the logic was working
    # perfectly. A self-test that breaks when the DATA changes, rather than when
    # the LOGIC breaks, teaches people to ignore it.
    with tempfile.TemporaryDirectory() as td2:
        fx = Path(td2) / "fixture.json"
        fx.write_text(
            json.dumps(
                {
                    "entries": [
                        {"current-path": "agents", "target-repo": "p", "status": "pending"},
                        {"current-path": "agents/shared", "target-repo": "c", "status": "pending"},
                        {"current-path": "agents/shared/codex.md", "target-repo": "c", "status": "pending"},
                    ]
                }
            )
        )
        rf = Router(fx, repos_root=Path(td2))
        check(
            "longest prefix wins: exact file beats its directory",
            rf._match("agents/shared/codex.md")[0] == "agents/shared/codex.md",
        )
        check(
            "longest prefix wins: agents/shared beats agents", rf._match("agents/shared/other.md")[0] == "agents/shared"
        )
        check("shortest applicable prefix still matches", rf._match("agents/kin-a/about-me.md")[0] == "agents")

    # simulate an adopted entry without touching the real registry
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "reg.json"
        fake.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "current-path": "lore",
                            "target-repo": "bonsai-garden-core",
                            "target-path": "lore",
                            "status": "adopted",
                        }
                    ]
                }
            )
        )
        r2 = Router(fake, repos_root=garden.parent)
        moved = r2.resolve("lore/cosmology.md")
        check(
            "adopted entry routes to the other repo",
            moved.repo == "bonsai-garden-core" and moved.moved and moved.path.name == "cosmology.md",
        )

        # archived / dropped / distributed
        fake2 = Path(td) / "reg2.json"
        fake2.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "current-path": "tools/old.sh",
                            "target-repo": "bonsai-garden-archive",
                            "target-path": "tools/old.sh",
                            "status": "archived",
                        },
                        {
                            "current-path": "tools/gone.bak",
                            "target-repo": "bonsai-garden-archive",
                            "target-path": "tools/gone.bak",
                            "status": "dropped",
                        },
                        {
                            "current-path": "lore",
                            "target-repo": "bonsai-garden-core",
                            "target-path": "lore",
                            "status": "distributed",
                        },
                    ]
                }
            )
        )
        r4 = Router(fake2, repos_root=garden.parent)
        try:
            r4.resolve("tools/old.sh")
            check("archived refuses", False)
        except PathArchived as exc:
            check("archived refuses and names the archive repo", "bonsai-garden-archive" in str(exc))
        try:
            r4.resolve("tools/gone.bak")
            check("dropped refuses", False)
        except PathDropped as exc:
            check("dropped refuses and points at git history", "git log" in str(exc))
        dist = r4.resolve("lore/cosmology.md")
        check("distributed still resolves to mono (mono is authoritative)", dist.repo == MONO_SLUG and not dist.moved)

        r3 = Router(fake, repos_root=Path(td) / "nowhere")
        try:
            r3.resolve("lore/cosmology.md")
            check("missing target repo raises rather than returning nothing", False)
        except PathHasMoved:
            check("missing target repo raises rather than returning nothing", True)

    # --- a registry edit must take effect without a restart ----------------
    with tempfile.TemporaryDirectory() as td3:
        reg3 = Path(td3) / "live.json"
        reg3.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "current-path": "lore",
                            "target-repo": "bonsai-garden-core",
                            "target-path": "lore",
                            "status": "pending",
                        }
                    ]
                }
            )
        )
        rl = Router(reg3, repos_root=garden.parent)
        check("before the edit, pending resolves to mono", rl.resolve("lore/cosmology.md").repo == MONO_SLUG)

        reg3.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "current-path": "lore",
                            "target-repo": "bonsai-garden-core",
                            "target-path": "lore",
                            "status": "adopted",
                        }
                    ]
                }
            )
        )
        check(
            "after the edit, the SAME router picks it up (no restart)",
            rl.resolve("lore/cosmology.md").repo == "bonsai-garden-core",
        )

        reg3.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "current-path": "tools/x.sh",
                            "target-repo": "bonsai-garden-archive",
                            "target-path": "tools/x.sh",
                            "status": "archived",
                        }
                    ]
                }
            )
        )
        try:
            rl.resolve("tools/x.sh")
            check("a later edit is picked up too", False)
        except PathArchived:
            check("a later edit is picked up too", True)

        # a registry that stops parsing must refuse, not serve the stale table
        reg3.write_text("{ this is not json")
        try:
            rl.resolve("lore/cosmology.md")
            check("unparseable registry refuses rather than routing on stale rules", False)
        except RegistryUnreadable:
            check("unparseable registry refuses rather than routing on stale rules", True)

        # and an UNCHANGED registry must not be re-read on every call
        reg3.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "current-path": "lore",
                            "target-repo": "bonsai-garden-core",
                            "target-path": "lore",
                            "status": "pending",
                        }
                    ]
                }
            )
        )
        rl.resolve("lore/a.md")
        loads = []
        _orig = rl._load
        rl._load = lambda q, _o=_orig: (loads.append(1), _o(q))[1]
        for _ in range(5):
            rl.resolve("lore/a.md")
        rl._load = _orig
        check("an unchanged registry is not reloaded on every resolve", loads == [])

    # --- 0.6.0: an entry from another repo routes that repo's path, never the hub's (test_source_repo_routing.py
    # holds the rest) --------------------------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as td4:
        for repo in (MONO_SLUG, "rx", "px"):
            (Path(td4) / repo).mkdir()
        reg4 = Path(td4) / "src.json"
        reg4.write_text(
            json.dumps(
                {
                    "hub": MONO_SLUG,
                    "entries": [
                        {
                            "source-repo": "rx",
                            "source-path": "tools/finder",
                            "type": "dir",
                            "target-repo": "px",
                            "target-path": "tools/finder",
                            "status": "pending",
                        },
                    ],
                }
            )
        )
        rs = Router(reg4, repos_root=Path(td4))
        own = rs.resolve_in("rx", "tools/finder/app.swift")
        check(
            "a pending entry from another repo resolves there, bound for its target",
            own.repo == "rx" and not own.moved and own.bound_for == "px:tools/finder/app.swift",
        )
        hub = rs.resolve("tools/finder/app.swift")
        check("… and never routes the hub's path of the same name", hub.repo == MONO_SLUG and hub.rule is None)

    print(f"\n  {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] in (["-h"], ["--help"]):
        print(__doc__)
        raise SystemExit(0)
    raise SystemExit(_self_test())
