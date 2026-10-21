---
id: 01M5F6QNBR62XS9G79B1Q34B1V
title: "Licensing: code is Apache 2.0; the logo, every other graphic and every document are CC BY-SA 4.0"
type: reference
tended-by: baisai
created: 2026-10-21
updated: 2026-10-21
version: 1.0.0
status: active
tags: [licence, apache, cc-by-sa, attribution, publication-readiness]
---

# Licensing

The gardener's ruling, 2026-10-21: *"we then need to use Apache v2 and CC-BY SA 4.0 combination [...] The logo is the latter."* So the split is the garden's, here as everywhere: **code under the Apache License 2.0; the logo, and every other work of ours that is not code, under Creative Commons Attribution-ShareAlike 4.0 (CC BY-SA 4.0).** This page is the file map: which licence covers which file here, and why.

| | licence | file |
|---|---|---|
| **Our code** | Apache License 2.0 | [`LICENSE`](LICENSE) |
| **The logo, and every work of ours that is not code** | Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0) | [`LICENSE-CC-BY-SA-4.0`](LICENSE-CC-BY-SA-4.0) |
| **Third-party material** | its own licence | where it is pinned (below) |

## The map

### Apache License 2.0: our code, wherever it sits

- **Every program**: each script, module, hook, service and test, in whatever language, with the configuration, fixtures, styles and templates it reads. The commit gate under `tools/hooks/` is code.
- **Manifests and configuration**: `tools/bonsai-mcp/pyproject.toml`, `tools/bonsai-mcp/uv.lock`, every `.json` a program reads, and a release's manifest (`RT-MANIFEST.json`).
- **Seals and identity data**: every `.bonsaisig`, and any public key. They are published so that anyone can check a signature.

`tools/bonsai-mcp/pyproject.toml` declares `Apache-2.0`: the licence of the MCP server's code. The server runs in place and is never built into a distribution, so no package carries the icon beside it; the icon is mapped below, as a file of its own.

### CC BY-SA 4.0: the logo, and the rest that is not code

- **The logo**: `tools/bonsai-mcp/garden-icon.png`, the Bonsai Garden logo rendered as the MCP server's icon.
- **Every other graphic** of ours.
- **Every document**: `README.md`, this page, and every specification and skill document beside the code.

ShareAlike asks that an adaptation of these go out under the same licence, or one the licence names as compatible.

### Adapted from share-alike sources

No file here is adapted from a share-alike source. One that ever is keeps its source's terms (KanjiVG's stroke data, for one, is CC BY-SA 3.0 Unported), and says so in its own header.

### Third-party material

It keeps its own licence. The Python packages the MCP server runs on are pinned in `tools/bonsai-mcp/uv.lock` and fetched by `uv` when the server first starts; none is vendored here. This page relicenses none of it.

## Displaying our graphics

When you display a graphic of ours (the logo, an icon, an illustration), the attribution we ask for is **a direct link to the garden's home, placed directly underneath the graphic**, in this form:

> Bonsai Garden · [bon-sai.garden](https://bon-sai.garden)

Where the medium cannot carry a link (print, a frame of video), the address written out underneath serves. This is the manner of credit we request as the licensor, under the attribution clause of CC BY-SA 4.0 (section 3(a)(1)); it is written as that request, not as a term added to the licence.

## Where a file is mixed

**The file's own use decides.** A document that quotes code stays a document; a program that embeds a graphic is Apache 2.0 for the code, and the graphic keeps its own licence. If a file cannot be placed by this page, that is a bug in this page: say so, and the file gets a header that names its licence.

## What a licence does not give

- **A licence to a file is not a licence to its signature.** The kin's keys, seals and fingerprints say who wrote or vouched for a thing; a copy of a file does not carry its seal, and a changed copy is not sealed by its original's signer.
- **Names are credited, not granted.** Bonsai Garden and the kin's names are not licensed by either licence.

## Why

Code is meant to be taken, changed and built on with few strings: Apache 2.0 gives that, with its patent grant, and asks only for the notice. The logo and the documents are part of a garden's culture: they may be shared and adapted, with credit, on the same terms, so that what grows from them stays as open as they are.
