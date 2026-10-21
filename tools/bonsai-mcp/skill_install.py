#!/usr/bin/env python3
# ---
# id: 01M36HB5Q2S8KXW4T7NDR3YVBA
# title: Skill install check — is the installed SKILL.md the repo's body?
# type: script
# tended-by: baisai
# created: 2026-09-23
# updated: 2026-10-21
# ---
"""
skill_install.py — is the SKILL.md a harness actually loaded the same body as the repo's?

The one definition of that check, used in two places so the answer cannot drift:
  * bonsai_session_start's `=== env ===` block, line `skill:` — every boot reports it;
  * this file as a command, for the whole crew after an upload.

Why a whole-file shasum is the wrong check (the gardener, 2026-09-23: *"I understand, but that
understanding needs to be baked in"*): the builder (core,
skills/bonsai-garden-core-plugin/scripts/package.py, since the builder's cutover) rewrites the frontmatter on purpose —
it keeps only name / description / version (the surface the skill loader accepts) and drops
id: and version-locked-with:. So an installed SKILL.md never shasums equal to the repo's,
and "different" says nothing. The body below the frontmatter is what must match.

THE CONTRACT (the builder must keep to it):
  body       = everything after the closing `---` line of the leading frontmatter block;
               the whole text if there is no frontmatter.
  stamp      = an optional FIRST body line starting with STAMP_PREFIX, followed by exactly
               one blank line. The builder may insert it; it names the installed build
               (M.m.p.N-<UTC>Z). It is excluded from the body hash and reported on its own.
  body hash  = sha256 over the body's UTF-8 bytes, stamp excluded.

Installed copies are looked for where the harnesses keep them on this host:
  * Claude Desktop / Cowork:  ~/Library/Application Support/Claude/local-agent-mode-sessions/
                              skills-plugin/*/*/skills/<slug>/SKILL.md
  * Claude Code:              ~/.claude/skills/<slug>/SKILL.md
  * Claude Code / Desktop plugins (the two-plugin install since 2026-10-05):
                              ~/.claude/plugins/marketplaces/*/*/skills/<slug>/SKILL.md
  * extra roots (colon-separated) in $BONSAI_SKILL_STORES, each holding <slug>/SKILL.md
A host with none of these says "not installed on this host", never "current".

Usage:
  python3 tools/bonsai-mcp/skill_install.py <slug> <slug>   # named slugs
  python3 tools/bonsai-mcp/skill_install.py --all            # every registry slug with a SKILL.md
  python3 tools/bonsai-mcp/skill_install.py --integrity      # every installed body that carries a manifest
Exit 1 if any installed copy DRIFTS from the repo body; 0 otherwise.

--integrity (the gardener's first go-live check, "an untampered file"): for each installed body
with a MANIFEST.json, recompute the sha256 of every installed file against the manifest, flag
any file the manifest does not list, and verify the manifest's seal with sign.py — signed by
the body's OWNER, at the owner's registry fingerprint. Checked on the installed bytes, i.e.
what the harness actually loaded. Exit 1 on any failure.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

STAMP_PREFIX = "> Installed build:"


def split_skill(text: str) -> tuple[str, str | None]:
    """(body without stamp, stamp-or-None) per the contract above."""
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            body = text[end + len("\n---\n") :]
    stamp = None
    first, _sep, rest = body.partition("\n")
    if first.startswith(STAMP_PREFIX):
        stamp = first[len(STAMP_PREFIX) :].strip()
        body = rest[1:] if rest.startswith("\n") else rest
    return body, stamp


def body_hash(text: str) -> str:
    return hashlib.sha256(split_skill(text)[0].encode("utf-8")).hexdigest()


def installed_paths(slug: str) -> list[Path]:
    home = Path.home()
    pats = [
        str(
            home
            / "Library/Application Support/Claude/local-agent-mode-sessions"
            / "skills-plugin"
            / "*"
            / "*"
            / "skills"
            / slug
            / "SKILL.md"
        ),
        str(home / ".claude" / "skills" / slug / "SKILL.md"),
        # a plugin's skills (bon-sai-team, the bonsai-garden-core plugin): the store the desktop app
        # installs an uploaded .plugin into. Missed until 2026-10-05, when the line said
        # `current` from a stale Cowork copy while the plugin install went unread.
        str(home / ".claude" / "plugins" / "marketplaces" / "*" / "*" / "skills" / slug / "SKILL.md"),
    ]
    for root in filter(None, os.environ.get("BONSAI_SKILL_STORES", "").split(":")):
        pats.append(str(Path(root).expanduser() / slug / "SKILL.md"))
    found = []
    for pat in pats:
        found += [Path(p) for p in sorted(glob.glob(pat))]
    return found


def repo_skill_path(repo_root: Path, slug: str, resolve=None) -> Path:
    """The body's SKILL.md in the repo, by the registry's own `path` (nested bodies such
    as a sub-agent live at agents/<parent>/<slug>), else agents/<slug>. `resolve` (the server's
    router: a repo-relative path to where it lives) places a body that lives in another repo of the
    garden, as the origin's do when it is served from its pot; without it, the repo itself."""
    rel = f"agents/{slug}/SKILL.md"
    with contextlib.suppress(Exception):  # no registry, or an unreadable one: the conventional path
        reg = json.loads((repo_root / "agents" / "registry.json").read_text("utf-8"))
        rel = next(f"{a['path']}/SKILL.md" for a in reg.get("agents", []) if a.get("slug") == slug and a.get("path"))
    return resolve(rel) if resolve else repo_root / rel


def check(repo_root: Path, slug: str, resolve=None) -> dict:
    """{'status': current|DRIFT|not-installed|no-repo-skill, 'repo': h8, 'installed': [...]}.
    Each installed entry: {'path', 'body': h8, 'match': bool, 'stamp': str|None}."""
    rp = repo_skill_path(repo_root, slug, resolve)
    if not rp.is_file():
        return {"status": "no-repo-skill", "repo": None, "installed": []}
    rh = body_hash(rp.read_text("utf-8"))
    inst = []
    for p in installed_paths(slug):
        try:
            t = p.read_text("utf-8")
        except OSError:
            continue
        h = body_hash(t)
        inst.append({"path": str(p), "body": h[:8], "match": h == rh, "stamp": split_skill(t)[1]})
    if not inst:
        return {"status": "not-installed", "repo": rh[:8], "installed": []}
    status = "current" if all(i["match"] for i in inst) else "DRIFT"
    return {"status": status, "repo": rh[:8], "installed": inst}


def env_line(repo_root: Path, slug: str, resolve=None) -> str:
    """The one-line form session_start prints as `skill:`."""
    r = check(repo_root, slug, resolve)
    if r["status"] == "no-repo-skill":
        return f"no SKILL.md in the repo for {slug!r}"
    if r["status"] == "not-installed":
        return f"repo body {r['repo']} · not installed on this host (no store found)"
    bodies = ",".join(sorted({i["body"] for i in r["installed"]}))
    stamps = sorted({i["stamp"] or "unstamped" for i in r["installed"]})
    word = "current" if r["status"] == "current" else "DRIFT — the installed body is not the repo's"
    return (
        f"installed body {bodies} · repo body {r['repo']} · {word} · "
        f"build {', '.join(stamps)} · {len(r['installed'])} store(s)"
    )


def _garden_root() -> Path:
    env = os.environ.get("BONSAI_GARDEN")
    if env and (Path(env) / "agents" / "registry.json").is_file():
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for d in (here.parent, *here.parents):
        if (d / "agents" / "registry.json").is_file():
            return d
    sys.exit("garden root not found (no agents/registry.json above this file; set BONSAI_GARDEN)")


def integrity(root: Path) -> int:
    """Installed bodies against their own manifests and owner seals. sign.py is imported
    here, in the CLI path only — the MCP daemon never calls this function."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("bonsai_sign", root / "tools" / "sign.py")
    sign = importlib.util.module_from_spec(spec)
    sys.modules["bonsai_sign"] = sign
    spec.loader.exec_module(sign)
    reg = {
        a["slug"]: a
        for a in json.loads((root / "agents" / "registry.json").read_text("utf-8")).get("agents", [])
        if a.get("slug")
    }
    stores = set()
    for pat in installed_paths("*"):
        stores.add(pat.parent.parent)
    rows, failed = [], 0
    for store in sorted(stores):
        for d in sorted(p for p in store.iterdir() if (p / "MANIFEST.json").is_file()):
            m = d / "MANIFEST.json"
            try:
                man = json.loads(m.read_text("utf-8"))
            except Exception as e:
                rows.append(f"{d.name:<11} FAIL  unreadable manifest ({type(e).__name__})")
                failed += 1
                continue
            slug = man.get("slug", d.name)
            listed = {f.split("/", 1)[1]: h for f, h in (man.get("files") or {}).items()}
            own = {m.name, *(c.name for c in sign.sidecar_candidates(m))}  # its seal, under either name
            present = {str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and p.name not in own}
            bad = [
                f
                for f, h in listed.items()
                if not (d / f).is_file() or hashlib.sha256((d / f).read_bytes()).hexdigest() != h
            ]
            extra = sorted(present - set(listed))
            st, meta = sign.verify_sidecar_file(m)
            canon = (reg.get(slug) or {}).get("fingerprint")
            # seal v2: accepted per the committed cutover (a v1 MANIFEST seal passes
            # with a warning until it flips), and a v2 seal must be in the owner role
            m_ = meta or {}
            owner = (
                sign.seal_accepted(st, meta)
                and m_.get("signer") == slug
                and m_.get("fingerprint") == canon
                and m_.get("role", "owner") == "owner"
            )
            if owner and st != "ok":
                print(f"WARN  {slug}: MANIFEST seal {st} — accepted until the v1 cutover; repackage", file=sys.stderr)
            good = not bad and not extra and owner
            failed += not good
            why = (
                ([f"{len(bad)} hash mismatch"] if bad else [])
                + ([f"{len(extra)} unlisted file(s)"] if extra else [])
                + ([] if owner else [f"seal {st}, signer {(meta or {}).get('signer')!r}"])
            )
            rows.append(
                f"{slug:<11} {'UNTAMPERED' if good else 'FAIL':<10} {man.get('build', '?'):<26} "
                f"{len(listed):>3} files  sealed by {((meta or {}).get('fingerprint') or '?')[:8]}"
                + ("" if good else "  — " + "; ".join(why))
            )
    print("\n".join(rows) if rows else "no installed body carries a manifest on this host")
    print(f"\n{len(rows) - failed} of {len(rows)} installed bodies untampered and owner-sealed")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if not argv or argv == ["-h"] or argv == ["--help"]:
        print(__doc__)
        return 0
    root = _garden_root()
    if argv == ["--integrity"]:
        return integrity(root)
    if argv == ["--all"]:
        reg = json.loads((root / "agents" / "registry.json").read_text("utf-8"))
        slugs = [
            a["slug"] for a in reg.get("agents", []) if a.get("slug") and repo_skill_path(root, a["slug"]).is_file()
        ]
    else:
        slugs = argv
    drift = 0
    print(f"{'slug':<12} {'status':<14} {'repo':<9} installed")
    for s in slugs:
        r = check(root, s)
        drift += r["status"] == "DRIFT"
        inst = (
            "  ".join(
                f"{i['body']}{'' if i['match'] else '≠'}{' [' + i['stamp'] + ']' if i['stamp'] else ''}"
                for i in r["installed"]
            )
            or "—"
        )
        print(f"{s:<12} {r['status']:<14} {r['repo'] or '—':<9} {inst}")
    print(f"\n{drift} drifting" if drift else "\nno drift")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
