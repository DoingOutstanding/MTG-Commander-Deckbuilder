// Stage the Python server for electron-builder. Runs PyInstaller on
// app.py, copies the resulting one-folder bundle into ../dist/server,
// and copies the read-only data files (cards.jsonl, oracle JSON,
// xmage_cards.txt, default xmage_excluded.txt, default
// pair_cache.pkl.gz) alongside it.
//
// The result lives at  ../dist/server/{deckbuilder-server, _internal/, ...}
// which electron-builder picks up via the `extraResources` config and
// drops into  process.resourcesPath/server/  inside the packaged app.
//
// Usage:  node scripts/build-server.js
// (run from the electron/ directory)

'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ELECTRON_DIR = path.resolve(__dirname, '..');
const DECKBUILDER_DIR = path.resolve(ELECTRON_DIR, '..');
const DIST_DIR = path.resolve(DECKBUILDER_DIR, 'dist');
const SERVER_OUT = path.resolve(DIST_DIR, 'server');

function log(msg) { console.log(`[build-server] ${msg}`); }
function die(msg) { console.error(`[build-server] ERROR: ${msg}`); process.exit(1); }

function rmrf(p) {
  if (fs.existsSync(p)) {
    fs.rmSync(p, { recursive: true, force: true });
  }
}

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function copyIfExists(src, dst) {
  if (!fs.existsSync(src)) {
    log(`  skip ${path.basename(src)} (not present)`);
    return;
  }
  ensureDir(path.dirname(dst));
  fs.copyFileSync(src, dst);
  log(`  copied ${path.basename(src)} -> ${path.relative(SERVER_OUT, dst)}`);
}

function pickPython() {
  const candidates = process.platform === 'win32'
    ? ['py', 'python', 'python3']
    : ['python3', 'python'];
  for (const c of candidates) {
    const r = spawnSync(c, ['--version'], { encoding: 'utf-8' });
    if (r.status === 0) return c;
  }
  die('No Python interpreter found on PATH. Install Python 3.10+ and try again.');
}

function runPyInstaller(python) {
  log('Checking PyInstaller availability...');
  const check = spawnSync(python, ['-m', 'PyInstaller', '--version'], { encoding: 'utf-8' });
  if (check.status !== 0) {
    log('PyInstaller not found. Installing into the active Python environment...');
    const install = spawnSync(python, ['-m', 'pip', 'install', '--quiet', 'pyinstaller', 'flask'], {
      stdio: 'inherit',
    });
    if (install.status !== 0) die('pip install pyinstaller flask failed.');
  }

  // PyInstaller arguments. We use one-folder mode (faster startup than
  // one-file). interactions.py and profile.py are picked up automatically
  // via PyInstaller's import analysis since app.py imports them; the
  // data files (cards.jsonl, oracle JSON, etc.) are *not* embedded in
  // the binary — they're staged alongside it by stageServer() so the
  // Python `ROOT = Path(__file__).parent` resolution finds them in the
  // bundle's executable directory at runtime.
  const args = [
    '-m', 'PyInstaller',
    '--name=deckbuilder-server',
    '--noconfirm',
    '--clean',
    '--onedir',
    '--console',
    '--paths=' + DECKBUILDER_DIR,  // ensure interactions/profile resolve
    `--distpath=${path.join(DIST_DIR, 'pyinstaller')}`,
    `--workpath=${path.join(DIST_DIR, 'pyinstaller-build')}`,
    `--specpath=${path.join(DIST_DIR, 'pyinstaller-spec')}`,
    'app.py',
  ];

  log('Running PyInstaller... (first time can take a few minutes)');
  const r = spawnSync(python, args, {
    cwd: DECKBUILDER_DIR,
    stdio: 'inherit',
  });
  if (r.status !== 0) die(`PyInstaller failed (exit ${r.status}).`);
}

function stageServer() {
  log('Staging server directory...');
  rmrf(SERVER_OUT);
  ensureDir(SERVER_OUT);

  const piOut = path.join(DIST_DIR, 'pyinstaller', 'deckbuilder-server');
  if (!fs.existsSync(piOut)) {
    die(`PyInstaller output missing at ${piOut}`);
  }

  // Copy the entire one-folder PyInstaller bundle into SERVER_OUT.
  fs.cpSync(piOut, SERVER_OUT, { recursive: true });
  log(`  copied PyInstaller bundle (${countFiles(SERVER_OUT)} files)`);

  // Copy data files. The Python app looks alongside its executable for
  // these (ROOT = directory of app.py, which PyInstaller maps to the
  // executable directory at runtime).
  const dataFiles = [
    'cards.jsonl',
    'xmage_cards.txt',
    'xmage_excluded.txt',
    'pair_cache.pkl.gz',
  ];
  for (const name of dataFiles) {
    copyIfExists(path.join(DECKBUILDER_DIR, name), path.join(SERVER_OUT, name));
  }

  // Copy the most recent oracle-cards-*.json (Scryfall data file) — the
  // Python profiler doesn't need this at runtime once cards.jsonl is
  // built, but ship it so users can rebuild profiles offline if they
  // tweak the regex rules.
  const entries = fs.readdirSync(DECKBUILDER_DIR);
  const oracleFiles = entries
    .filter(n => n.startsWith('oracle-cards-') && n.endsWith('.json'))
    .sort();
  if (oracleFiles.length > 0) {
    const latest = oracleFiles[oracleFiles.length - 1];
    copyIfExists(path.join(DECKBUILDER_DIR, latest), path.join(SERVER_OUT, latest));
  }
}

function countFiles(dir) {
  let n = 0;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    n += e.isDirectory() ? countFiles(path.join(dir, e.name)) : 1;
  }
  return n;
}

function main() {
  log(`deckbuilder dir: ${DECKBUILDER_DIR}`);
  log(`output dir:      ${SERVER_OUT}`);

  if (!fs.existsSync(path.join(DECKBUILDER_DIR, 'app.py'))) {
    die(`app.py not found at ${DECKBUILDER_DIR}`);
  }

  const python = pickPython();
  log(`using python: ${python}`);
  runPyInstaller(python);
  stageServer();

  log('Done. Server ready for electron-builder.');
}

main();
