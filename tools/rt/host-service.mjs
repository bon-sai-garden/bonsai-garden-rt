#!/usr/bin/env node
// ---
// id: 01M59EZRG6H48DZH7ZNGG2AMNR
// title: rt's host service — the garden's MCP as a long-lived service on this host, registered once at user level under the garden's own name
// type: script
// tended-by: baisai
// created: 2026-10-19
// updated: 2026-10-19
// ---
//
//   node <rt>/tools/rt/host-service.mjs install     --pot <dir> [--name <mcp name>] [--port <n>] [--launcher pm2|launchd] [--wait <s>]
//   node <rt>/tools/rt/host-service.mjs probe       --pot <dir> [--kin <slug>[,<slug>...]]
//   node <rt>/tools/rt/host-service.mjs link-skills --pot <dir>
//   node <rt>/tools/rt/host-service.mjs uninstall   --pot <dir>
//
// A garden's MCP runs as one long-lived HTTP service on 127.0.0.1, one port and one name per garden, so that two gardens
// can live on one host. PM2 runs it where PM2 is present, else a LaunchAgent on macOS. It is registered once, at user
// level, with Claude (`claude mcp add --scope user --transport http <name> <url>`): every session on this host reaches it,
// and none starts it. The shape it replaces, a server per session started from the pot's own .mcp.json, is dropped by the
// desktop app after one turn; install retires that entry from the pot's .mcp.json (the old bytes kept in this garden's
// service folder) so no session sees two garden MCPs.
//
// Linux without PM2: a systemd --user unit is owed, not built; install refuses and says so. Windows: owed.
//
// install also links each kin's skill into the pot (.claude/skills/<slug> -> agents/<slug>, local only, never a copy), so
// a new kin's /<slug> is known to Claude in a session opened in the pot; where a link cannot be made it names the step.
//
// probe tells "not connected" (not installed, not running, not registered, turned off in this pot, shadowed by a
// project entry) apart from "answered STOP" (a kin's integrity line refused), and says which.
//
// Host-local state, per garden (its ULID), never in the pot: ~/.bonsai-garden/services/<garden>.json (the record) and
// ~/.bonsai-garden/services/<garden>/ (PM2's ecosystem file, the service's logs, the pot's retired .mcp.json).
//
// PM2 is always called with a clean environment (HOME, USER, LOGNAME, LANG, PATH, PM2_HOME): PM2 saves the environment
// it is called with into its dump, and a shell's tokens have no business there.
//
// Exit: install, link-skills, uninstall: 0 done · 2 refused (nothing changed; one line: why, and what to do) · 3 halt (a
// step failed part way; the line says what is left) · 64 usage. probe: 0 connected, every kin answered ok · 1 not
// connected (which) · 2 answered STOP (which kin, and its line) · 64 usage.

import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export const RECORD_FORMAT = 'bonsai-garden-host-service/1';
export const PORT_RANGE = [5100, 5899];
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;
const SLUG_RE = /^[a-z0-9][a-z0-9-]*$/;
const LABEL_PREFIX = 'bonsai-garden.mcp.';

class Exit extends Error {
  constructor(code, msg) {
    super(msg);
    this.exit = code;
  }
}
const refuse = (m) => new Exit(2, m);
const halt = (m) => new Exit(3, m);
const usage = (m) => new Exit(64, m);

const USAGE = `host-service.mjs — the garden's MCP as a host service, registered once at user level

  install     --pot <dir> [--name <mcp name>] [--port <n>] [--launcher pm2|launchd] [--wait <s>]
              run the garden's MCP on 127.0.0.1 under PM2 (else a LaunchAgent on macOS), register it with Claude at
              user level under the garden's own name (default <garden name>-garden), retire the pot's per-session
              .mcp.json entry, and link each kin's skill into the pot. Idempotent: a second run changes nothing.
  probe       --pot <dir> [--kin <slug>[,<slug>...]]
              connected? (installed, running, answering, registered, not turned off here, not shadowed), then each kin's
              integrity line through the service: ok, or the STOP it answered
  link-skills --pot <dir>   only the skill links (a kin planted after install)
  uninstall   --pot <dir>   the way back: stop and remove the service, unregister it, remove the links install made,
              restore the pot's .mcp.json if it is as install left it; the record is kept, marked uninstalled

Binaries by absolute path: BONSAI_PM2, BONSAI_LAUNCHCTL, BONSAI_CLAUDE, BONSAI_UV when set (and then only they), else PATH.

Exit (install, link-skills, uninstall): 0 done · 2 refused · 3 halt · 64 usage.
Exit (probe): 0 connected and every kin ok · 1 not connected · 2 answered STOP · 64 usage.`;

// ── arguments ────────────────────────────────────────────────────────────────────────────────────────────────────────────

const KNOWN = new Set(['pot', 'name', 'port', 'launcher', 'wait', 'kin']);

function parse(argv) {
  const pos = [];
  const f = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) {
      pos.push(a);
      continue;
    }
    const k = a.slice(2);
    if (k === 'help') throw new Exit(0, USAGE);
    if (!KNOWN.has(k)) throw usage(`unknown flag --${k}`);
    const v = argv[i + 1];
    if (v === undefined || v.startsWith('--')) throw usage(`--${k} needs a value`);
    f[k] = v;
    i++;
  }
  return { pos, f };
}

// ── the host: binaries, a clean environment, subprocesses ─────────────────────────────────────────────────────────────

function findBin(name, envVar, home) {
  if (process.env[envVar] !== undefined)
    return fs.existsSync(process.env[envVar]) ? path.resolve(process.env[envVar]) : null;
  const dirs = [
    ...(process.env.PATH || '').split(path.delimiter),
    path.join(home, '.local', 'bin'),
    path.join(home, '.cargo', 'bin'),
    '/opt/homebrew/bin',
    '/usr/local/bin',
  ];
  for (const d of dirs.filter(Boolean)) {
    const p = path.join(d, process.platform === 'win32' ? `${name}.exe` : name);
    try {
      if (fs.statSync(p).isFile()) return p;
    } catch {
      /* not here */
    }
  }
  return null;
}

function run(cmd, args, { env = process.env, cwd, timeout = 60000 } = {}) {
  const r = spawnSync(cmd, args, { env, cwd, encoding: 'utf8', timeout });
  return {
    code: r.error ? null : r.status,
    stdout: r.stdout || '',
    out: `${r.stdout || ''}${r.stderr || ''}`.trim(),
    error: r.error,
  };
}
const tail = (s, n = 240) => (s.length > n ? `…${s.slice(-n)}` : s);

/** The environment PM2 is called with: never the caller's shell, whose tokens PM2 would save into its dump. */
function cleanEnv(ctx) {
  const e = { HOME: ctx.home, PATH: process.env.PATH || '/usr/bin:/bin' };
  for (const k of ['USER', 'LOGNAME', 'LANG', 'PM2_HOME']) if (process.env[k]) e[k] = process.env[k];
  return e;
}

function context(argv, deps = {}) {
  const { pos, f } = parse(argv);
  const verb = pos[0];
  if (!verb) throw usage(USAGE);
  if (!f.pot) throw usage(`${verb} needs --pot <dir>`);
  const home = deps.home || os.homedir();
  return {
    verb,
    f,
    pot: path.resolve(f.pot),
    home,
    platform: deps.platform || process.platform,
    uid: deps.uid ?? (typeof process.getuid === 'function' ? process.getuid() : null),
    say: deps.say || ((s) => console.log(s)),
    bins: {
      pm2: findBin('pm2', 'BONSAI_PM2', home),
      launchctl:
        findBin('launchctl', 'BONSAI_LAUNCHCTL', home) || (fs.existsSync('/bin/launchctl') ? '/bin/launchctl' : null),
      claude: findBin('claude', 'BONSAI_CLAUDE', home),
      uv: findBin('uv', 'BONSAI_UV', home),
      git: findBin('git', 'BONSAI_GIT', home) || 'git',
      node: process.execPath,
    },
  };
}

// ── the pot ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

const readJson = (file) => {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
};
const sha256 = (x) => crypto.createHash('sha256').update(x).digest('hex');
const real = (p) => {
  try {
    return fs.realpathSync(p);
  } catch {
    return path.resolve(p);
  }
};

function gitConfig(ctx, key) {
  const r = run(ctx.bins.git, ['-C', ctx.pot, 'config', '--get', key]);
  return r.code === 0 && r.stdout.trim() ? r.stdout.trim() : null;
}

/** What the pot says about itself: its garden (id, name), its rt (path, name, version), the server the service runs. */
export function potFacts(ctx) {
  const pot = ctx.pot;
  if (!fs.existsSync(path.join(pot, '.git'))) throw refuse(`${pot} is not a git repository: a pot is one`);
  const g = readJson(path.join(pot, 'garden.json'));
  if (!g)
    throw refuse(
      `${pot} has no readable garden.json: the keys' id step writes it, and the host service comes after it`,
    );
  if (typeof g.garden !== 'string' || !ULID_RE.test(g.garden))
    throw refuse(`garden.json names no garden id (a ULID), so the service has nothing to be keyed by`);
  if (typeof g.name !== 'string' || !SLUG_RE.test(g.name)) throw refuse('garden.json names no garden name');
  if (!fs.existsSync(path.join(pot, 'agents', 'registry.json')))
    throw refuse(
      'the pot has no agents/registry.json yet: the garden MCP refuses to serve a pot without one (seed it first)',
    );
  const rt = gitConfig(ctx, 'bonsai.rt');
  if (!rt)
    throw refuse('git config bonsai.rt is unset in this pot: install-runtime wires the rt the service runs from');
  const server = path.join(rt, 'tools', 'bonsai-mcp', 'server.py');
  if (!fs.existsSync(server))
    throw refuse(`the pot's rt (${rt}) carries no garden MCP server (tools/bonsai-mcp/server.py)`);
  const rec = readJson(path.join(pot, 'runtime.json'));
  if (!rec?.name || !rec?.version)
    throw refuse(
      'the pot has no readable runtime.json: install-runtime records the rt it took, and the service is named by it',
    );
  return {
    pot: real(pot),
    garden: g.garden,
    gardenName: g.name,
    rt: real(rt),
    server: real(server),
    rtName: rec.name,
    rtVersion: rec.version,
  };
}

/** Every kin with a body in this pot (a registry row whose path holds a SKILL.md): the skills to link, the kin to probe. */
export function kinWithBodies(pot) {
  const reg = readJson(path.join(pot, 'agents', 'registry.json'));
  const rows = Array.isArray(reg?.agents) ? reg.agents : [];
  const out = [];
  for (const r of rows) {
    if (!r || typeof r.slug !== 'string' || !SLUG_RE.test(r.slug) || typeof r.path !== 'string') continue;
    const body = path.resolve(pot, r.path);
    if (!body.startsWith(path.resolve(pot) + path.sep)) continue; // a row never points out of the pot
    const skill = path.join(body, 'SKILL.md');
    if (!fs.existsSync(skill)) continue;
    const m = /^---\n[\s\S]*?^name:\s*['"]?([^'"\n]+?)['"]?\s*$/m.exec(fs.readFileSync(skill, 'utf8'));
    out.push({ slug: r.slug, body, skillName: m ? m[1].trim() : r.slug });
  }
  return out;
}

// ── the record ───────────────────────────────────────────────────────────────────────────────────────────────────────────

const servicesDir = (home) => path.join(home, '.bonsai-garden', 'services');
const recordFile = (home, garden) => path.join(servicesDir(home), `${garden}.json`);
const serviceDir = (home, garden) => path.join(servicesDir(home), garden);
export const readRecord = (home, garden) => readJson(recordFile(home, garden));

function otherRecords(home, garden) {
  const dir = servicesDir(home);
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir)
    .filter((n) => n.endsWith('.json') && n !== `${garden}.json`)
    .map((n) => readJson(path.join(dir, n)))
    .filter((r) => r?.format === RECORD_FORMAT && r.status === 'installed');
}

function writeRecord(home, rec) {
  fs.mkdirSync(servicesDir(home), { recursive: true });
  fs.writeFileSync(recordFile(home, rec.garden), `${JSON.stringify(rec, null, 2)}\n`);
}

// ── Claude's own configuration, read only (it is written through its CLI) ─────────────────────────────────────────────

const claudeConfigFile = (ctx) =>
  process.env.CLAUDE_CONFIG_DIR
    ? path.join(process.env.CLAUDE_CONFIG_DIR, '.claude.json')
    : path.join(ctx.home, '.claude.json');

function claudeConfig(ctx) {
  return readJson(claudeConfigFile(ctx));
}

function projectOf(cfg, pot) {
  const want = real(pot);
  for (const [k, v] of Object.entries(cfg?.projects || {})) if (real(k) === want) return v || {};
  return null;
}

// ── the port ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

function portFree(port) {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.once('error', () => resolve(false));
    s.listen(port, '127.0.0.1', () => s.close(() => resolve(true)));
  });
}

async function choosePort(ctx, facts, rec, others) {
  const claimed = new Map(others.map((r) => [r.port, r]));
  const asked = ctx.f.port !== undefined ? Number(ctx.f.port) : null;
  if (asked !== null) {
    if (!Number.isInteger(asked) || asked < 1024 || asked > 65535)
      throw usage(`--port ${ctx.f.port} is not a port (1024-65535)`);
    if (claimed.has(asked))
      throw refuse(`port ${asked} is the service of ${claimed.get(asked).name} (garden ${claimed.get(asked).garden})`);
    if (rec?.status === 'installed' && rec.port === asked) return asked;
    if (!(await portFree(asked)))
      throw refuse(`port ${asked} is in use on 127.0.0.1 by something that is not this garden's service`);
    return asked;
  }
  if (rec?.status === 'installed') return rec.port; // the garden keeps its port
  if (rec?.port && !claimed.has(rec.port) && (await portFree(rec.port))) return rec.port; // and comes back to it after an uninstall
  const [lo, hi] = PORT_RANGE;
  const span = hi - lo + 1;
  const first = lo + (Number.parseInt(sha256(facts.garden).slice(0, 8), 16) % span);
  for (let i = 0; i < span; i++) {
    const p = lo + ((first - lo + i) % span);
    if (claimed.has(p)) continue;
    if (await portFree(p)) return p;
  }
  throw refuse(`no free port in ${lo}-${hi} on 127.0.0.1; name one with --port`);
}

// ── the service's definition ─────────────────────────────────────────────────────────────────────────────────────────

function definition(ctx, facts, name, port, launcher) {
  const dirs = [path.dirname(ctx.bins.uv), path.dirname(ctx.bins.node)];
  if (path.isAbsolute(ctx.bins.git)) dirs.push(path.dirname(ctx.bins.git));
  const PATH = [...new Set([...dirs, '/usr/bin', '/bin', '/usr/sbin', '/sbin'])].join(path.delimiter);
  const tag = `${facts.rtName}-${facts.rtVersion}`;
  const env = {
    BONSAI_GARDEN: facts.pot,
    BONSAI_MCP_NAME: name,
    HOME: ctx.home,
    LANG: process.env.LANG || 'en_US.UTF-8',
    PATH,
    // rt stays exactly its manifest: its Python environment and its bytecode live outside it (install-runtime's rt-env)
    UV_PROJECT_ENVIRONMENT: path.join(ctx.home, '.bonsai-garden', 'rt-env', tag),
    PYTHONPYCACHEPREFIX: path.join(ctx.home, '.bonsai-garden', 'pycache', tag),
  };
  for (const k of ['UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR']) if (process.env[k]) env[k] = process.env[k]; // uv's own dirs, when the host moved them
  const args = ['run', '--frozen', '--project', path.dirname(facts.server), 'python', facts.server];
  args.push('--garden', facts.pot, '--transport', 'http', '--port', String(port));
  const def = { launcher, name, port, url: `http://127.0.0.1:${port}/`, cmd: ctx.bins.uv, args, env, cwd: facts.pot };
  return { ...def, hash: sha256(JSON.stringify(def)) };
}

// ── PM2 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────

const pm2Name = (name) => `${name}-mcp`;

/** PM2's own folder, where its daemon's socket lives: a reading never starts a daemon that is not running. */
const pm2Home = (ctx) => process.env.PM2_HOME || path.join(ctx.home, '.pm2');

/** A Unix socket path holds 103 bytes here (104 with its NUL, macOS); past that PM2 starts a daemon that cannot listen,
 *  and it spins (found rehearsing with a PM2_HOME deep in a scratch folder: a daemon at 70 % CPU, reachable by nothing). */
function pm2SocketProblem(ctx) {
  const sock = path.join(pm2Home(ctx), 'rpc.sock');
  const n = Buffer.byteLength(sock);
  return n > 103
    ? `PM2's daemon socket would be ${sock} (${n} bytes), over the 103 a Unix socket path may hold: PM2 would start a daemon that cannot listen. Set PM2_HOME to a shorter folder`
    : null;
}

function pm2List(ctx, { spawn = false } = {}) {
  if (!spawn && !fs.existsSync(path.join(pm2Home(ctx), 'rpc.sock')))
    return { error: `the PM2 daemon is not running (no ${path.join(pm2Home(ctx), 'rpc.sock')})` };
  if (spawn && pm2SocketProblem(ctx)) return { error: pm2SocketProblem(ctx) };
  const r = run(ctx.bins.pm2, ['jlist'], { env: cleanEnv(ctx) });
  if (r.code !== 0) return { error: `pm2 jlist failed (exit ${r.code}): ${tail(r.out)}` };
  // the list is the last line that parses as a JSON array: PM2 prints its own lines first when it spawns its daemon or
  // finds itself out of date ("[PM2] Spawning PM2 daemon", in colour), and their brackets are not the list's
  const lines = r.stdout.split('\n').filter((l) => l.trim());
  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      const v = JSON.parse(lines[i]);
      if (Array.isArray(v)) return { apps: v };
    } catch {
      /* one of PM2's own lines */
    }
  }
  return { error: `pm2 jlist printed no list: ${tail(r.out)}` };
}

function pm2App(ctx, app, opts) {
  const l = pm2List(ctx, opts);
  if (l.error) return { error: l.error };
  const a = l.apps.find((x) => x?.name === app);
  return a ? { status: a.pm2_env?.status || 'unknown', pid: a.pid } : null; // only these two fields are read: an app's env is never printed
}

function pm2Install(ctx, def, rec) {
  const dir = serviceDir(ctx.home, def.garden);
  fs.mkdirSync(dir, { recursive: true });
  const eco = path.join(dir, 'pm2-ecosystem.json');
  const app = {
    name: def.app,
    script: def.cmd,
    args: def.args,
    interpreter: 'none',
    cwd: def.cwd,
    env: def.env,
    autorestart: true,
    min_uptime: 10000,
    max_restarts: 10,
    restart_delay: 3000,
    out_file: path.join(dir, 'mcp.out.log'),
    error_file: path.join(dir, 'mcp.err.log'),
    time: true,
  };
  fs.writeFileSync(eco, `${JSON.stringify({ apps: [app] }, null, 2)}\n`);
  const cur = pm2App(ctx, def.app, { spawn: true });
  if (cur?.error) throw halt(cur.error);
  if (cur && rec?.hash === def.hash && cur.status === 'online') {
    ctx.say(`service: PM2 app ${def.app} already runs this definition (pid ${cur.pid}); left as it is`);
    return false;
  }
  if (cur) {
    const d = run(ctx.bins.pm2, ['delete', def.app], { env: cleanEnv(ctx) });
    if (d.code !== 0) throw halt(`pm2 delete ${def.app} failed (exit ${d.code}): ${tail(d.out)}`);
  }
  const s = run(ctx.bins.pm2, ['start', eco], { env: cleanEnv(ctx) });
  if (s.code !== 0) throw halt(`pm2 start failed (exit ${s.code}): ${tail(s.out)}; nothing registered yet`);
  const v = run(ctx.bins.pm2, ['save'], { env: cleanEnv(ctx) });
  if (v.code !== 0)
    throw halt(
      `pm2 save failed (exit ${v.code}): ${tail(v.out)}; the service runs but will not come back after a restart`,
    );
  ctx.say(`service: PM2 app ${def.app} ${cur ? 'replaced' : 'started'} and saved (${eco})`);
  return true;
}

function pm2Remove(ctx, rec) {
  const cur = pm2App(ctx, rec.app, { spawn: true });
  if (cur?.error) throw halt(cur.error);
  if (!cur) {
    ctx.say(`service: no PM2 app ${rec.app}; nothing to stop`);
    return;
  }
  const d = run(ctx.bins.pm2, ['delete', rec.app], { env: cleanEnv(ctx) });
  if (d.code !== 0) throw halt(`pm2 delete ${rec.app} failed (exit ${d.code}): ${tail(d.out)}`);
  const v = run(ctx.bins.pm2, ['save'], { env: cleanEnv(ctx) });
  if (v.code !== 0)
    throw halt(`pm2 save failed (exit ${v.code}): ${tail(v.out)}; the app is stopped, PM2's saved list still names it`);
  ctx.say(`service: PM2 app ${rec.app} deleted and the list saved`);
}

// ── launchd (macOS without PM2) ──────────────────────────────────────────────────────────────────────────────────────────

const plistFile = (ctx, label) => path.join(ctx.home, 'Library', 'LaunchAgents', `${label}.plist`);
const xml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function plistText(def, logs) {
  const str = (s) => `<string>${xml(s)}</string>`;
  const lines = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
    '<plist version="1.0">',
    '<dict>',
    `  <key>Label</key>${str(def.label)}`,
    '  <key>ProgramArguments</key>',
    '  <array>',
    ...[def.cmd, ...def.args].map((a) => `    ${str(a)}`),
    '  </array>',
    '  <key>EnvironmentVariables</key>',
    '  <dict>',
    ...Object.entries(def.env).map(([k, v]) => `    <key>${xml(k)}</key>${str(v)}`),
    '  </dict>',
    `  <key>WorkingDirectory</key>${str(def.cwd)}`,
    '  <key>RunAtLoad</key><true/>',
    '  <key>KeepAlive</key><true/>',
    '  <key>ThrottleInterval</key><integer>10</integer>',
    // a service a session waits on is interactive work: never the background band that throttles it
    '  <key>ProcessType</key><string>Interactive</string>',
    `  <key>StandardOutPath</key>${str(path.join(logs, 'mcp.out.log'))}`,
    `  <key>StandardErrorPath</key>${str(path.join(logs, 'mcp.err.log'))}`,
    '</dict>',
    '</plist>',
    '',
  ];
  return lines.join('\n');
}

const launchdTarget = (ctx, label) => `gui/${ctx.uid}/${label}`;

function launchdState(ctx, label) {
  const r = run(ctx.bins.launchctl, ['print', launchdTarget(ctx, label)]);
  if (r.code !== 0) return null;
  const st = /^\s*state = (.+)$/m.exec(r.stdout);
  const pid = /^\s*pid = (\d+)$/m.exec(r.stdout);
  return { status: st ? st[1].trim() : 'loaded', pid: pid ? Number(pid[1]) : null };
}

function launchdInstall(ctx, def, rec) {
  const file = plistFile(ctx, def.label);
  const logs = serviceDir(ctx.home, def.garden);
  fs.mkdirSync(logs, { recursive: true });
  const text = plistText(def, logs);
  const same = fs.existsSync(file) && fs.readFileSync(file, 'utf8') === text;
  const st = launchdState(ctx, def.label);
  if (same && st && rec?.hash === def.hash && st.status === 'running') {
    ctx.say(`service: LaunchAgent ${def.label} already runs this definition (pid ${st.pid}); left as it is`);
    return false;
  }
  if (st) {
    const b = run(ctx.bins.launchctl, ['bootout', launchdTarget(ctx, def.label)]);
    if (b.code !== 0) throw halt(`launchctl bootout ${def.label} failed (exit ${b.code}): ${tail(b.out)}`);
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
  const r = run(ctx.bins.launchctl, ['bootstrap', `gui/${ctx.uid}`, file]);
  if (r.code !== 0)
    throw halt(`launchctl bootstrap ${file} failed (exit ${r.code}): ${tail(r.out)}; the plist is written, not loaded`);
  ctx.say(`service: LaunchAgent ${def.label} ${st ? 'replaced' : 'loaded'} (${file})`);
  return true;
}

function launchdRemove(ctx, rec) {
  const file = plistFile(ctx, rec.label);
  if (launchdState(ctx, rec.label)) {
    const b = run(ctx.bins.launchctl, ['bootout', launchdTarget(ctx, rec.label)]);
    if (b.code !== 0) throw halt(`launchctl bootout ${rec.label} failed (exit ${b.code}): ${tail(b.out)}`);
  }
  if (fs.existsSync(file)) {
    if (sha256(fs.readFileSync(file)) !== rec['plist-sha256'])
      ctx.say(
        `service: ${file} changed since install; unloaded, and left on disk for you to read (it is no longer loaded)`,
      );
    else fs.rmSync(file);
  }
  ctx.say(`service: LaunchAgent ${rec.label} unloaded and its plist removed`);
}

// ── registration with Claude, at user level ──────────────────────────────────────────────────────────────────────────

function registration(ctx, name) {
  const e = claudeConfig(ctx)?.mcpServers?.[name];
  return e ? { type: e.type || (e.command ? 'stdio' : '?'), url: e.url || null, command: e.command || null } : null;
}

function register(ctx, def) {
  const cur = registration(ctx, def.name);
  if (cur && cur.type === 'http' && cur.url === def.url) {
    ctx.say(`registered: ${def.name} at user level, ${def.url} (already)`);
    return false;
  }
  const env = { ...process.env, HOME: ctx.home };
  if (cur) {
    const r = run(ctx.bins.claude, ['mcp', 'remove', '--scope', 'user', def.name], { env, cwd: ctx.home });
    if (r.code !== 0) throw halt(`claude mcp remove --scope user ${def.name} failed (exit ${r.code}): ${tail(r.out)}`);
  }
  const r = run(ctx.bins.claude, ['mcp', 'add', '--scope', 'user', '--transport', 'http', def.name, def.url], {
    env,
    cwd: ctx.home,
  });
  if (r.code !== 0)
    throw halt(`claude mcp add failed (exit ${r.code}): ${tail(r.out)}; the service runs, unregistered`);
  const now = registration(ctx, def.name);
  if (!(now && now.type === 'http' && now.url === def.url))
    throw halt(`claude mcp add said done, but ${claudeConfigFile(ctx)} does not name ${def.name} at ${def.url}`);
  ctx.say(`registered: ${def.name} at user level, ${def.url}`);
  return true;
}

function unregister(ctx, rec) {
  const cur = registration(ctx, rec.name);
  if (!cur) return ctx.say(`registered: ${rec.name} is not registered; nothing to remove`);
  if (!(cur.type === 'http' && cur.url === rec.url))
    return ctx.say(`registered: ${rec.name} now points at ${cur.url || cur.command}, not this service; left as it is`);
  const r = run(ctx.bins.claude, ['mcp', 'remove', '--scope', 'user', rec.name], {
    env: { ...process.env, HOME: ctx.home },
    cwd: ctx.home,
  });
  if (r.code !== 0) throw halt(`claude mcp remove --scope user ${rec.name} failed (exit ${r.code}): ${tail(r.out)}`);
  ctx.say(`registered: ${rec.name} removed from user level`);
}

// ── the pot's own .mcp.json: a per-session garden server there would shadow the service ─────────────────────────────────

/** The entries of the pot's .mcp.json that run a garden MCP per session, or carry this service's name. */
function shadowEntries(pot, name) {
  const cfg = readJson(path.join(pot, '.mcp.json'));
  const servers = cfg?.mcpServers || {};
  return Object.entries(servers)
    .filter(
      ([k, v]) =>
        k === name ||
        (Array.isArray(v?.args) && v.args.some((a) => /(^|[\\/])bonsai-mcp[\\/]server\.py$/.test(String(a)))),
    )
    .map(([k]) => k);
}

function retireShadow(ctx, def) {
  const file = path.join(ctx.pot, '.mcp.json');
  const keys = shadowEntries(ctx.pot, def.name);
  if (!keys.length) return null;
  const before = fs.readFileSync(file);
  const kept = path.join(serviceDir(ctx.home, def.garden), 'pot-mcp.json.before');
  fs.mkdirSync(path.dirname(kept), { recursive: true });
  if (!fs.existsSync(kept)) fs.writeFileSync(kept, before);
  const cfg = JSON.parse(before.toString('utf8'));
  for (const k of keys) delete cfg.mcpServers[k];
  const text = `${JSON.stringify(cfg, null, 2)}\n`;
  fs.writeFileSync(file, text);
  ctx.say(
    `retired: ${keys.join(', ')} from the pot's .mcp.json (a server per session, which the desktop app drops); the old bytes kept at ${kept}`,
  );
  return { keys, kept, 'wrote-sha256': sha256(text) };
}

function restoreShadow(ctx, rec) {
  const r = rec.retired;
  if (!r) return;
  const file = path.join(ctx.pot, '.mcp.json');
  if (!fs.existsSync(r.kept)) return ctx.say(`restore: ${r.kept} is gone; the pot's .mcp.json left as it is`);
  if (!fs.existsSync(file) || sha256(fs.readFileSync(file)) !== r['wrote-sha256'])
    return ctx.say(
      `restore: the pot's .mcp.json changed since install; left as it is (its old bytes are at ${r.kept})`,
    );
  fs.copyFileSync(r.kept, file);
  ctx.say(`restore: the pot's .mcp.json is back as it was before install (${r.keys.join(', ')})`);
}

// ── skills: each kin's /<slug> known to Claude in a session opened in the pot ──────────────────────────────────────────

function excludeFile(ctx) {
  const r = run(ctx.bins.git, ['-C', ctx.pot, 'rev-parse', '--git-path', 'info/exclude']);
  return r.code === 0 ? path.resolve(ctx.pot, r.stdout.trim()) : null;
}

export function linkSkills(ctx, prior = []) {
  const kin = kinWithBodies(ctx.pot);
  const dir = path.join(ctx.pot, '.claude', 'skills');
  const linked = [];
  const steps = [];
  for (const k of kin) {
    const link = path.join(dir, k.slug);
    const target = path.relative(dir, k.body);
    let st = null;
    try {
      st = fs.lstatSync(link);
    } catch {
      /* none yet */
    }
    if (st) {
      if (st.isSymbolicLink() && real(link) === real(k.body)) {
        linked.push({ slug: k.slug, link: path.relative(ctx.pot, link), made: prior.includes(k.slug) });
        ctx.say(`skill: /${k.skillName} is linked (${path.relative(ctx.pot, link)} -> ${target})`);
      } else
        steps.push(
          `${path.relative(ctx.pot, link)} exists and is not a link to ${path.relative(ctx.pot, k.body)}: left as it is. ` +
            `Move it aside yourself, then: ln -s ${target} ${path.relative(ctx.pot, link)} (in ${ctx.pot})`,
        );
      continue;
    }
    try {
      fs.mkdirSync(dir, { recursive: true });
      fs.symlinkSync(target, link, 'dir');
      linked.push({ slug: k.slug, link: path.relative(ctx.pot, link), made: true });
      ctx.say(`skill: /${k.skillName} linked (${path.relative(ctx.pot, link)} -> ${target})`);
    } catch (e) {
      steps.push(
        `the link for /${k.skillName} could not be made (${e.code || e.message}). In ${ctx.pot}: ` +
          `ln -s ${target} ${path.relative(ctx.pot, link)}  (a link, never a copy)`,
      );
    }
  }
  if (linked.length) {
    const ex = excludeFile(ctx);
    const line = '/.claude/skills/';
    if (!ex)
      steps.push(
        `git could not name this pot's info/exclude; add the line ${line} to it yourself, never to the sealed .gitignore`,
      );
    else {
      const was = fs.existsSync(ex) ? fs.readFileSync(ex, 'utf8') : '';
      if (!was.split('\n').includes(line)) {
        fs.mkdirSync(path.dirname(ex), { recursive: true });
        fs.writeFileSync(ex, `${was}${was && !was.endsWith('\n') ? '\n' : ''}${line}\n`);
        ctx.say(
          `skill: ${line} added to the pot's .git/info/exclude (local only; the sealed .gitignore is not touched)`,
        );
      }
    }
  }
  if (!kin.length) ctx.say('skill: no kin with a body in the registry yet; nothing to link');
  for (const s of steps) ctx.say(`STEP  ${s}`);
  return { linked, steps };
}

function unlinkSkills(ctx, rec) {
  for (const l of rec.skills || []) {
    if (!l.made) continue;
    const link = path.join(ctx.pot, l.link);
    try {
      if (fs.lstatSync(link).isSymbolicLink()) {
        fs.unlinkSync(link);
        ctx.say(`skill: ${l.link} unlinked (the body is untouched)`);
      }
    } catch {
      /* already gone */
    }
  }
}

// ── MCP over HTTP: a minimal client (initialize, one call, close) ─────────────────────────────────────────────────────

function parseBody(text, ct) {
  if (/event-stream/.test(ct || '')) {
    const out = [];
    for (const block of text.split(/\r?\n\r?\n/)) {
      const data = block
        .split(/\r?\n/)
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).trimStart())
        .join('\n');
      if (!data) continue;
      try {
        out.push(JSON.parse(data));
      } catch {
        /* a partial event */
      }
    }
    return out;
  }
  try {
    const j = JSON.parse(text);
    return Array.isArray(j) ? j : [j];
  } catch {
    return [];
  }
}

function post(url, payload, { sid, timeoutMs = 10000, method = 'POST' } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const data = payload === undefined ? null : Buffer.from(JSON.stringify(payload));
    let settled = false;
    const req = http.request(
      {
        host: u.hostname,
        port: u.port,
        path: u.pathname || '/',
        method,
        headers: {
          'content-type': 'application/json',
          accept: 'application/json, text/event-stream',
          ...(sid ? { 'mcp-session-id': sid } : {}),
          ...(data ? { 'content-length': data.length } : {}),
        },
      },
      (res) => {
        let buf = '';
        const want = payload && payload.id !== undefined ? payload.id : null;
        const done = () => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve({
            status: res.statusCode,
            sid: res.headers['mcp-session-id'] || sid,
            messages: parseBody(buf, res.headers['content-type']),
          });
          req.destroy();
        };
        res.setEncoding('utf8');
        res.on('data', (c) => {
          buf += c;
          if (want !== null && parseBody(buf, res.headers['content-type']).some((m) => m?.id === want)) done();
        });
        res.on('end', done);
        res.on('error', () => done());
      },
    );
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      req.destroy();
      reject(new Error(`no answer in ${Math.round(timeoutMs / 1000)} s`));
    }, timeoutMs);
    req.on('error', (e) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(e);
    });
    if (data) req.write(data);
    req.end();
  });
}

export async function mcpSession(url, timeoutMs = 10000) {
  const init = await post(
    url,
    {
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: {
        protocolVersion: '2025-06-18',
        capabilities: {},
        clientInfo: { name: 'garden-host-service-probe', version: '1' },
      },
    },
    { timeoutMs },
  );
  const r = init.messages.find((m) => m?.id === 1);
  if (!r?.result) throw new Error(`initialize: ${r?.error?.message || `HTTP ${init.status}, no answer`}`);
  const sid = init.sid;
  await post(url, { jsonrpc: '2.0', method: 'notifications/initialized' }, { sid, timeoutMs });
  let n = 1;
  return {
    info: r.result.serverInfo || {},
    async tool(name, args, ms = 120000) {
      const id = ++n;
      const res = await post(
        url,
        { jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args } },
        { sid, timeoutMs: ms },
      );
      const m = res.messages.find((x) => x?.id === id);
      if (!m) throw new Error(`${name}: no answer (HTTP ${res.status})`);
      if (m.error) throw new Error(`${name}: ${m.error.message}`);
      return (m.result?.content || [])
        .filter((c) => c?.type === 'text')
        .map((c) => c.text)
        .join('\n');
    },
    async tools() {
      const id = ++n;
      const res = await post(url, { jsonrpc: '2.0', id, method: 'tools/list', params: {} }, { sid, timeoutMs });
      const m = res.messages.find((x) => x?.id === id);
      return (m?.result?.tools || []).map((t) => t.name);
    },
    async close() {
      try {
        await post(url, undefined, { sid, method: 'DELETE', timeoutMs: 3000 });
      } catch {
        /* the session ends with the server either way */
      }
    },
  };
}

async function answering(url, name, timeoutMs = 10000) {
  try {
    const s = await mcpSession(url, timeoutMs);
    await s.close();
    return s.info.name === name
      ? { ok: true, info: s.info }
      : { ok: false, why: `answers as ${s.info.name || '(no name)'}, not ${name}` };
  } catch (e) {
    return { ok: false, why: e.code === 'ECONNREFUSED' ? `nothing answers on ${url}` : e.message };
  }
}

async function waitAnswer(url, name, secs) {
  const until = Date.now() + secs * 1000;
  let last = null;
  while (Date.now() < until) {
    last = await answering(url, name, 5000);
    if (last.ok) return last;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return last || { ok: false, why: 'not asked' };
}

// ── connected? one reading, shared by probe and rt's doctor ─────────────────────────────────────────────────────────

/**
 * Rows of [status, what, detail, kind] for this pot's garden MCP on this host, read only: the record, the launcher, the
 * answer on the port, the user-level registration, this pot's own switch for it, and a per-session entry shadowing it.
 * status is ok, FAIL or note; kind names the "not connected" reason a FAIL stands for. Never throws.
 */
export async function connectionRows(ctx, facts) {
  const rows = [];
  const rec = readRecord(ctx.home, facts.garden);
  const step = `node ${path.join(facts.rt, 'tools', 'rt', 'host-service.mjs')} install --pot ${facts.pot}`;
  if (rec?.status !== 'installed') {
    rows.push([
      'FAIL',
      'mcp service',
      `not installed as a host service on this host${rec ? ' (uninstalled)' : ''}: ${step}`,
      'not installed',
    ]);
    return { rows, rec: null };
  }
  rows.push([
    'ok',
    'mcp record',
    `${recordFile(ctx.home, facts.garden)}: ${rec.name} on ${rec.url}, run by ${rec.launcher}`,
  ]);
  if (real(rec.pot) !== real(facts.pot))
    rows.push([
      'FAIL',
      'mcp service',
      `the record serves ${rec.pot}, not this pot (moved?): run install again here`,
      'moved',
    ]);
  // the launcher
  if (rec.launcher === 'pm2') {
    const a = ctx.bins.pm2 ? pm2App(ctx, rec.app) : { error: 'pm2 is not found on this host' };
    if (a?.error) rows.push(['FAIL', 'mcp launcher', `PM2: ${a.error}`, 'not running']);
    else if (!a) rows.push(['FAIL', 'mcp launcher', `PM2 has no app ${rec.app}: ${step}`, 'not running']);
    else if (a.status !== 'online')
      rows.push(['FAIL', 'mcp launcher', `PM2 app ${rec.app} is ${a.status}: pm2 restart ${rec.app}`, 'not running']);
    else rows.push(['ok', 'mcp launcher', `PM2 app ${rec.app} online (pid ${a.pid})`]);
  } else {
    const s = ctx.bins.launchctl ? launchdState(ctx, rec.label) : null;
    if (!s) rows.push(['FAIL', 'mcp launcher', `LaunchAgent ${rec.label} is not loaded: ${step}`, 'not running']);
    else if (s.status !== 'running')
      rows.push([
        'FAIL',
        'mcp launcher',
        `LaunchAgent ${rec.label} is ${s.status}: launchctl kickstart ${launchdTarget(ctx, rec.label)}`,
        'not running',
      ]);
    else rows.push(['ok', 'mcp launcher', `LaunchAgent ${rec.label} running (pid ${s.pid})`]);
  }
  // the answer
  const ans = await answering(rec.url, rec.name);
  if (ans.ok) rows.push(['ok', 'mcp answers', `${rec.url} answers as ${ans.info.name}`]);
  else
    rows.push([
      'FAIL',
      'mcp answers',
      `${ans.why}; its log: ${path.join(serviceDir(ctx.home, facts.garden), 'mcp.err.log')}`,
      'not running',
    ]);
  // the registration
  const reg = registration(ctx, rec.name);
  if (!reg)
    rows.push([
      'FAIL',
      'mcp registered',
      `${rec.name} is not registered with Claude at user level: ${step}`,
      'not registered',
    ]);
  else if (reg.type !== 'http' || reg.url !== rec.url)
    rows.push([
      'FAIL',
      'mcp registered',
      `${rec.name} is registered as ${reg.type} ${reg.url || reg.command}, not ${rec.url}: ${step}`,
      'not registered',
    ]);
  else rows.push(['ok', 'mcp registered', `${rec.name} at user level, ${rec.url} (${claudeConfigFile(ctx)})`]);
  // this pot's own switch, and a per-session entry that would shadow the service
  const cfg = claudeConfig(ctx);
  const proj = projectOf(cfg, facts.pot);
  if (proj?.disabledMcpServers?.includes(rec.name))
    rows.push([
      'FAIL',
      'mcp in this pot',
      `${rec.name} is turned off for this pot: in a session here, /mcp, select it, enable`,
      'turned off',
    ]);
  else rows.push(['ok', 'mcp in this pot', `${rec.name} is not turned off here`]);
  if (proj?.mcpServers?.[rec.name])
    rows.push([
      'FAIL',
      'mcp shadowed',
      `a local-scope entry named ${rec.name} for this pot shadows the service: in ${facts.pot}, claude mcp remove --scope local ${rec.name}`,
      'shadowed',
    ]);
  const sh = shadowEntries(facts.pot, rec.name);
  if (sh.length)
    rows.push([
      'FAIL',
      'mcp shadowed',
      `the pot's .mcp.json still runs a garden MCP per session (${sh.join(', ')}): ${step} retires it`,
      'shadowed',
    ]);
  if (proj && proj.hasTrustDialogAccepted !== true)
    rows.push([
      'note',
      'mcp trust',
      "this pot's folder has not been trusted in Claude yet; the first session here asks",
    ]);
  else if (!proj)
    rows.push([
      'note',
      'mcp trust',
      'no session has been opened in this pot yet; the first one asks whether to trust its folder',
    ]);
  // another garden's MCP, live in this pot's sessions too: a kin could enter the wrong garden
  const others = [];
  for (const [n, e] of Object.entries(cfg?.mcpServers || {})) {
    if (n === rec.name || e?.type !== 'http' || !/^http:\/\/(127\.0\.0\.1|localhost)[:/]/.test(String(e.url))) continue;
    if (proj?.disabledMcpServers?.includes(n)) continue;
    try {
      const s = await mcpSession(e.url, 3000);
      const t = await s.tools();
      await s.close();
      if (t.includes('bonsai_session_start')) others.push(n);
    } catch {
      /* not answering: not live here */
    }
  }
  if (others.length)
    rows.push([
      'note',
      'other gardens',
      `${others.join(', ')} also serve${others.length === 1 ? 's' : ''} a garden in this pot's sessions; turn ${others.length === 1 ? 'it' : 'them'} off here (/mcp), so a kin enters only ${rec.name}`,
    ]);
  return { rows, rec };
}

// ── the verbs ────────────────────────────────────────────────────────────────────────────────────────────────────────

async function cmdInstall(ctx) {
  const facts = potFacts(ctx);
  const name = ctx.f.name ?? `${facts.gardenName}-garden`;
  if (!NAME_RE.test(name))
    throw usage(`--name ${JSON.stringify(name)} is not a server name (^[a-z0-9][a-z0-9-]{0,62}$)`);
  const rec = readRecord(ctx.home, facts.garden);
  const others = otherRecords(ctx.home, facts.garden);
  // every precondition before anything is written: a refusal never writes
  const clash = others.find((r) => r.name === name);
  if (clash)
    throw refuse(`the name ${name} is the service of garden ${clash.garden} (${clash.pot}); name this one with --name`);
  let launcher = ctx.f.launcher || (rec?.status === 'installed' ? rec.launcher : null);
  if (!launcher) {
    if (ctx.bins.pm2) launcher = 'pm2';
    else if (ctx.platform === 'darwin') launcher = 'launchd';
    else
      throw refuse(
        ctx.platform === 'linux'
          ? 'no PM2 on this host, and a systemd --user unit is owed, not built: install PM2 (npm install -g pm2, then pm2 startup) and run install again'
          : `no PM2 on this host, and no service launcher is built for ${ctx.platform} yet (owed): install PM2 and run install again`,
      );
  }
  if (!['pm2', 'launchd'].includes(launcher)) throw usage(`--launcher ${launcher}: pm2 or launchd`);
  if (rec?.status === 'installed' && rec.launcher !== launcher)
    throw refuse(`this garden's service is run by ${rec.launcher}; uninstall it first to move it to ${launcher}`);
  if (launcher === 'pm2' && !ctx.bins.pm2) throw refuse('--launcher pm2, and pm2 is not found (BONSAI_PM2, or PATH)');
  if (launcher === 'pm2' && pm2SocketProblem(ctx)) throw refuse(pm2SocketProblem(ctx));
  if (launcher === 'launchd' && ctx.platform !== 'darwin') throw refuse('a LaunchAgent is macOS only');
  if (launcher === 'launchd' && (!ctx.bins.launchctl || ctx.uid === null))
    throw refuse('launchctl is not found on this host');
  if (!ctx.bins.uv)
    throw refuse(
      'no uv on this host (https://docs.astral.sh/uv/), so the server cannot run; install uv, or name it with BONSAI_UV',
    );
  if (!ctx.bins.claude)
    throw refuse(
      "no claude CLI on this host, so the service cannot be registered at user level; install Claude Code's CLI, or name it with BONSAI_CLAUDE",
    );
  const app = pm2Name(name);
  const label = `${LABEL_PREFIX}${name}`;
  if (launcher === 'pm2' && !(rec?.status === 'installed' && rec.app === app)) {
    const a = pm2App(ctx, app, { spawn: true });
    if (a?.error) throw halt(a.error);
    if (a)
      throw refuse(
        `PM2 already runs an app named ${app} that this garden's record does not name; it is not this tool's to replace`,
      );
  }
  if (
    launcher === 'launchd' &&
    !(rec?.status === 'installed' && rec.label === label) &&
    fs.existsSync(plistFile(ctx, label))
  )
    throw refuse(
      `${plistFile(ctx, label)} exists and this garden's record does not name it; it is not this tool's to replace`,
    );
  const port = await choosePort(ctx, facts, rec, others);
  // a user-level entry under this name is this garden's only when it points where this garden's service is or was
  const reg = registration(ctx, name);
  const url = `http://127.0.0.1:${port}/`;
  if (reg && !(reg.type === 'http' && (reg.url === url || (rec && reg.url === rec.url))))
    throw refuse(
      `a user-level MCP named ${name} is already registered (${reg.type} ${reg.url || reg.command}) and is not this garden's service; ` +
        'name this one with --name, or remove that one yourself (claude mcp remove --scope user <name>)',
    );
  const wait = ctx.f.wait !== undefined ? Number(ctx.f.wait) : 90;
  if (!Number.isFinite(wait) || wait < 0) throw usage('--wait <seconds>');

  // the service
  const def = { ...definition(ctx, facts, name, port, launcher), garden: facts.garden, app, label };
  const moved = rec?.status === 'installed' && rec.port !== port;
  if (moved) ctx.say(`port: ${rec.port} -> ${port}`);
  const next = {
    format: RECORD_FORMAT,
    status: 'installed',
    garden: facts.garden,
    'garden-name': facts.gardenName,
    pot: facts.pot,
    name,
    port,
    url: def.url,
    launcher,
    ...(launcher === 'pm2' ? { app } : { label }),
    rt: facts.rt,
    'rt-version': `${facts.rtName} ${facts.rtVersion}`,
    hash: def.hash,
    retired: rec?.retired || null,
    skills: rec?.skills || [],
    installed: rec?.status === 'installed' ? rec.installed : new Date().toISOString(),
    updated: new Date().toISOString(),
  };
  const changed = launcher === 'pm2' ? pm2Install(ctx, def, rec) : launchdInstall(ctx, def, rec);
  if (launcher === 'launchd') next['plist-sha256'] = sha256(fs.readFileSync(plistFile(ctx, label)));
  writeRecord(ctx.home, next); // the record follows the launcher at once: a later halt leaves a record that uninstall can read
  // the pot's per-session entry, retired so no session sees two garden MCPs
  const retired = retireShadow(ctx, def);
  if (retired) {
    next.retired = retired;
    // recorded at once, before anything else can halt: a halt in registration left the pot's .mcp.json retired and the
    // record unaware, so uninstall could not put it back (the push round of 2026-10-19, F-HS1)
    writeRecord(ctx.home, next);
  }
  // registration
  register(ctx, def);
  // skills
  const prior = (rec?.skills || []).filter((s) => s.made).map((s) => s.slug);
  const { linked, steps } = linkSkills(ctx, prior);
  next.skills = linked;
  writeRecord(ctx.home, next);
  // does it answer?
  const ans = changed || moved ? await waitAnswer(def.url, name, wait) : await answering(def.url, name);
  if (!ans.ok)
    throw halt(
      `installed and registered, but ${def.url} does not answer as ${name} (${ans.why}); read ${path.join(serviceDir(ctx.home, facts.garden), 'mcp.err.log')}, then probe`,
    );
  ctx.say(`answers: ${def.url} as ${ans.info.name}`);
  ctx.say(
    `next: in a new session opened in ${facts.pot}, /mcp lists ${name} · connected` +
      (linked.length
        ? `, and ${linked.map((l) => `/${l.slug}`).join(', ')} wake${linked.length === 1 ? 's' : ''} its kin`
        : '') +
      `; then: node ${path.join(facts.rt, 'tools', 'rt', 'host-service.mjs')} probe --pot ${facts.pot}`,
  );
  if (steps.length) ctx.say(`open: ${steps.length} step(s) above are yours (STEP)`);
}

async function cmdLinkSkills(ctx) {
  const facts = potFacts(ctx);
  const rec = readRecord(ctx.home, facts.garden);
  const prior = (rec?.skills || []).filter((s) => s.made).map((s) => s.slug);
  const { linked } = linkSkills(ctx, prior);
  if (rec) writeRecord(ctx.home, { ...rec, skills: linked, updated: new Date().toISOString() });
}

async function cmdUninstall(ctx) {
  const facts = potFacts(ctx);
  const rec = readRecord(ctx.home, facts.garden);
  if (rec?.status !== 'installed')
    throw refuse(`no installed host service for garden ${facts.garden} on this host; nothing to remove`);
  if (rec.launcher === 'pm2') {
    if (!ctx.bins.pm2) throw refuse('this service is run by PM2, and pm2 is not found (BONSAI_PM2, or PATH)');
    pm2Remove(ctx, rec);
  } else {
    if (!ctx.bins.launchctl) throw refuse('this service is a LaunchAgent, and launchctl is not found');
    launchdRemove(ctx, rec);
  }
  if (ctx.bins.claude) unregister(ctx, rec);
  else ctx.say(`registered: no claude CLI; remove it yourself: claude mcp remove --scope user ${rec.name}`);
  unlinkSkills(ctx, rec);
  restoreShadow(ctx, rec);
  writeRecord(ctx.home, { ...rec, status: 'uninstalled', skills: [], updated: new Date().toISOString() });
  ctx.say(
    `uninstalled: ${rec.name}; the record is kept at ${recordFile(ctx.home, facts.garden)}, marked uninstalled (its port comes back on a new install)`,
  );
}

/** probe: connected, then each kin through the service. Exit 0 ok · 1 not connected · 2 answered STOP. */
async function cmdProbe(ctx) {
  const facts = potFacts(ctx);
  const { rows, rec } = await connectionRows(ctx, facts);
  const show = (s, w, d) => ctx.say(`${s.padEnd(4)}  ${w.padEnd(15)} ${d}`);
  for (const [s, w, d] of rows) show(s, w, d);
  const not = rows.filter((r) => r[0] === 'FAIL');
  if (not.length) {
    const kinds = [...new Set(not.map((r) => r[3]))];
    ctx.say(
      `\nprobe: NOT CONNECTED — ${kinds.join(', ')}. No kin was asked: a STOP cannot be told from silence until the service answers.`,
    );
    return 1;
  }
  const all = kinWithBodies(facts.pot).map((k) => k.slug);
  const kin = ctx.f.kin ? ctx.f.kin.split(',').map((s) => s.trim()) : all;
  for (const k of kin) if (!SLUG_RE.test(k)) throw usage(`--kin ${k} is not a slug`);
  if (!kin.length) {
    ctx.say('\nprobe: connected; no kin with a body in the registry yet, so no integrity line was asked for');
    return 0;
  }
  const stops = [];
  const silent = [];
  for (const slug of kin) {
    let line = null;
    let served = null;
    try {
      const s = await mcpSession(rec.url);
      const text = await s.tool('bonsai_session_start', { agent_slug: slug });
      const tok = /^session_token: (\S+)$/m.exec(text);
      line = (/^integrity: (.*)$/m.exec(text) || [])[1] || null;
      if (!line && /not a recognized agent/.test(text)) line = `STOP — ${slug} is not a kin in this garden's registry`;
      try {
        const g = await s.tool(
          'bonsai_read_file',
          { path: 'garden.json', ...(tok ? { session_token: tok[1] } : {}) },
          30000,
        );
        const id = /"garden"\s*:\s*"([0-9A-HJKMNP-TV-Z]{26})"/.exec(g);
        served = id ? id[1] : null;
      } catch {
        /* said below as unconfirmed */
      }
      await s.close();
    } catch (e) {
      show('FAIL', `kin ${slug}`, `the service did not answer for ${slug}: ${e.message}`);
      silent.push(slug);
      continue;
    }
    if (served && served !== facts.garden) {
      show('FAIL', `kin ${slug}`, `the service on ${rec.url} serves garden ${served}, not this pot's ${facts.garden}`);
      stops.push([slug, 'another garden answered']);
      continue;
    }
    if (!served)
      show(
        'note',
        `kin ${slug}`,
        `the served garden.json could not be read back through the service; its name (${rec.name}) is the identity checked`,
      );
    if (line && /^ok\b/.test(line)) show('ok', `kin ${slug}`, `integrity: ${line}`);
    else {
      const l = line || '(no integrity line in its answer)';
      show(
        'STOP',
        `kin ${slug}`,
        `integrity: ${l}${/pot-untrusted/.test(l) ? ' — not pinned: the gardener pins it first (integrity_line.py pin)' : ''}`,
      );
      stops.push([slug, l]);
    }
  }
  if (silent.length) {
    ctx.say(`\nprobe: NOT CONNECTED — no answer: the service stopped answering for ${silent.join(', ')}`);
    return 1;
  }
  if (stops.length) {
    ctx.say(`\nprobe: ANSWERED STOP — connected; ${stops.map(([s, l]) => `${s}: ${l}`).join(' · ')}`);
    return 2;
  }
  ctx.say(`\nprobe: connected; ${kin.length} kin answered integrity ok through ${rec.name}`);
  return 0;
}

export async function main(argv, deps = {}) {
  let ctx;
  try {
    ctx = context(argv, deps);
    switch (ctx.verb) {
      case 'install':
        await cmdInstall(ctx);
        return 0;
      case 'link-skills':
        await cmdLinkSkills(ctx);
        return 0;
      case 'uninstall':
        await cmdUninstall(ctx);
        return 0;
      case 'probe':
        return await cmdProbe(ctx);
      default:
        throw usage(`unknown verb ${ctx.verb}\n\n${USAGE}`);
    }
  } catch (e) {
    if (!(e instanceof Exit)) throw e;
    const say = deps.say || ((s) => console.log(s));
    if (e.exit === 0) say(e.message);
    else say(`${e.exit === 2 ? 'refused' : e.exit === 3 ? 'HALT' : 'usage'}: ${e.message}`);
    return e.exit;
  }
}

/** For rt's doctor: the context this tool would build for a pot, without a verb. */
export function doctorContext(pot, deps = {}) {
  return context(['probe', '--pot', pot], deps);
}

const invoked = (() => {
  try {
    return !!process.argv[1] && import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
})();
if (invoked) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (e) => {
      console.error(`HALT: ${e.stack || e.message}`);
      process.exit(3);
    },
  );
}
