#!/usr/bin/env python3
# ---
# id: 01M4RNT99VAXNS5FHGTY8Y77JV
# title: signer — which signer a served garden is verified and sealed by (the seam between the daemon and a garden's sign.py)
# type: script
# tended-by: baisai
# created: 2026-10-12
# updated: 2026-10-21
# ---
"""
signer.py — the one place the daemon learns *how* a served garden's seals are read and made.

Until 2026-10-12 the daemon reached the origin's Python `tools/sign.py` directly, in five places, so no garden but the one
that holds that file could be served honestly. Two providers, one interface, chosen by the garden being served:

  PySigner    the origin's own garden: `<tree>/tools/sign.py`, exactly as before.
  CoreSigner  a pot: the core THE POT IS WIRED TO (`git config bonsai.core`), through its Node verifier
              (`skills/bonsai-garden-sign/bin/sign.mjs`). The sidecar finder is two names, written here once (parity-tested
              against sign.py). It does NOT re-seal: a pot seals at commit time (`keys.mjs seal-staged`), by the named
              kin's own key, never through a daemon holding the origin's signing salt.

Fail-safe: a provider that cannot run says so in `missing()`; a caller turns that into a STOP, never into an ok.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SIG_EXT = ".bonsaisig"
TWO_SIDECARS = "two-sidecars"
NO_SIDECAR = "no-sidecar"


def sidecar_candidates(path: Path) -> tuple[Path, Path]:
    """(old, hidden): `X.bonsaisig` and `.X.bonsaisig` beside `path` (one rule, with sign.py's and core's)."""
    return (path.parent / (path.name + SIG_EXT), path.parent / ("." + path.name + SIG_EXT))


def find_sidecar(path: Path) -> tuple[Path | None, str]:
    """(sidecar, "ok"), (None, "no-sidecar") or (None, "two-sidecars"): both names present is a refusal, not a preference."""
    have = [p for p in sidecar_candidates(path) if p.is_file()]
    if len(have) == 2:
        return None, TWO_SIDECARS
    return (have[0], "ok") if have else (None, NO_SIDECAR)


class PySigner:
    """The origin's signer: its own tools/sign.py."""

    kind = "sign.py"

    def __init__(self, sign_py: Path):
        self.sign_py = Path(sign_py)
        self._mod = None

    def missing(self) -> str | None:
        return None if self.sign_py.is_file() else f"{self.sign_py} is missing"

    def verify_argv(self, file: Path, *, pot: Path | None = None, pin: str | None = None) -> list:
        flags = ["--keys", "pot", "--pot", str(pot)] + (["--expect-fp", pin] if pin else []) if pot is not None else []
        return [sys.executable or "python3", str(self.sign_py), "verify-sidecar", str(file), *flags]

    def module(self):
        """sign.py, imported once (the origin's own garden only)."""
        if self._mod is None:
            spec = importlib.util.spec_from_file_location("bonsai_sign_srv", self.sign_py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            self._mod = mod
        return self._mod

    def find_sidecar(self, path: Path):
        return self.module().find_sidecar(path)

    def sign(self, file: Path, slug: str, role: str, cwd: Path) -> tuple[bool, str]:
        if not self.sign_py.exists():
            return False, "tools/sign.py not found"
        try:
            r = subprocess.run(
                [sys.executable, str(self.sign_py), "sign", str(file), "--as", slug, "--hkdf", "--role", role],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(cwd),
            )
        except subprocess.TimeoutExpired:
            return False, "sign.py timed out after 30s"
        except Exception as exc:
            return False, str(exc)
        if r.returncode == 0:
            return True, ""
        return False, r.stderr.strip() or r.stdout.strip() or "unknown error"


class CoreSigner:
    """A pot's signer: its wired core's Node verifier. Reads seals; makes none."""

    kind = "core"

    def __init__(self, core: Path | None, node: str | None = None):
        self.core = Path(core) if core else None
        self.node = (
            node
            or shutil.which("node")
            or ("/opt/homebrew/bin/node" if os.path.exists("/opt/homebrew/bin/node") else None)
        )
        self.sign_mjs = self.core / "skills" / "bonsai-garden-sign" / "bin" / "sign.mjs" if self.core else None

    def missing(self) -> str | None:
        if self.core is None:
            return "no core is wired to this pot (git config bonsai.core is unset)"
        if self.sign_mjs is None or not self.sign_mjs.is_file():
            return f"{self.sign_mjs} is missing"
        if not self.node:
            return "node is not on this host's PATH"
        return None

    def verify_argv(self, file: Path, *, pot: Path | None = None, pin: str | None = None) -> list:
        flags = ["--keys", "pot", "--pot", str(pot)] + (["--expect-fp", pin] if pin else []) if pot is not None else []
        return [str(self.node), str(self.sign_mjs), "verify-sidecar", "--file", str(file), *flags]

    def find_sidecar(self, path: Path):
        return find_sidecar(path)

    def sign(self, file: Path, slug: str, role: str, cwd: Path) -> tuple[bool, str]:
        return False, (
            "this server does not seal in a pot: a pot's seals are made at commit time by the named kin's own key "
            "(core: keys.mjs seal-staged), never by a daemon holding another garden's signing salt"
        )


# --- the exec gate, for a daemon that does not live beside the origin's sign.py ---------------------------------------------
# The daemon refuses to serve when a module of its own import closure no longer matches its seal. Beside the origin's sign.py
# that is sign.py's job (server.py, unchanged). A daemon hosted by RUNTIME has no sign.py, and answers one of two ways, by what
# it is:
#   an INSTALLED rt (RT-MANIFEST.json at its root): every module's sha256 is the manifest's; the rt is the one the wired core's
#       rt-minimum.json names, at or above its min-version; and the manifest is the one the served garden recorded at its install
#       (runtime.json: core's install-runtime writes it only after the published keys vouched for the manifest's owner seal),
#       or, for a garden with no install record that declares itself the origin in its own sealed pot.json (role origin),
#       the manifest's owner seal verifies through core's Node verifier with the served garden's own keys, by a key the
#       minimum names. Any other garden without a record is told to run install-runtime. A seal declaring the software
#       fallback is refused on both paths. Core pins no rt by hash (2026-10-18; the gardener: "we should never refer to a specific version of runtime as a
#       dependency me thinks"); this gate reads no rt-pin.json;
#   a runtime CHECKOUT (the origin's own daemon): every module's seal, through core's Node verifier with the served garden's
#       own keys (--keys garden, $BONSAI_GARDEN = the served garden), AND the checkout's sealed CODE-MANIFEST.json (below).
# Anything that cannot be checked is a failure: a gate that fails open when its tooling is missing is no gate.
#
# CODE RUNS ONLY FROM A SEALED MANIFEST (F-12, from 2026-10-21; the gardener's pick of 2026-10-20, "2a"). A seal's address
# is provenance, never a verdict (bonsai-garden-sign 1.1.0): a module passes its own seal wherever its bytes are. So a
# module's own seal cannot tell this gate which document belongs at which position, and another sealed module put in
# repo_routing.py's place would pass it. A map can: an installed rt's RT-MANIFEST.json (paths and bytes), and for a
# checkout CODE-MANIFEST.json at its root, generated from the tree by core's tools/code-manifest.mjs and sealed (paths
# and the seal ids of the documents that belong there). No manifest, or one whose seal does not hold: the gate refuses,
# and says so. Never a fall back to a position check.

SIGN_MARKER = Path("skills") / "bonsai-garden-sign" / "bin" / "sign.mjs"
RT_MINIMUM = Path("skills") / "bonsai-garden-keys" / "rt-minimum.json"
_VERSION = re.compile(r"^\d+\.\d+\.\d+(\.\d+)?(-[a-z0-9.-]+)?$")


def version_key(v: str) -> tuple:
    """MAJOR.MINOR.PATCH[.BUILD] as numbers, BUILD 0 when absent; a -LABEL never orders (core's rt-manifest.mjs, the one rule)."""
    if not isinstance(v, str) or not _VERSION.match(v):
        raise ValueError(f"{v!r} is not a version")
    n = [int(x) for x in v.split("-")[0].split(".")]
    return tuple(n + [0] * (4 - len(n)))


def _sidecar_verdict(node: str, core: Path, file: Path, garden: Path) -> tuple[bool, str, str, str]:
    """(ok, first line, signer, role) of core's verifier on `file`'s seal, with `garden`'s own keys."""
    try:
        r = subprocess.run(
            [node, str(core / SIGN_MARKER), "verify-sidecar", "--file", str(file), "--keys", "garden"],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, BONSAI_GARDEN=str(garden)),
        )
    except Exception as e:
        return False, f"cannot-verify: {type(e).__name__}: {e}", "", ""
    out = (r.stdout or "") + (r.stderr or "")
    first = (out.strip().splitlines() or [f"cannot-verify: the verifier gave no verdict (rc={r.returncode})"])[0]
    signer = re.search(r"^\s*signer:\s*(\S+)", out, re.M)
    role = re.search(r"^\s*sealed as:\s*(\w+):", out, re.M)
    return (
        r.returncode == 0 and first.startswith("ok"),
        first,
        signer.group(1) if signer else "",
        role.group(1) if role else "",
    )


def _installed_rt_gate(paths: list, server_root: Path, repo_root: Path, core: Path, node: str) -> list:
    """The INSTALLED rt half of core_gate (see above)."""
    manifest = server_root / "RT-MANIFEST.json"
    try:
        raw = manifest.read_bytes()
        doc = json.loads(raw)
        listed = {f["path"]: f["sha256"] for f in doc.get("files", [])}
        name, version = doc["name"], doc["version"]
        version_key(version)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [("<verifier>", f"rt's RT-MANIFEST.json unreadable ({type(e).__name__})")]
    try:
        minimum = json.loads((core / RT_MINIMUM).read_text("utf-8"))
        min_name, min_version, signers = minimum["name"], minimum["min-version"], list(minimum["signers"])
        below = version_key(version) < version_key(min_version)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [("<verifier>", f"the wired core's rt-minimum.json unreadable ({type(e).__name__}): no rt can be taken")]
    if name != min_name or below:
        return [
            (
                "<verifier>",
                f"this rt is {name} {version}; the wired core takes {min_name} {min_version} or newer "
                "(install-runtime --upgrade moves the pot to one)",
            )
        ]
    # R3 of the trust read of 2026-10-18: a manifest seal that declares the software fallback is no release, on either path and
    # whatever core's seal policy says (a lab preview sets it to warn)
    try:
        custody = json.loads((server_root / ".RT-MANIFEST.json.bonsaisig").read_text("utf-8")).get("custody")
    except (OSError, ValueError, AttributeError):
        custody = None
    if custody == "software-fallback":
        return [
            (
                "<verifier>",
                "RT-MANIFEST.json is sealed through the software fallback (custody: software-fallback): no release",
            )
        ]
    record = repo_root / "runtime.json"
    if record.is_file():
        try:
            want = json.loads(record.read_text("utf-8"))["manifest-sha256"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            return [("<verifier>", f"the served garden's runtime.json unreadable ({type(e).__name__})")]
        if hashlib.sha256(raw).hexdigest() != want:
            return [
                ("<verifier>", "RT-MANIFEST.json is not the one the served garden recorded at install (runtime.json)")
            ]
    else:
        # R4 of the trust read of 2026-10-18: a missing file never chooses the anchor. With no install record, only a garden that
        # declares itself the origin in its own declaration (pot.json, role origin), sealed by a key it holds, is served by
        # the manifest's seal; any other is told to run install-runtime.
        decl = repo_root / "pot.json"
        try:
            declared = json.loads(decl.read_text("utf-8")).get("role") == "origin"
        except (OSError, ValueError, AttributeError):
            declared = False
        if declared:
            declared = _sidecar_verdict(node, core, decl, repo_root)[0]
        if not declared:
            return [
                (
                    "<verifier>",
                    "the served garden has no install record (runtime.json) and does not declare itself the origin "
                    "(pot.json, role origin, sealed by a key it holds): run install-runtime",
                )
            ]
        ok, first, signer, role = _sidecar_verdict(node, core, manifest, repo_root)
        if not ok:
            return [("<verifier>", f"RT-MANIFEST.json's seal, with the served garden's keys: {first}")]
        if role != "owner" or signer not in signers:
            return [
                (
                    "<verifier>",
                    f"RT-MANIFEST.json is sealed {role or '?'} by {signer or '?'}; the wired core takes an owner seal by "
                    f"{', '.join(signers)}",
                )
            ]
    out = []
    for p in paths:
        rel = str(Path(p).resolve().relative_to(server_root.resolve()))
        want = listed.get(rel)
        if want is None:
            out.append((p, "not listed in RT-MANIFEST.json"))
        elif hashlib.sha256(Path(p).read_bytes()).hexdigest() != want:
            out.append((p, "hash-mismatch against RT-MANIFEST.json"))
    return out


CODE_MANIFEST = "CODE-MANIFEST.json"
_ID_LINE = re.compile(r"^\s*id:\s*(\S+)", re.M)


def seal_id(verifier_out: str) -> str | None:
    """The id a verdict's ok lines name (`  id:          <ULID>`), from either verifier's printed verdict."""
    m = _ID_LINE.search(verifier_out or "")
    return m.group(1) if m else None


def code_manifest_check(paths: list, server_root: Path, ids: dict, manifest_verdict) -> list:
    """[(path, reason)] for every module of `paths` whose position the checkout's sealed CODE-MANIFEST.json does not vouch
    for; one ("<verifier>", reason) when there is no such manifest, or its seal does not hold. `ids` maps each module whose
    own seal held to the id that seal carries (a module missing from `ids` failed its own seal and is reported there).
    `manifest_verdict(file) -> (ok, first line)` is the gate's own verifier on the manifest's seal."""
    man = Path(server_root) / CODE_MANIFEST
    if not man.is_file():
        return [
            (
                "<verifier>",
                f"no sealed manifest for this daemon's code: {CODE_MANIFEST} is not at {server_root}; code runs only from "
                "a sealed manifest (write it with core's tools/code-manifest.mjs, then seal it)",
            )
        ]
    ok, first = manifest_verdict(man)[:2]
    if not ok:
        return [("<verifier>", f"{CODE_MANIFEST}'s seal does not hold: {first}")]
    try:
        doc = json.loads(man.read_text("utf-8"))
        listed = {f["path"]: f["id"] for f in doc["files"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [("<verifier>", f"{CODE_MANIFEST} unreadable ({type(e).__name__})")]
    out = []
    root = Path(server_root).resolve()
    for p in paths:
        if p not in ids:
            continue
        try:
            rel = Path(p).resolve().relative_to(root).as_posix()
        except ValueError:
            out.append((p, f"outside {root}: {CODE_MANIFEST} places nothing there"))
            continue
        want = listed.get(rel)
        if want is None:
            out.append((p, f"not in {CODE_MANIFEST}: no sealed manifest places code at {rel}"))
        elif ids[p] != want:
            out.append((p, f"{CODE_MANIFEST} places {want} at {rel}; the module there is sealed as {ids[p]}"))
    return out


def sibling_core(server_root: Path) -> Path | None:
    """The one core beside this runtime that carries the Node verifier, by marker; none or more than one is None (never a guess)."""
    hits = (
        [d for d in server_root.parent.iterdir() if d != server_root and (d / SIGN_MARKER).is_file()]
        if server_root.parent.is_dir()
        else []
    )
    return hits[0] if len(hits) == 1 else None


def core_gate(paths: list, server_root: Path, repo_root: Path, core: Path | None, node: str | None = None) -> list:
    """[(path, reason)] for every module that does not verify; "<verifier>" when the gate itself cannot run."""
    core = Path(core) if core else sibling_core(server_root)
    if core is None:
        return [
            (
                "<verifier>",
                "no core to check the daemon's modules against (none wired, none beside this runtime by marker)",
            )
        ]
    node = (
        node or shutil.which("node") or ("/opt/homebrew/bin/node" if os.path.exists("/opt/homebrew/bin/node") else None)
    )
    if not node:
        return [("<verifier>", "node is not on this host's PATH")]
    if (server_root / "RT-MANIFEST.json").is_file():
        return _installed_rt_gate(paths, server_root, repo_root, core, node)
    out = []
    ids = {}
    env = dict(os.environ, BONSAI_GARDEN=str(repo_root))
    for p in paths:
        try:
            r = subprocess.run(
                [node, str(core / SIGN_MARKER), "verify-sidecar", "--file", str(p), "--keys", "garden"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        except Exception as e:
            out.append((p, f"cannot-verify: {type(e).__name__}: {e}"))
            continue
        first = (r.stdout or r.stderr or "").strip().splitlines()[:1]
        if r.returncode != 0 or not first or not first[0].startswith("ok"):
            out.append(
                (
                    p,
                    first[0].split()[0]
                    if first and first[0]
                    else f"cannot-verify: the verifier gave no verdict (rc={r.returncode})",
                )
            )
            continue
        ids[p] = seal_id(r.stdout)
    # its own seal is bytes, signer and id; WHICH document belongs at this position is the checkout's sealed map (2a)
    return out + code_manifest_check(paths, server_root, ids, lambda f: _sidecar_verdict(node, core, f, repo_root))
