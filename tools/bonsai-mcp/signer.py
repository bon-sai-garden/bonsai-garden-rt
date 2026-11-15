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
              against sign.py). A pot is sealed ONE way: by the wired core's `keys.mjs seal-write`, which reads THE POT'S OWN
              garden's salt from the OS keystore (no prompt) and seals as the named KIN, never as the gardener, the root or
              the guardian, and never where the salt asks for a hand (1Password, the passphrase file). A daemon never holds the
              ORIGIN's signing salt, and this one holds none at all: the Node child reads the garden's item for one call.
              A pot's commit-time seals (`keys.mjs seal-staged`, the gardener's hand) are unchanged.
              ONE exception, for a core whose keys tool predates seal-write (keys before 0.11.0: the 6 Nov core plugin, 2026-11-11): see `CoreSigner._seal_staged`. It runs only on that tool's usage error, under seal-write's own
              refusals made here first, in a throwaway git index, and a seal whose signer or role is not what was asked is withdrawn.
              It goes when no core in use predates 0.11.0.

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
import tempfile
from pathlib import Path

SIG_EXT = ".bonsaisig"
KEYS_MJS = Path("skills") / "bonsai-garden-keys" / "bin" / "keys.mjs"
# Where a garden's salt rests when it is a named keychain file and not the login keychain (a gardener who keeps one for the
# garden; the lab rehearsal's scratch keychain). A PLACE, never the salt: custody is read by core, and its answer is checked
# against the registry's fingerprint, so a wrong place seals nothing.
CUSTODY_KEYCHAIN_ENV = "BONSAI_CUSTODY_KEYCHAIN"
TWO_SIDECARS = "two-sidecars"
NO_SIDECAR = "no-sidecar"

# keys.mjs answers a verb or flag it has never heard of with exit 64 and one of these first lines. The flags are judged BEFORE the
# verb, so a keys tool without seal-write says `unknown flag --role` (the first flag this module passes) and not `unknown verb`.
# Nothing else is "this core predates seal-write": a refusal (exit 2), a halt (3), or another usage error is the answer.
_PREDATES_SEAL_WRITE = re.compile(r'unknown verb "seal-write"|unknown flag --(?:role|created)\b')

# What seal-write refuses by name, mirrored here for the one case where it is not the verb that runs (keys 0.11.0's own wording,
# `cmdSealWrite`): a service seals only a KIN's key (a registry entry of type shin), never the root, the gardener or the origin's,
# and only a salt it can open without a person (the OS keystore). The older verb would open any custody and seal for the root.
SERVICE_SEALS = ("shin",)
UNATTENDED_TIER = "os-protected"
# The one custody the older verb may open for a service. It does not read the garden's declared tier to pick custody: it asks every
# backend the host offers (on macOS the keychain, then 1Password's `op`, then the passphrase file), so a service would reach 1Password
# (a prompt for the gardener's hand, or the salt read from an item the tier says is not there). seal-write reads the OS keystore only;
# the fallback pins the older verb to the same one with its `--backends`.
UNATTENDED_BACKEND = "macos-keychain" if sys.platform == "darwin" else "secret-service"


def service_refusal(pot: Path, slug: str) -> str | None:
    """None when the older verb may be run for `slug` in `pot`; else the sentence that refuses, in seal-write's own words.
    Judged from the pot's own garden.json and agents/registry.json, before any custody is opened and before anything is staged."""
    try:
        garden = json.loads((pot / "garden.json").read_text(encoding="utf-8"))
        registry = json.loads((pot / "agents" / "registry.json").read_text(encoding="utf-8"))
        agents = registry.get("agents") or []
    except Exception as exc:
        return (
            f"{pot} has no readable garden.json and agents/registry.json ({type(exc).__name__}): the custody cannot be judged, "
            "so nothing is sealed; nothing written"
        )
    entry = next((a for a in agents if isinstance(a, dict) and a.get("slug") == slug), None)
    if entry is None:
        return f"{slug} is not in this pot's registry; only a key the pot lists can seal here; nothing written"
    if entry.get("origin"):
        return f"{slug}'s key is the origin's, and no one in this garden holds that key; nothing written"
    root, gardener = garden.get("name"), garden.get("gardener")
    if slug == root or slug == gardener or entry.get("type") not in SERVICE_SEALS:
        what = "the garden's root" if slug == root else "the gardener" if slug == gardener else f"a {entry.get('type')}"
        return (
            f"{slug} is {what}, not a kin: the service seals only a kin's key. The root's and the gardener's seals are made by "
            "his own hand, at his terminal; nothing written"
        )
    tier = garden.get("custody-tier")
    if tier != UNATTENDED_TIER:
        if tier == "vault-cli":
            return (
                "this garden's salt rests in 1Password (vault-cli), which asks for the gardener's hand: a service cannot open that custody, "
                "and this tool does not try. Seal at his terminal (seal-staged); nothing written"
            )
        if tier == "software-fallback":
            return (
                "this garden's salt rests in a passphrase-wrapped file (software-fallback), which asks for the gardener's "
                "passphrase: a service cannot open that custody, and this tool does not try. Seal at his terminal (seal-staged); nothing written"
            )
        return f"this garden's custody tier is {json.dumps(tier)}, not one a service opens without a person; nothing written"
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return f"this host ({sys.platform}) has no OS keystore this tool reads without a prompt; nothing written"
    return None


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

    def sign(self, file: Path, slug: str, role: str, cwd: Path, created: bool = False) -> tuple[bool, str]:
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

    def sign(self, file: Path, slug: str, role: str, cwd: Path, created: bool = False) -> tuple[bool, str]:
        """Seal `file` in the pot `cwd` as `slug`, with the key the pot's own salt derives for that kin: the wired core's
        `keys.mjs seal-write`. The tool's refusals (not a kin, a salt a service cannot open, a key that is not the registry's, a
        role above the credit's) and halts come back as (False, the tool's first line); none becomes an ok.
        `created`: this write made the file (core's role rule: a new file crediting no one belongs to the writer, as owner)."""
        gone = self.missing()
        if gone:
            return False, gone
        keys = self.core / KEYS_MJS
        if not keys.is_file():
            return False, f"{keys} is missing: this core has no keys tool, so a pot's write cannot be sealed here"
        argv = [str(self.node), str(keys), "seal-write", "--pot", str(cwd), "--as", slug, "--role", role]
        if created:
            argv.append("--created")
        keychain = os.environ.get(CUSTODY_KEYCHAIN_ENV, "").strip()
        if keychain:  # the named keychain file that holds THIS garden's salt, when the login keychain does not: a place, never a secret
            argv += ["--keychain", keychain]
        argv += ["--", str(file)]
        try:
            r = subprocess.run(
                argv, capture_output=True, text=True, timeout=30, cwd=str(cwd), stdin=subprocess.DEVNULL
            )
        except subprocess.TimeoutExpired:
            return False, "keys.mjs seal-write timed out after 30s"
        except Exception as exc:
            return False, str(exc)
        if r.returncode == 0:
            return True, ""
        lines = (r.stderr or r.stdout or "").strip().splitlines()
        first = lines[0] if lines else f"keys.mjs seal-write exited {r.returncode} and said nothing"
        if r.returncode == 64 and _PREDATES_SEAL_WRITE.search(first):
            return self._seal_staged(keys, file, slug, role, cwd, created, first)
        if r.returncode == 64 and ("unknown verb" in first or "unknown flag" in first):
            first += " (this core predates seal-write)"
        return False, first

    def _seal_staged(
        self, keys: Path, file: Path, slug: str, role: str, cwd: Path, created: bool, why: str
    ) -> tuple[bool, str]:
        """The wired core's keys tool has no seal-write (keys before 0.11.0). Seal with its `seal-staged`, the verb it does have,
        and keep every promise seal-write makes:
          * seal-write's refusals are made FIRST (`service_refusal`): the older verb opens any custody and seals for the root;
          * `seal-staged` needs its file staged and reads "created" off git (a file Added against HEAD). So it runs against a
            THROWAWAY git view (`_throwaway_git`): the pot's own index, refs and objects are never written, and the view says
            what the runtime CLAIMS: created, the file is not in HEAD; an edit, it is, with other bytes. That is seal-write's
            `--created` and its refusal of one for a file already in HEAD, and core's one role rule then reads the same facts;
          * the older verb opens whatever custody the host offers, so it is pinned to the OS keystore alone (`--backends`): a
            service never reaches 1Password, whatever the pot's garden.json declares;
          * the older verb has no --role. The answer is READ BACK, and a seal whose signer is not `slug`, whose role is not the
            one asked (the write is the shared shintai's: custodian, always), or whose file-hash is not the bytes the view was
            staged with (seal-write's own guard against a file changed under the seal) is withdrawn, an earlier seal put back
            byte for byte, and the call fails: a seal never carries a role the caller did not ask.
        Any halt of the older verb withdraws the same way (it leaves a seal that does not verify in place). The throwaway view
        lives in a temp dir that is removed on every way out of this call; a daemon killed with SIGKILL in the middle of it cannot
        remove it, and it holds the pot file as a git object (not the pot's history)."""
        predates = f"{why} (this core predates seal-write)"
        refusal = service_refusal(cwd, slug)
        if refusal:
            return False, refusal
        try:
            real_pot, real_file = Path(os.path.realpath(cwd)), Path(os.path.realpath(file))
            rel = real_file.relative_to(real_pot).as_posix()
        except (ValueError, OSError):
            return False, f"{file}: outside this pot ({cwd}) or not found; nothing written"
        if rel == ".git" or rel.startswith(".git/") or rel.endswith(SIG_EXT) or not real_file.is_file():
            return False, f"{rel}: not a regular file of the pot's, or a seal itself; nothing written"
        head = subprocess.run(
            ["git", "-C", str(real_pot), "rev-parse", "--verify", "-q", "HEAD^{commit}"], capture_output=True, text=True
        ).stdout.strip()
        if created and head:
            in_head = subprocess.run(["git", "-C", str(real_pot), "cat-file", "-e", f"{head}:{rel}"], capture_output=True)
            if in_head.returncode == 0:
                return False, (
                    f"{rel}: --created is the caller's word, and this file is already in HEAD, so this write did not create it "
                    "(an edit is not authorship; a file credited to no one is held custodian); nothing written"
                )
        try:  # before the temp dir exists: an unreadable earlier seal must leave nothing behind
            prior = {p: (p.read_bytes() if p.is_file() else None) for p in sidecar_candidates(real_file)}
        except OSError as exc:
            return False, f"{rel}: an earlier seal cannot be read ({type(exc).__name__}: {exc}); nothing written"
        tmp = Path(tempfile.mkdtemp(prefix="bonsai-seal-index-"))

        def withdraw() -> None:
            for p, data in prior.items():
                if data is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(data)

        try:
            env, bad = _throwaway_git(real_pot, rel, created, head, tmp)
            if bad:
                return False, f"{bad}; {predates}"
            try:  # the bytes the view was staged with; the seal's file-hash must cover exactly these
                staged_hash = hashlib.blake2b(real_file.read_bytes()).hexdigest()
            except OSError as exc:
                withdraw()
                return False, f"{rel}: cannot be read ({type(exc).__name__}: {exc}); nothing written"
            argv = [str(self.node), str(keys), "seal-staged", "--pot", str(real_pot), "--as", slug]
            argv += ["--backends", UNATTENDED_BACKEND]
            keychain = os.environ.get(CUSTODY_KEYCHAIN_ENV, "").strip()
            if keychain:
                argv += ["--keychain", keychain]
            argv += ["--", str(real_file)]
            try:
                r = subprocess.run(
                    argv, capture_output=True, text=True, timeout=30, cwd=str(real_pot), env=env, stdin=subprocess.DEVNULL
                )
            except subprocess.TimeoutExpired:
                withdraw()
                return False, "keys.mjs seal-staged timed out after 30s"
            if r.returncode != 0:
                withdraw()
                out = (r.stdout or "").splitlines()
                said = next((ln.strip() for ln in out if "REFUSED" in ln), None) or next(
                    iter((r.stderr or r.stdout or "").strip().splitlines()), f"keys.mjs seal-staged exited {r.returncode}"
                )
                if r.returncode == 64 and ("unknown verb" in said or "unknown flag" in said):
                    return False, (
                        f"{why} (this core predates seal-write and has no seal-staged this fallback can use either: {said})"
                    )
                return False, f"seal-staged: {said}"
            side, how = find_sidecar(real_file)
            try:
                got = json.loads(side.read_text(encoding="utf-8")) if side else {}
            except Exception:
                got = {}
            if got.get("signer") != slug:
                withdraw()
                return False, (
                    f"seal-staged left {rel} sealed by {got.get('signer')!r}, not by {slug} (a seal that already held was kept, "
                    "not renewed); nothing sealed"
                )
            if got.get("role") != role:
                withdraw()
                return False, (
                    f"seal-staged sealed {rel} as {got.get('role')} by core's rule; this write asked for {role}, and a seal never "
                    "carries a role the caller did not ask; nothing sealed"
                )
            if got.get("file-hash") != staged_hash:
                withdraw()
                return False, (
                    f"seal-staged sealed {rel} over bytes other than the ones this write staged (the file changed under the seal, "
                    "or the seal carries no file-hash); nothing sealed"
                )
            return True, ""
        except Exception as exc:
            withdraw()
            return False, f"seal-staged: {type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _throwaway_git(pot: Path, rel: str, created: bool, head: str, tmp: Path) -> tuple[dict, str]:
    """(env, "") or ({}, why): a git view of `pot` that is not the pot's, for the older `seal-staged` to read and `git add` into.
    Its own GIT_DIR under `tmp` (HEAD is a synthetic commit; the pot's objects are read through GIT_ALTERNATE_OBJECT_DIRECTORIES
    and nothing is written to them), its own index, the pot's tree as the work tree. The synthetic HEAD is the pot's HEAD with
    `rel` removed when this write CREATED it (the verb then sees it Added, which is "created"), or with `rel` holding other bytes
    when it did not (the verb sees it Modified: an edit). The index then holds the file as it is on disk: staged, whole."""
    common = subprocess.run(
        ["git", "-C", str(pot), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True
    ).stdout.strip()
    if not common:
        return {}, f"{pot} is not a git repository"
    gitdir = tmp / "gitdir"
    (gitdir / "objects").mkdir(parents=True)
    (gitdir / "refs").mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    env = dict(
        os.environ,
        GIT_DIR=str(gitdir),
        GIT_WORK_TREE=str(pot),
        GIT_INDEX_FILE=str(tmp / "index"),
        GIT_ALTERNATE_OBJECT_DIRECTORIES=str(Path(common) / "objects"),
        GIT_AUTHOR_NAME="bonsai-seal",
        GIT_AUTHOR_EMAIL="seal@invalid",
        GIT_COMMITTER_NAME="bonsai-seal",
        GIT_COMMITTER_EMAIL="seal@invalid",
    )
    for k in ("GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE"):
        env.pop(k, None)

    def git(*a, data=None):
        return subprocess.run(
            ["git", *a], capture_output=True, text=True, timeout=30, env=env, cwd=str(pot), input=data
        )

    def fail(what, r):
        line = (r.stderr or r.stdout).strip().splitlines()
        return {}, f"{rel}: git {what} failed for the older verb's view ({line[0] if line else r.returncode})"

    if head:
        r = git("read-tree", head)
        if r.returncode != 0:
            return fail("read-tree", r)
    if created:
        r = git("update-index", "--force-remove", "--", rel)
    else:
        blob = git("hash-object", "-w", "--stdin", data="the bytes this write replaced\n")
        if blob.returncode != 0:
            return fail("hash-object", blob)
        r = git("update-index", "--add", "--cacheinfo", f"100644,{blob.stdout.strip()},{rel}")
    if r.returncode != 0:
        return fail("update-index", r)
    tree = git("write-tree")
    if tree.returncode != 0:
        return fail("write-tree", tree)
    commit = git("commit-tree", tree.stdout.strip(), "-m", "the view the older seal-staged reads", *(["-p", head] if head else []))
    if commit.returncode != 0:
        return fail("commit-tree", commit)
    (gitdir / "HEAD").write_text(commit.stdout.strip() + "\n")
    r = git("add", "--", rel)  # the index takes the file as it is on disk: whole, staged
    if r.returncode != 0:
        return fail("add", r)
    return env, ""


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

# THE MODE (ruled 2026-10-21; from rt 0.9.2, the format string unchanged): RT-MANIFEST.json's entries state the
# mode git holds, "100644" or "100755", on every entry or on none (none: every rt before 0.9.2); some, or another value, is
# malformed and refuses. Where it is stated, each module's owner execute bit must be what its entry says, and a miss refuses
# that module, with the fix in plain words. LAST_RT_MODE says what the last installed-rt gate did of the modes: checked,
# not-stated, or not-checkable (no execute bit on this platform), and never checked when it was not.
RT_MODES = ("100644", "100755")
EXEC_READABLE = os.name != "nt"
LAST_RT_MODE: str | None = None

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
    global LAST_RT_MODE
    LAST_RT_MODE = None  # no manifest read yet: no word on modes
    manifest = server_root / "RT-MANIFEST.json"
    try:
        raw = manifest.read_bytes()
        doc = json.loads(raw)
        entries = doc.get("files", [])
        listed = {f["path"]: f["sha256"] for f in entries}
        name, version = doc["name"], doc["version"]
        version_key(version)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [("<verifier>", f"rt's RT-MANIFEST.json unreadable ({type(e).__name__})")]
    stating = [f for f in entries if "mode" in f]
    if stating and len(stating) != len(entries):
        return [
            (
                "<verifier>",
                f"rt's RT-MANIFEST.json is malformed: mode on some entries ({len(stating)} of {len(entries)}); "
                "a manifest states every entry's mode or none",
            )
        ]
    odd = next((f for f in stating if not (isinstance(f["mode"], str) and f["mode"] in RT_MODES)), None)
    if odd:
        return [
            (
                "<verifier>",
                f"rt's RT-MANIFEST.json is malformed: {odd['path']} has mode {json.dumps(odd['mode'])}; "
                'a mode is "100644" or "100755"',
            )
        ]
    modes = {f["path"]: f["mode"] for f in stating}
    mode_said = "not-stated" if not modes else "checked" if EXEC_READABLE else "not-checkable"
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
        elif mode_said == "checked" and bool(os.stat(p).st_mode & 0o100) != (modes.get(rel) == "100755"):
            # the owner's execute bit, and only it (a umask owns the rest); a miss refuses, never a warning
            out.append(
                (
                    p,
                    f"mode: RT-MANIFEST.json says executable (100755) and it is not; run chmod +x {rel}"
                    if modes.get(rel) == "100755"
                    else f"mode: RT-MANIFEST.json says not executable (100644) and it is; run chmod -x {rel}",
                )
            )
    LAST_RT_MODE = mode_said
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
