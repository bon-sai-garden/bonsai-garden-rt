---
id: 01M4KSMR3WVR8VAMTCCKZJZGZZ
title: "rt — the runtime a new garden's pot installs"
type: readme
tended-by: baisai
created: 2026-10-10
updated: 2026-10-21
---

# rt — the runtime a new garden's pot installs

rt is the runtime of a Bonsai Garden. It holds the gate every commit in a garden's pot passes, a doctor that says whether a host and a pot are ready, and the garden's MCP: the server a garden's sessions read and write the garden through. It is not installed by hand: the garden's core (the seed, `bonsai-garden-seed`) installs it, and only when every byte checks out.

> [!note] Release candidate, 0.9.1-rc
> The seed and rt are released together under one version, and this is it: 0.9.1-rc. A label never orders a version: to every check, 0.9.1-rc is 0.9.1. The garden's MCP is locked past three critical advisories (anyio, pyjwt, certifi), so the seed takes rt 0.9.0 or newer; a pot on an older rt moves with `install-runtime --upgrade`. What comes before 1.0 is in the seed's README.

## What is in it

| Path | What |
|---|---|
| `tools/hooks/garden/pre-commit` | the commit gate: ids and seals, checked with the seed's verifier |
| `tools/rt/doctor.mjs` | the doctor: can this host and this pot commit, seal and verify with the seed and rt alone? |
| `tools/rt/host-service.mjs` | the garden's MCP as a service on this host: installed once, registered once, probed, and taken back |
| `tools/bonsai-mcp/` | the garden's MCP itself |

Every file is listed with its sha256 in `RT-MANIFEST.json`. The manifest is sealed by its owner (`.RT-MANIFEST.json.bonsaisig`), the one file it does not list.

## Start here

In the commands, `<seed>` is your copy of the seed, and `<rt>` is the rt the seed installed for your pot.

1. **Install it into your pot.** The seed's keys tool does it, and wires the pot to it:

   ```bash
   node <seed>/skills/bonsai-garden-keys/bin/keys.mjs install-runtime --pot <your pot> --from <this rt: a path or a URL>
   ```

   It takes rt only when three things hold: every byte matches the manifest, the version is at least the oldest rt the seed takes (its `rt-minimum.json`), and the published keys vouch for the owner's seal on the manifest. No rt is named by hash: any rt that meets the three is taken.

2. **Check the pot.**

   ```bash
   node <rt>/tools/rt/doctor.mjs --pot <your pot>
   ```

   Each line says what it saw. Exit 0 when every check holds, 1 when one fails.

3. **Run the garden's MCP as a service**, once the garden has its keys (`garden.json`) and its registry. Ask first what it will do:

   ```bash
   node <rt>/tools/rt/host-service.mjs install --pot <your pot> --explain   # what it will write and download
   node <rt>/tools/rt/host-service.mjs install --pot <your pot>
   node <rt>/tools/rt/host-service.mjs probe   --pot <your pot>
   ```

## The garden's MCP as a service

`install --explain` (or `--dry-run`) changes nothing on the host. It names every file `install` would write, and where: the LaunchAgent, or PM2's definition and saved list; the service's record and logs; the user-level registration in Claude's configuration; the pot's `.mcp.json`, when it retires an entry there; each skill link; the pot's `.git/info/exclude`. It says what it would run, and what `uv` downloads when the service first starts: the Python packages pinned in the MCP's `uv.lock`, from its index (or uv's cache), into `~/.bonsai-garden/rt-env/<rt>`, and a Python only if uv finds none on the host. It ends with the way back, `uninstall`. `install` says the same download line before it starts the service.

`install` runs the server on `127.0.0.1`, one port and one name per garden (`<garden name>-garden`), so two gardens can share a host. PM2 runs it where PM2 is present, else a LaunchAgent on macOS; on Linux without PM2 it refuses and says the launcher is still owed. `uv` runs the server from the installed rt with `--garden <your pot>`, its Python environment and bytecode kept outside rt, so rt stays exactly what its manifest says. The service is registered once, with `claude mcp add --scope user --transport http`. A per-session garden entry in the pot's `.mcp.json` is retired (the desktop app drops such a server after one turn), and each kin's skill is linked into the pot (`.claude/skills/<slug>`, local only). A second run changes nothing.

`probe` says *connected* or *not connected*, and which: not installed, not running, not registered, turned off for this pot, or shadowed. Then it asks each kin's integrity line through the service and shows the answer: `ok`, or a STOP.

`uninstall` is the way back.

The service serves one garden. A garden kept in more than one repository names them in its own routing table (`rituals/repo/repo-split-registry.json`, with its `hub`), and the server reads each path where the table says, labelled with the repository's name.

## What it does not do

It captures no transcripts and holds no key. The server answers only on `127.0.0.1`, and reads no repository but the garden's own, those its routing table names, and the seed the pot is wired to. Seals are made by the seed's sign skill, with the key the garden's own salt derives; rt checks them at commit time and when a session starts.

## How it is tested

rt ships without its tests. Every script in it is tested before a release is made, and a release is refused when its text names a garden or carries a ticket id, or when a version its lock pins has a published advisory.

## License

Code is licensed under the [Apache License 2.0](LICENSE). The logo, `tools/bonsai-mcp/garden-icon.png`, and everything else of ours that is not code are licensed under [Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)](LICENSE-CC-BY-SA-4.0). When you display one of our graphics, credit it with a direct link underneath, in the form [LICENSING.md](LICENSING.md) gives; it also says which licence covers which file.
