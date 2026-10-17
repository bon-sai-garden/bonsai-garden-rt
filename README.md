---
id: 01M4KSMR3WVR8VAMTCCKZJZGZZ
title: "rt — the runtime a new garden's pot installs"
type: readme
tended-by: baisai
created: 2026-10-10
updated: 2026-10-17
---

# rt — the runtime a new garden's pot installs

rt is the part of a garden that sits in its pot's commit path and looks at the host. It is cut from a larger runtime by a generator, and every file in it is listed with its sha256 in `RT-MANIFEST.json`, together with the upstream commit it was cut from.

It holds three things:

| Path | What |
|---|---|
| `tools/hooks/garden/pre-commit` | the commit gate: ids and seals, checked through core |
| `tools/rt/doctor.mjs` | the doctor: is this host and this pot set up to commit, seal and verify on core and rt alone |
| `tools/bonsai-mcp/` | the garden MCP: the server a pot's sessions read and write the garden through, launched from the pot's own folder |

## Installing it

rt is not installed by hand. The garden's core installs it, verifies every byte against the pin core carries, and wires the pot to it:

```bash
node <core>/skills/bonsai-garden-keys/bin/keys.mjs install-runtime --pot <your pot> --from <this rt, a path or a URL>
```

## Checking it

```bash
node <rt>/tools/rt/doctor.mjs --pot <your pot>
```

Exit 0 when every check holds, 1 when one fails; each line says what it saw.

## How it is tested

rt ships no tests. Every shipped script is tested upstream, in the runtime it is cut from, and a cut is refused when the shipped text names a garden or carries a board ticket id:

| Shipped | Tested upstream by |
|---|---|
| `tools/bonsai-mcp/server.py` | `tools/bonsai-mcp/test.py`, `test_home_working_state.py`, `test_session_start_loop.py`, `test_write_routing.py`, `test_pot_hub_routing.py`, `test_server_port.py`, the repro gates |
| `tools/bonsai-mcp/signer.py` | `test_signer_gate.py`, `test_commit_broker.py` |
| `tools/bonsai-mcp/integrity_line.py` | `test_integrity_line.py`, `test_integrity_pot.py`, `test_integrity_pins.py`, `test_instance_free.py` |
| `tools/bonsai-mcp/repo_routing.py` | its own self-test (`python3 repo_routing.py`), `test_rt_scripts.py`, `test_write_routing.py`, `test_pot_hub_routing.py` |
| `tools/bonsai-mcp/deployment_context.py` | `test_rt_scripts.py` |
| `tools/bonsai-mcp/skill_install.py` | `test_rt_scripts.py` |
| `tools/repo_skip_dirs.py` | `test_rt_scripts.py` |
| `tools/rt/doctor.mjs` | `tools/rt/test-rt.mjs` |

## Installing the garden MCP

`install-runtime` writes the pot's `.mcp.json` (host-local, ignored by git): the server is run by `uv` from the installed rt with `--garden <your pot>`, its Python environment outside rt, so rt stays exactly its manifest. It serves that one garden. A garden whose content still lives in more than one repository names them in its own routing table (`rituals/repo/repo-split-registry.json`, with its `hub`), and the server reads each path where the table says, labelled with the repository's name.

## What it does not do

It captures no transcripts and holds no key. The server runs only when a session of the pot starts it, and reads no repository but the garden's own, those its routing table names, and the core the pot is wired to. Seals are made by core, with the key the garden's own salt derives; rt checks them at commit time and when a session starts.

## This release

0.8.3. Seed and rt are released together under one version.
