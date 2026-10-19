#!/usr/bin/env node
// ---
// id: 01M4KSMR0BZQSYTX9HKY671M6M
// title: rt's doctor — is this host and this pot set up to commit, seal and verify on core and rt alone?
// type: script
// tended-by: baisai
// created: 2026-10-10
// updated: 2026-10-18
// ---
//
//   node <rt>/tools/rt/doctor.mjs --pot <dir> [--attribution <record>]
//
// A generated output is unsealed by design and held to its derivation (the trust ruling of 2026-10-17). The doctor
// reads the pot's attribution record by the commit gate's rule (runtime tools/runtime/seal-staged.py: --attribution, else
// the newest *-attribution.json in identity/ownership/, the `generated` rows whose `repo` is this checkout's name), and
// exempts an unsealed file only when its row names a gate, only from a record whose own seal verified in this run, and
// it says what it exempted and that it did not run the gate. A pot with no record exempts nothing.
//
// The pot's doctor, and only it. Each
// check prints one line, `ok`, `FAIL` or `note`, with what it saw; the exit is 1 when any check
// fails, else 0. It reads and hashes; it writes nothing, seals nothing, asks no secret and calls no
// network but this host's own loopback, to ask the garden's MCP whether it answers (its host service, 2026-10-19). What it
// needs from core (the manifest reader, the verifier) it imports from the core the
// pot names in its local git config (bonsai.core), a down edge; it never reaches for a garden by a
// directory name or an environment variable, and it fails when $BONSAI_GARDEN points anywhere but
// the pot.
//
// The garden MCP (host-service.mjs beside this file, one reading shared with its probe): before the service is installed
// (planting §8, after the keys) the doctor notes it and does not fail; once this host holds the garden's service record,
// every part of "connected" is a check: the launcher runs it, it answers on its port, it is registered at user level, it
// is not turned off for this pot, and no per-session entry in the pot shadows it. A kin's integrity is the probe's to ask.

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const args = process.argv.slice(2);
const at = args.indexOf('--pot');
const atAttr = args.indexOf('--attribution');
if (at < 0 || !args[at + 1] || (atAttr >= 0 && !args[atAttr + 1]) || args.includes('--help')) {
  console.log('usage: node <rt>/tools/rt/doctor.mjs --pot <the pot> [--attribution <record>]');
  process.exit(args.includes('--help') ? 0 : 64);
}
const pot = path.resolve(args[at + 1]);
const results = [];
const ok = (what, detail) => results.push(['ok', what, detail]);
const fail = (what, detail) => results.push(['FAIL', what, detail]);
const note = (what, detail) => results.push(['note', what, detail]);
const real = (p) => {
  try {
    return fs.realpathSync(p);
  } catch {
    return null;
  }
};
const run = (cmd, a, cwd = pot) => {
  const r = spawnSync(cmd, a, { cwd: fs.existsSync(cwd) ? cwd : undefined, encoding: 'utf8' });
  return { code: r.error ? null : r.status, out: `${r.stdout || ''}${r.stderr || ''}`.trim(), error: r.error };
};

// rt itself: the directory above this file that holds RT-MANIFEST.json, found by marker
let rt = path.dirname(fileURLToPath(import.meta.url));
while (!fs.existsSync(path.join(rt, 'RT-MANIFEST.json'))) {
  const up = path.dirname(rt);
  if (up === rt) {
    rt = null;
    break;
  }
  rt = up;
}

function report() {
  for (const [s, w, d] of results) console.log(`${s.padEnd(4)}  ${w.padEnd(14)} ${d}`);
  const n = results.filter((r) => r[0] === 'FAIL').length;
  console.log(
    n
      ? `\nrt doctor: ${n} check(s) failed.`
      : '\nrt doctor: this pot commits, seals and verifies on core and rt alone.',
  );
  process.exit(n ? 1 : 0);
}

// the attribution record's `generated` rows for this checkout, read by the commit gate's rule, and trusted only when the
// record's own seal verified in this run (`verdict` is the run's one verifier, so the record and every file are read alike)
function generatedList(verdict) {
  let file = atAttr >= 0 ? path.resolve(args[atAttr + 1]) : null;
  if (!file) {
    const dir = path.join(pot, 'identity', 'ownership');
    const names = fs.existsSync(dir)
      ? fs
          .readdirSync(dir)
          .filter((n) => n.endsWith('-attribution.json') && !n.startsWith('.'))
          .sort()
      : [];
    if (!names.length) return { rows: new Map(), trusted: false, file: null, why: null }; // no record: nothing is exempt
    file = path.join(dir, names[names.length - 1]);
  }
  const shown = path.relative(pot, file).startsWith('..') ? file : path.relative(pot, file);
  const none = (why) => ({
    rows: new Map(),
    trusted: false,
    file: shown,
    why: `${why}: no file is exempt as generated`,
  });
  let doc;
  try {
    doc = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return none(`the attribution record ${shown} could not be read (${e.message})`);
  }
  const rel = path.relative(pot, file);
  const v = rel.startsWith('..') || path.isAbsolute(rel) ? null : verdict(rel);
  if (!v?.ok)
    return none(`the attribution record ${shown} ${v ? `does not verify (${v.v.status})` : 'is outside this pot'}`);
  const name = path.basename(real(pot) || pot);
  const rows = new Map();
  for (const r of Array.isArray(doc?.generated) ? doc.generated : [])
    if (r && r.repo === name && typeof r.path === 'string') rows.set(r.path, r);
  return { rows, trusted: true, file: shown, why: rows.size ? null : `${shown} lists nothing generated for ${name}` };
}
const namesGate = (row) => typeof row.gate === 'string' && row.gate.trim() !== '' && row.gate.trim() !== 'none';

// 1. the tools on this host
const major = Number(process.versions.node.split('.')[0]);
(major >= 18 ? ok : fail)(
  'node',
  `${process.versions.node}${major >= 18 ? '' : ' (18 or newer is needed: Ed25519 and scrypt in node:crypto)'}`,
);
const g = run('git', ['--version']);
(g.code === 0 ? ok : fail)('git', g.code === 0 ? g.out : 'git does not run');
const py = run('python3', [
  '-c',
  'import sys; print("%d.%d.%d" % sys.version_info[:3]); sys.exit(0 if sys.version_info >= (3, 9) else 1)',
]);
(py.code === 0 ? ok : fail)(
  'python3',
  py.code === 0
    ? `${py.out} (core's ulid.py, the commit gate's id check)`
    : `python3 3.9 or newer does not run (${py.out || py.error?.code || 'missing'}); the gate's id check needs it`,
);

// 2. the pot
if (!fs.existsSync(path.join(pot, '.git'))) {
  fail('pot', `${pot} is not a git repository`);
  report();
}
ok('pot', pot);
const bg = process.env.BONSAI_GARDEN;
if (bg === undefined || bg === '') ok('BONSAI_GARDEN', 'unset');
else if (real(bg) && real(bg) === real(pot)) ok('BONSAI_GARDEN', 'points at this pot');
else
  fail(
    'BONSAI_GARDEN',
    `points at ${bg}, not this pot: a tool that reads it would act on another garden; unset it, or point it here`,
  );

// 3. core, by the pot's local config and by marker
const coreCfg = run('git', ['config', '--get', 'bonsai.core']);
const core = coreCfg.code === 0 ? coreCfg.out : null;
const MARKERS = [
  'skills/bonsai-garden-keys/bin/keys.mjs',
  'skills/bonsai-garden-keys/lib/rt-manifest.mjs',
  'skills/bonsai-garden-sign/lib/verify.mjs',
  'skills/bonsai-garden-keys/lib/pot-verdict.mjs',
  'tools/ulid.py',
];
const coreMissing = core ? MARKERS.filter((m) => !fs.existsSync(path.join(core, m))) : MARKERS;
if (!core) {
  fail('core', 'git config bonsai.core is unset in this pot; run install-runtime from your core');
  report();
}
if (coreMissing.length) {
  fail('core', `bonsai.core is ${core}, and ${coreMissing.join(', ')} is not there`);
  report();
}
ok('core', `${core} (found by marker)`);
const M = await import(pathToFileURL(path.join(core, 'skills', 'bonsai-garden-keys', 'lib', 'rt-manifest.mjs')).href);
const PV = await import(pathToFileURL(path.join(core, 'skills', 'bonsai-garden-keys', 'lib', 'pot-verdict.mjs')).href); // the one reader of a pot's own lab-preview switch

// 4. rt: its files against the pot's install record (runtime.json), and core's minimum
if (!rt) {
  fail('rt', 'this doctor is not inside an rt checkout (no RT-MANIFEST.json above it)');
  report();
}
const potRecordFile = path.join(pot, 'runtime.json');
let potRecord = null;
try {
  potRecord = M.readRecord(potRecordFile);
} catch (e) {
  fail('record', `the pot's runtime.json: ${e.message}`);
}
if (potRecord) {
  const v = M.verifyTree(rt, potRecord);
  if (v.ok)
    ok(
      'rt',
      `${rt}: ${v.manifest.name} ${v.manifest.version}, cut from ${v.manifest.upstream.repo} ${v.manifest.upstream.commit.slice(0, 12)}; every file matches what the pot recorded`,
    );
  else fail('rt', `${rt} is not the rt this pot recorded: ${v.problems.map((p) => `${p.kind}: ${p.what}`).join('; ')}`);
  if (potRecord['rt-commit'] && fs.existsSync(path.join(rt, '.git'))) {
    const head = run('git', ['-C', rt, 'rev-parse', 'HEAD']).out;
    (head === potRecord['rt-commit'] ? ok : fail)(
      'rt commit',
      head === potRecord['rt-commit']
        ? head.slice(0, 12)
        : `rt is at ${head}, the pot recorded ${potRecord['rt-commit']}`,
    );
  }
  // core takes an rt by a minimum version, never by a hash it pins (2026-10-18): this pot's rt must be at or above it,
  // or the garden MCP's exec gate (signer.py) refuses to serve from it
  try {
    const min = M.readMinimum(path.join(core, 'skills', 'bonsai-garden-keys', 'rt-minimum.json'));
    if (potRecord.name === min.name && M.atLeast(potRecord.version, min['min-version']))
      ok(
        'core minimum',
        `core takes ${min.name} ${min['min-version']} or newer; this pot's rt is ${potRecord.version}`,
      );
    else
      fail(
        'core minimum',
        `core takes ${min.name} ${min['min-version']} or newer; this pot's rt is ${potRecord.name} ${potRecord.version}: install-runtime --upgrade --from <a newer rt>`,
      );
  } catch (e) {
    fail('core minimum', `core carries no readable rt minimum (${e.message}); no rt can be taken`);
  }
}

// 5. the commit gate is wired to this rt
const hp = run('git', ['config', '--get', 'core.hooksPath']);
const want = path.join(rt, ...M.HOOKS_DIR.split('/'));
if (hp.code !== 0) fail('hooksPath', `core.hooksPath is unset; the commit gate does not run (want ${want})`);
else if (real(path.resolve(pot, hp.out)) !== real(want))
  fail('hooksPath', `core.hooksPath is ${hp.out}, not this rt's ${want}`);
else ok('hooksPath', want);

// 6. the pot's seals: core's policy as it stands, and the pot's own declared lab-preview (its garden.json), read by core's lib
const reg = path.join(pot, 'agents', 'registry.json');
if (!fs.existsSync(reg)) {
  note(
    'seals',
    'no agents/registry.json yet: no seal in this repository can be checked until its signing keys are set up, and the commit check refuses commits until then',
  );
} else {
  const tracked = run('git', ['ls-files', '-z'])
    .out.split('\0')
    .filter((x) => x && !x.endsWith('.bonsaisig'));
  const verdicts = new Map();
  const verdict = (rel) => {
    if (!verdicts.has(rel)) verdicts.set(rel, PV.potVerdict(pot, path.join(pot, rel)));
    return verdicts.get(rel);
  };
  const gen = generatedList(verdict);
  const bad = [];
  const exempt = [];
  let custody = 0,
    softened = 0;
  for (const rel of tracked) {
    const { ok: good, v: vv, softened: sf } = verdict(rel);
    if (sf) softened++;
    if (good) continue;
    const row = gen.rows.get(rel);
    if (row && gen.trusted && namesGate(row)) {
      exempt.push(`${rel} (${vv.status}; gate ${row.gate.trim()})`);
      continue;
    }
    if (vv.status === 'custody-refused') custody++;
    bad.push(
      `${rel} ${vv.status}${row && !namesGate(row) ? ' (listed as generated, but its row names no gate: it must carry a seal)' : ''}`,
    );
  }
  const exemptSaid = exempt.length ? `; ${exempt.length} exempt as generated, not verified (below)` : '';
  if (!tracked.length) note('seals', 'nothing committed yet');
  else if (!bad.length)
    ok(
      'seals',
      `${tracked.length - exempt.length} of ${tracked.length} committed file(s) verify against this pot's registry${exemptSaid}`,
    );
  else {
    fail(
      'seals',
      `${bad.length} of ${tracked.length} committed file(s) do not verify: ${bad.slice(0, 5).join(', ')}${bad.length > 5 ? ', …' : ''}${exemptSaid}`,
    );
    if (custody) note('test keys', PV.NOT_LAB_NOTE);
  }
  if (exempt.length)
    note(
      'generated',
      `unsealed by design, held to their derivation: ${exempt.join(', ')}; the gate was NOT run by this doctor (run it where the generator lives). From ${gen.file}, whose seal verified in this run`,
    );
  else if (gen.why) note('generated', gen.why);
  if (softened) note('test keys', PV.labNote(softened));
}

// 7. the garden MCP on this host: one reading, the probe's (host-service.mjs beside this file)
let HS = null;
try {
  HS = await import(new URL('./host-service.mjs', import.meta.url).href);
} catch (e) {
  fail(
    'mcp service',
    `this rt carries no readable tools/rt/host-service.mjs (${e.code || e.message}): the garden MCP cannot be installed or checked from it`,
  );
}
if (HS) {
  const hctx = HS.doctorContext(pot);
  let facts = null;
  try {
    facts = HS.potFacts(hctx);
  } catch (e) {
    note('mcp service', `not checked: ${e.message}`);
  }
  if (facts) {
    const { rows } = await HS.connectionRows(hctx, facts);
    for (const [s, w, d, kind] of rows) {
      if (s === 'FAIL' && kind === 'not installed') note(w, `${d} (planting §8, after the keys and the pins)`);
      else if (s === 'FAIL') fail(w, d);
      else if (s === 'ok') ok(w, d);
      else note(w, d);
    }
  }
}
report();
