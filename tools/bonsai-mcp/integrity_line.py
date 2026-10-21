#!/usr/bin/env python3
# ---
# id: 01M3J0SXA6Z8QZ5ZRXFJE2HPJ5
# title: Integrity line — the repo-copy integrity check, run by session start
# type: script
# tended-by: baisai
# created: 2026-09-27
# ---
"""
integrity_line.py — the repo-copy integrity check, as one line of bonsai_session_start's env block.

AGENTS.md, "Every kin": a seat entering the repo copy (no MANIFEST.json, no scripts/integrity/) runs
three checks and stops on anything but ok / PASS:
  1. sign.py verify-sidecar on the kin's SKILL.md,
  2. the same on its constitution (references/about-me.md),
  3. core's shintai conformance gate on the body.
The ask of 2026-09-27: session start runs them itself and prints one `integrity:` line, so the
clause shrinks to *read the line*. This file is the one definition of that line, used by the env
block and as a command for the crew.

    integrity: ok · SKILL.md ok · about-me.md ok · shintai PASS (garden keys)
    integrity: STOP — SKILL.md FAIL [hash-mismatch] · shintai FAIL (1 finding: …) — use nothing; tell the gardener

Fail-safe, never fall back: a check that cannot run (a tool missing, a timeout, a slug the registry
does not know, a body the router cannot place) is a STOP that names why, never an ok and never
`unknown`. The installed skill against the repo's body is the `skill:` line's job, not this one's.

Where the body is: the registry's `path` (a `shared:<slug>` shintai is that kin's body), through the
repo router, so a body that moved to core is checked where it lives. A body in this repo is
checked with the garden's keys; a body in another repo with the published keys, since it carries
seals whose keys that repo does not hold (the gate's own --keys help, Bonsai's finding 2026-09-24).

    python3 integrity_line.py <slug> [<slug> …]     # one line each; exit 1 if any is not ok
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import signer as _signer

# (repo name, absolute path) for a repo-relative path in the mono repo's namespace
Resolver = Callable[[str], "tuple[str, Path]"]

STOP_TAIL = "use nothing; tell the gardener"
NOT_A_KIN = "not a kin: the gardener carries no shintai to check"
# A garden's human, by the registry's type, never by a slug (the gardener, 2026-10-12: replace every literal human slug with
# '<gardener-slug>'). Core's rite writes "gardener". A garden whose registry still names the type otherwise declares that
# name in its own garden.json (`legacy-human-types`), so no garden's word lives in this code.
HUMAN_TYPE = "gardener"


def human_types(repo_root: Path) -> tuple:
    """("gardener", *the served garden's declared legacy names): garden.json `legacy-human-types`, else the host's
    launch config BONSAI_LEGACY_HUMAN_TYPES (comma-separated), for a garden that keeps no garden.json at its root."""
    try:
        g = json.loads((repo_root / "garden.json").read_text("utf-8"))
        extra = g.get("legacy-human-types") if isinstance(g, dict) else None
    except (OSError, ValueError):
        extra = None
    if not extra:
        extra = [t.strip() for t in os.environ.get("BONSAI_LEGACY_HUMAN_TYPES", "").split(",")]
    return (HUMAN_TYPE, *[t for t in (extra or []) if isinstance(t, str) and t and t != HUMAN_TYPE])


def _first(r: subprocess.CompletedProcess) -> str:
    return " ".join(
        (
            (r.stdout or "").strip().splitlines()
            or (r.stderr or "").strip().splitlines()
            or [f"no output, exit {r.returncode}"]
        )[0].split()
    )


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


# --- the gardener's pin for a pot ---------------------------------------------------------------------------------------
# A pot is trusted by a lab-preview declaration or by a pin: the signer fingerprint the GARDENER expects (sign.py
# --expect-fp). The pin lives in the gardener's own home, never in the pot: a pot that carried its own pin would vouch for
# itself, which is exactly what the verdict refuses. ~/.bonsai-garden/pins/<garden-id>.json = {"garden": id, "pins":
# {slug: fp32}}, written once by the gardener's hand (`integrity_line.py pin`). Missing, unreadable or malformed means no
# pin, and a pot with no pin is `pot-untrusted`: never a guess, never a fall-back to the pot's own registry.
_FP32 = re.compile(r"^[0-9a-f]{32}$")


def pins_file(garden_id: str) -> Path:
    base = os.environ.get("BONSAI_PINS_DIR") or os.path.join(os.path.expanduser("~"), ".bonsai-garden", "pins")
    return Path(base) / f"{garden_id}.json"


def _garden_id(pot: Path) -> str | None:
    try:
        gid = json.loads((pot / "garden.json").read_text("utf-8")).get("garden")
    except (OSError, ValueError, AttributeError):
        return None
    return gid if isinstance(gid, str) and re.fullmatch(r"[0-9A-Za-z]{10,40}", gid) else None


def pin_for(pot: Path, slug: str) -> str | None:
    """The gardener's pinned signer fingerprint (32 hex) for `slug` in `pot`, or None."""
    gid = _garden_id(pot)
    if not gid:
        return None
    try:
        doc = json.loads(pins_file(gid).read_text("utf-8"))
        fp = (doc.get("pins") or {}).get(slug)
    except (OSError, ValueError, AttributeError):
        return None
    return fp.lower() if isinstance(fp, str) and _FP32.match(fp.lower()) else None


def write_pin(pot: Path, slug: str, fp: str) -> Path:
    gid = _garden_id(pot)
    if not gid:
        raise ValueError(f"{pot}/garden.json carries no garden id")
    fp = fp.strip().lower()
    if not _FP32.match(fp):
        raise ValueError("a pin is the full 32-hex signer fingerprint")
    f = pins_file(gid)
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc = {"garden": gid, "pins": {**(doc.get("pins") or {}), slug: fp}}
    f.write_text(json.dumps(doc, indent=2) + "\n", "utf-8")
    os.chmod(f, 0o600)
    return f


def _pin_cli(argv: list) -> int:
    """integrity_line.py pin <pot> <slug>: the gardener types the fingerprint to pin; it is saved only if it is the one the
    pot's registry shows for that kin. Confirming the value the guardian presents is fine: who looks the fingerprint up is a
    convenience question, not a security one (the gardener, 2026-10-20, runbook 7k: "It does not matter who searches for
    that fingerprint"). The pin guards the pot against a change after the planting, not against the hand that planted it."""
    if len(argv) != 2:
        print("usage: integrity_line.py pin <pot dir> <slug>")
        return 2
    pot, slug = Path(argv[0]).resolve(), argv[1]
    try:
        reg = json.loads((pot / "agents" / "registry.json").read_text("utf-8"))
        ent = next(a for a in (reg.get("agents", []) if isinstance(reg, dict) else reg) if a.get("slug") == slug)
    except (OSError, ValueError, StopIteration):
        print(f"{slug!r} is not in {pot}/agents/registry.json")
        return 1
    shown = str(ent.get("fingerprint") or ent.get("fp") or "").lower()
    typed = (
        input(
            f"Pin for {slug} in {pot.name}. Type the full 32-hex fingerprint to pin (confirming the one the guardian presents is fine): "
        )
        .strip()
        .lower()
    )
    if not _FP32.match(typed):
        print("not a 32-hex fingerprint; nothing written")
        return 1
    if shown and typed != shown:
        print(
            f"MISMATCH: you typed {typed[:8]}…, the pot's registry shows {shown[:8]}…. Nothing written. One of them is wrong; find out which before pinning."
        )
        return 1
    print(f"pinned {slug} = {typed[:8]}… in {write_pin(pot, slug, typed)}")
    return 0


def body_of(repo_root: Path, slug: str, resolve: Resolver) -> tuple[Path | None, bool, str]:
    """(body dir, lives in this repo?, why not) for a kin, from the registry and the router."""
    try:
        reg = json.loads((repo_root / "agents" / "registry.json").read_text("utf-8"))
    except (OSError, ValueError) as e:
        return None, False, f"the registry is unreadable ({type(e).__name__})"
    entries = {a.get("slug"): a for a in (reg.get("agents", []) if isinstance(reg, dict) else reg)}
    a = entries.get(slug)
    if a is None:
        return None, False, f"{slug!r} is not in agents/registry.json"
    if a.get("type") in human_types(repo_root):
        return None, False, NOT_A_KIN
    shintai = str(a.get("shintai") or "")
    if shintai.startswith("shared:"):
        a = entries.get(shintai.split(":", 1)[1]) or {}
    rel = a.get("path")
    if not rel:
        return None, False, f"{slug!r} has no body path in the registry"
    try:
        repo, skill = resolve(f"{rel}/SKILL.md")
    except Exception as e:  # PathHasMoved, PathRetired, RegistryUnreadable: each is a STOP
        return None, False, f"the router will not place {rel}/SKILL.md ({type(e).__name__}: {e})"
    if not skill.is_file():
        return None, False, f"no SKILL.md at {skill}"
    return skill.parent, repo == repo_root.name, ""


def line(
    repo_root: Path,
    slug: str,
    resolve: Resolver,
    *,
    sign_py: Path | None = None,
    conformance: Path | None = None,
    timeout: float = 45.0,
    pot: Path | None = None,
    pin: str | None = None,
    no_core: bool = False,
    signer=None,
) -> str:
    """The integrity line for one kin, without the `integrity: ` key.

    `pot`: the daemon is serving a POT (a garden that is not the one it runs from). SKILL.md and the constitution are then
    verified against the pot's own registry and keys (`sign.py verify-sidecar --keys pot --pot <dir>`, the ruling of
    2026-10-11), a pot's yes reads `pot-ok`, never `ok`, and a lab-preview pot says so in the line, so a rehearsal's green
    never reads as production's. `pin` is the signer's fingerprint a caller pins for a pot that is not lab-preview."""
    if not slug:
        return "no agent slug given"
    try:
        body, local, why = body_of(repo_root, slug, resolve)
        if why == NOT_A_KIN:
            return why
        if body is None:
            return f"STOP — {why} — {STOP_TAIL}"
        if pot is not None and no_core:
            # the checker is the core the pot is WIRED to (git config bonsai.core); none wired is never answered by a guess
            return f"STOP — cannot check: no core wired to this pot (git config bonsai.core is unset in {pot}) — {STOP_TAIL}"
        # The verifier is the SIGNER's (signer.py): mono's sign.py for the origin's own garden, the wired core's Node verifier
        # for a pot. A caller that names `sign_py` (the tests, the CLI) gets the Python one, as before.
        signer = signer or _signer.PySigner(sign_py or repo_root / "tools" / "sign.py")
        conformance = conformance or repo_root.parent / "bonsai-garden-core" / "tools" / "check-shintai-conformance.py"
        gone = signer.missing()
        if gone:
            return f"STOP — cannot check: {gone} — {STOP_TAIL}"
        if not conformance.is_file():
            return f"STOP — cannot check: {conformance} is missing — {STOP_TAIL}"
        abouts = sorted((body / "references").glob("about-me.*"))
        about = next((p for p in abouts if p.suffix == ".md"), abouts[0] if abouts else None)
        if about is None:
            return f"STOP — no constitution (references/about-me.*) in {body} — {STOP_TAIL}"
        keys = "pot" if pot is not None else ("garden" if local else "published")
        py = sys.executable or "python3"
        potflags = []
        lab = False
        if pot is not None:
            # the body holds seals by more than one signer (a custodian's, a sibling's): the shintai check pins each seal by
            # its own signer from the gardener's pin file, when there is one; one --expect-fp pinned only the kin, and every
            # other seal read pin-mismatch (the 0.8.2 acceptance run, 2026-10-17)
            gid = _garden_id(Path(pot))
            pf = pins_file(gid) if gid else None
            potflags = ["--keys", "pot", "--pot", str(pot)] + (
                ["--pins", str(pf)]
                if pin and pf is not None and pf.is_file()
                else (["--expect-fp", pin] if pin else [])
            )
            try:
                lab = json.loads((Path(pot) / "garden.json").read_text("utf-8")).get("lab-preview") is True
            except (OSError, ValueError):
                lab = False
        vp = pot if pot is not None else None
        jobs = {
            "SKILL.md": signer.verify_argv(body / "SKILL.md", pot=vp, pin=pin),
            about.name: signer.verify_argv(about, pot=vp, pin=pin),
            "shintai": [
                py,
                str(conformance),
                *(potflags if pot is not None else ["--keys", keys]),
                str(body),
            ],
        }
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = {k: ex.submit(_run, argv, timeout) for k, argv in jobs.items()}
            res = {}
            for k, f in futs.items():
                try:
                    res[k] = f.result()
                except subprocess.TimeoutExpired:
                    res[k] = None
        parts, ok = [], True
        for k in ("SKILL.md", about.name):
            r = res[k]
            if r is None:
                ok = False
                parts.append(f"{k} not checked (timed out after {timeout:.0f}s)")
            elif r.returncode == 0 and _first(r).startswith("ok"):
                parts.append(f"{k} ok")
            elif pot is not None and r.returncode == 0 and _first(r).startswith("pot-ok"):
                parts.append(f"{k} pot-ok")
            else:
                ok = False
                f = _first(r)
                bare = " ".join(f.replace(str(body / k), "").replace(str(about), "").split())
                parts.append(f"{k} {bare or f}")
        r = res["shintai"]
        if r is None:
            ok = False
            parts.append(f"shintai not checked (timed out after {timeout:.0f}s)")
        else:
            out = (r.stdout or "").splitlines()
            findings = [ln[4:] for ln in out if ln.startswith("  - ")]
            if r.returncode == 0 and out and out[0].startswith("PASS"):
                parts.append(f"shintai PASS ({keys} keys)")
            elif pot is not None and r.returncode == 0 and out and out[0].startswith("POT-PASS"):
                parts.append(f"shintai POT-PASS ({keys} keys)")  # never PASS: a rehearsal's green is not production's
            else:
                ok = False
                shown = findings[0].replace(str(body) + "/", "") if findings else _first(r)
                n = len(findings)
                parts.append(
                    f"shintai FAIL ({keys} keys; {n} finding{'s' if n != 1 else ''}"
                    f"{': ' + shown if shown else ''}{'; …' if n > 1 else ''})"
                )
        if pot is not None:  # a pot's line says what vouched; a rehearsal says it is one
            parts.insert(
                0,
                "lab-preview pot (a rehearsal, not production)"
                if lab
                else (
                    "pot (the pot's own keys, pinned)"
                    if pin
                    else "pot (the pot's own keys, neither lab-preview nor pinned)"
                ),
            )
        return ("ok · " + " · ".join(parts)) if ok else f"STOP — {' · '.join(parts)} — {STOP_TAIL}"
    except Exception as e:
        return f"STOP — the integrity check itself failed ({type(e).__name__}: {e}) — {STOP_TAIL}"


def _cli_resolver(repo_root: Path) -> Resolver:
    """The router the server uses; without it, this repo only (a moved body then reads STOP)."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    try:
        import repo_routing as rr

        router = rr.Router(
            repo_root / "rituals" / "repo" / "repo-split-registry.json",
            repos_root=repo_root.parent,
            served_root=repo_root,
        )
        return lambda rel: (lambda x: (x.repo, x.path))(router.resolve(rel))
    except Exception:
        return lambda rel: (repo_root.name, repo_root / rel)


def _garden_root() -> Path:
    for d in Path(__file__).resolve().parents:
        if (d / "agents" / "registry.json").is_file():
            return d
    sys.exit("garden root not found (no agents/registry.json above this file)")


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):
        print(__doc__)
        sys.exit(0)
    if sys.argv[1:2] == ["pin"]:
        sys.exit(_pin_cli(sys.argv[2:]))
    root = _garden_root()
    slugs = sys.argv[1:]
    if not slugs:
        sys.exit(__doc__.strip().splitlines()[-1].strip())
    res = _cli_resolver(root)
    bad = 0
    for s in slugs:
        v = line(root, s, res)
        bad += not v.startswith("ok")
        print(f"{s}: integrity: {v}")
    sys.exit(1 if bad else 0)
