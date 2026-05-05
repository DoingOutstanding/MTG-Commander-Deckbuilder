// Electron main process — spawns the Flask server as a child, waits for
// it to start listening, then opens a BrowserWindow pointed at it.
//
// Two execution modes:
//
//   * Development  (`npm start` in this directory): finds a system
//     `python3` / `python` on PATH and runs `../app.py`. The host
//     machine needs Python 3.10+ and Flask installed.
//
//   * Packaged     (`npm run dist`): runs the PyInstaller-bundled
//     `deckbuilder-server` binary that electron-builder copied into
//     `process.resourcesPath/server/`. End-users don't need Python.
//
// Either way the server gets these env vars:
//
//   DECKBUILDER_HOST=127.0.0.1
//   DECKBUILDER_PORT=<picked free port>
//   DECKBUILDER_DATA_DIR=<app.getPath('userData')>
//
// so the persistent pair-cache and personal xmage_excluded.txt go in
// the per-user data directory (e.g. %APPDATA%/MTGCommanderDeckbuilder
// on Windows, ~/Library/Application Support/MTGCommanderDeckbuilder on
// macOS) instead of the read-only bundle.

const { app, BrowserWindow, dialog, shell, Menu } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');
const net = require('net');

const HOST = '127.0.0.1';
let serverProcess = null;
let mainWindow = null;
let serverPort = null;

// Ask the OS for a free TCP port. We don't hard-code 5000 because a
// local dev server might already own it.
function pickFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, HOST, () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

function getServerCommand() {
  if (app.isPackaged) {
    // electron-builder copies the PyInstaller output into the resources
    // directory. The binary is named `deckbuilder-server` (or .exe on
    // Windows) and lives in `resources/server/`.
    const serverDir = path.join(process.resourcesPath, 'server');
    const exeName = process.platform === 'win32'
      ? 'deckbuilder-server.exe'
      : 'deckbuilder-server';
    const exePath = path.join(serverDir, exeName);
    return { cmd: exePath, args: [], cwd: serverDir };
  } else {
    // Dev mode — run the source app.py through whatever Python is on PATH.
    const scriptPath = path.resolve(__dirname, '..', 'app.py');
    const cwd = path.dirname(scriptPath);
    const cmd = process.platform === 'win32' ? 'python' : 'python3';
    return { cmd, args: [scriptPath], cwd };
  }
}

function startServer() {
  return new Promise(async (resolve, reject) => {
    serverPort = await pickFreePort();
    const userDataDir = app.getPath('userData');
    fs.mkdirSync(userDataDir, { recursive: true });

    const { cmd, args, cwd } = getServerCommand();
    if (!fs.existsSync(cmd) && app.isPackaged) {
      reject(new Error(`Bundled server binary not found at ${cmd}.`));
      return;
    }

    const env = Object.assign({}, process.env, {
      DECKBUILDER_HOST: HOST,
      DECKBUILDER_PORT: String(serverPort),
      DECKBUILDER_DATA_DIR: userDataDir,
      // Force unbuffered output so we see stdout/stderr live
      PYTHONUNBUFFERED: '1',
    });

    serverProcess = spawn(cmd, args, { cwd, env, stdio: 'pipe' });

    serverProcess.stdout.on('data', (d) => {
      process.stdout.write(`[server] ${d.toString()}`);
    });
    serverProcess.stderr.on('data', (d) => {
      process.stderr.write(`[server] ${d.toString()}`);
    });
    serverProcess.on('error', reject);
    serverProcess.on('exit', (code, signal) => {
      console.log(`[server] exited (code=${code}, signal=${signal})`);
      if (mainWindow && !mainWindow.isDestroyed()) {
        // If the server crashed while the window was open, close the app.
        mainWindow.close();
      }
    });

    resolve();
  });
}

// Poll the server's root URL until it responds (or we give up after ~60s).
function waitForServer(timeoutMs = 60_000, intervalMs = 250) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryConnect = () => {
      const req = http.get(`http://${HOST}:${serverPort}/`, (res) => {
        // Any HTTP response means the server is up.
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (Date.now() >= deadline) {
          reject(new Error('Server did not become reachable within 60s.'));
        } else {
          setTimeout(tryConnect, intervalMs);
        }
      });
      req.setTimeout(intervalMs * 4, () => req.destroy());
    };
    tryConnect();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 950,
    minWidth: 900,
    minHeight: 600,
    title: 'MTG Commander Deckbuilder',
    backgroundColor: '#1a1a1f',
    autoHideMenuBar: process.platform !== 'darwin',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.loadURL(`http://${HOST}:${serverPort}/`);

  // Open external links (e.g. Scryfall card pages) in the system browser
  // instead of inside the Electron window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(`http://${HOST}:${serverPort}/`)) {
      return { action: 'allow' };
    }
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function buildMenu() {
  const isMac = process.platform === 'darwin';
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        { label: 'Reload', accelerator: 'CmdOrCtrl+R',
          click: () => mainWindow && mainWindow.reload() },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        { role: 'toggleDevTools' },
      ],
    },
    {
      role: 'help',
      submenu: [
        {
          label: 'Open user data folder',
          click: () => shell.showItemInFolder(app.getPath('userData')),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(async () => {
  buildMenu();
  try {
    await startServer();
    await waitForServer();
    createWindow();
  } catch (err) {
    dialog.showErrorBox(
      'MTG Commander Deckbuilder failed to start',
      String(err && err.message || err)
    );
    app.quit();
  }
});

// macOS: keep app alive when last window closes (typical for Mac apps).
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0 && serverPort) {
    createWindow();
  }
});

// Make absolutely sure the server doesn't outlive the GUI.
function killServer() {
  if (serverProcess && !serverProcess.killed) {
    try { serverProcess.kill(); } catch (_) { /* ignore */ }
  }
}
app.on('before-quit', killServer);
app.on('quit', killServer);
process.on('exit', killServer);
process.on('SIGINT', () => { killServer(); process.exit(0); });
process.on('SIGTERM', () => { killServer(); process.exit(0); });
