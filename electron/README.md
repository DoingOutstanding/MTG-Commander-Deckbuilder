# MTG Commander Deckbuilder — Electron desktop bundle

This directory wraps the Flask deckbuilder as a self-contained desktop
application using Electron. End-users get a normal `.exe` / `.dmg` /
`.AppImage` they double-click — no Python install, no terminal, no
`.bat` file warnings. Internally Electron spawns the Flask server as a
child process and points a `BrowserWindow` at it.

## Building the installer

You'll need **Node 20+**, **Python 3.10+** (any version on `PATH` is
fine — it's only used at build time to run PyInstaller; users don't
need it), and the deckbuilder data files (`cards.jsonl`, the Scryfall
oracle JSON, the pre-warmed `pair_cache.pkl.gz`).

From this directory:

```bash
npm install
npm run dist        # builds for the current platform
# or:
npm run dist:win    # Windows NSIS installer + portable
npm run dist:mac    # macOS DMG (signed only if you provide certs)
npm run dist:linux  # Linux AppImage
```

`npm run dist` does two things in sequence:

1. **`build:server`** — runs `scripts/build-server.js`, which uses
   PyInstaller to bundle `app.py` + all its imports into a one-folder
   binary at `../dist/server/`. It also stages the data files
   (`cards.jsonl`, `xmage_cards.txt`, `xmage_excluded.txt`, the
   pre-warmed `pair_cache.pkl.gz`, and the most recent
   `oracle-cards-*.json`) into the same directory.

2. **`electron-builder`** — packages `main.js` + `package.json` into
   the platform-native installer, including the staged server folder
   as `extraResources`. Final installer goes to `../dist/electron/`.

## Running in development

For day-to-day iteration without rebuilding the binary every time:

```bash
npm install
npm start
```

This launches Electron with `app.isPackaged === false`. `main.js`
spawns `python3 ../app.py` (or `python` on Windows) directly, so any
edits you make to the Python source are picked up on the next
**File → Reload** without rebuilding. You need Python and Flask
installed locally for this mode (only the dev box, not end-users).

## Where state goes

Per-user state — the persistent `pair_cache.pkl.gz` and any local
`xmage_excluded.txt` overrides — lives in the OS-specific user-data
directory:

- **Windows**: `%APPDATA%\MTG Commander Deckbuilder\`
- **macOS**: `~/Library/Application Support/MTG Commander Deckbuilder/`
- **Linux**: `~/.config/MTG Commander Deckbuilder/`

The bundle ships a read-only seed of `pair_cache.pkl.gz` that the
Python app loads on first launch if the user-data copy doesn't exist.
After that the user-data file takes precedence and accumulates new
entries as the user explores commanders.

The **Help → Open user data folder** menu item in the desktop app
opens that directory directly.

## Bundle size

Approximate distributable sizes per platform (compressed installer):

| Platform | Installer | Unpacked |
|----------|-----------|----------|
| Windows  | ~140 MB   | ~330 MB  |
| macOS    | ~150 MB   | ~340 MB  |
| Linux    | ~140 MB   | ~330 MB  |

The biggest contributors are: Electron runtime (~140 MB), Python
interpreter via PyInstaller (~30 MB), `cards.jsonl` (~70 MB), and the
pre-warmed pair cache (~6–10 MB).

## Re-warming the pair cache before shipping

Run `python prewarm_cache.py` in the parent directory before building
the bundle if you want users to start with cached scores for the most
popular commanders. The script auto-builds 20 commanders and writes
`pair_cache.pkl.gz`, which the build script then copies into
`dist/server/`.
