"use strict";

/**
 * Desktop shell for the algo trading platform. Deliberately does NOT
 * reimplement backend process management -- it reuses
 * scripts/start_platform.ps1 (already-proven: Docker-backed MySQL/Redis,
 * Django/Daphne, both Celery workers, Celery Beat, the live Angel One
 * feed, each tracked with its own PID/log file) rather than duplicating
 * that orchestration in Node. This file's own job is just three things:
 *   1. Make sure that backend is running (launch it if not).
 *   2. Serve the already-built frontend/dist/ over a real local HTTP
 *      origin (not file://, so react-router-dom's BrowserRouter keeps
 *      working unmodified -- see config/backendOrigin.js's docstring).
 *   3. Open a window pointed at that local origin.
 *
 * getBackendOrigin() in config/backendOrigin.js defaults Electron to
 * http://127.0.0.1:8000 -- the backend this file launches is always on
 * the SAME machine, so no Server Settings step is needed on desktop
 * unless the user explicitly wants to point at a different backend
 * (e.g. the cloud-hosted one) via that page.
 */

const { app, BrowserWindow } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const DIST_DIR = path.join(__dirname, "..", "dist");
const START_SCRIPT = path.join(REPO_ROOT, "scripts", "start_platform.ps1");
const BACKEND_HEALTH_URL = "http://127.0.0.1:8000/api/monitoring/health/";
const STATIC_PORT = 4300;
// Verified live: a cold start (Docker container start + venv checks +
// `manage.py check`/`migrate`, which import every app including
// torch/transformers, then 5 separate process launches) took a little
// over 60s end to end -- the original value here. That's not a fixed
// worst case, so this leaves real headroom rather than tightening it
// back down to what one measurement happened to need.
const BACKEND_STARTUP_TIMEOUT_MS = 120000;
const HEALTH_POLL_INTERVAL_MS = 2000;
const LAUNCH_LOG_PATH = path.join(REPO_ROOT, "logs", "electron_backend_launch.log");

const MIME_TYPES = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function checkBackendHealth() {
  return new Promise((resolve) => {
    const req = http.get(BACKEND_HEALTH_URL, { timeout: 3000 }, (res) => {
      res.resume();
      resolve(res.statusCode < 500);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

/**
 * Windows-only, matching every other launcher already in this repo
 * (start_platform.ps1's own header comment lists Windows-specific
 * assumptions throughout). On macOS/Linux this is a documented no-op --
 * see docs/desktop_and_android_apps.md -- the window still opens, and
 * whoever's running it must have started the backend themselves first.
 * -SkipFrontend because this file serves the pre-built dist/ itself
 * (startStaticServer below), not `npm run dev`.
 */
function launchBackend() {
  if (process.platform !== "win32") {
    console.warn(
      "[electron] Automatic backend launch is Windows-only -- start it manually " +
        "(see docs/desktop_and_android_apps.md) before using this app."
    );
    return;
  }
  // Verified live: with stdio "ignore" here, a start_platform.ps1 failure
  // (Docker Desktop not running, a missing backend/.env, etc.) was
  // completely invisible -- the window just opened with no backend and
  // no clue why. Redirecting to a log file is what makes that
  // diagnosable instead of a silent 120s wait for nothing.
  fs.mkdirSync(path.dirname(LAUNCH_LOG_PATH), { recursive: true });
  const launchLog = fs.openSync(LAUNCH_LOG_PATH, "a");
  spawn("powershell.exe", ["-ExecutionPolicy", "Bypass", "-File", START_SCRIPT, "-SkipFrontend"], {
    cwd: REPO_ROOT,
    detached: true,
    stdio: ["ignore", launchLog, launchLog],
    windowsHide: true,
  }).unref();
}

async function ensureBackendRunning() {
  if (await checkBackendHealth()) {
    console.log("[electron] Backend already reachable at 127.0.0.1:8000.");
    return;
  }
  console.log("[electron] Backend not reachable -- launching scripts/start_platform.ps1...");
  launchBackend();

  const deadline = Date.now() + BACKEND_STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, HEALTH_POLL_INTERVAL_MS));
    if (await checkBackendHealth()) {
      console.log("[electron] Backend is up.");
      return;
    }
  }
  console.warn(
    `[electron] Backend still not reachable after ${BACKEND_STARTUP_TIMEOUT_MS}ms -- ` +
      `opening the window anyway; check ${LAUNCH_LOG_PATH} and logs\\*.err.log under the repo root.`
  );
}

/**
 * A real HTTP origin (not file://) is what keeps BrowserRouter working
 * with zero React Router changes. The catch-all fallback to index.html
 * is this server's equivalent of a production web server's history-API
 * rewrite -- without it, reloading on a deep route like /paper-trading
 * would 404 instead of re-rendering the app.
 */
function startStaticServer() {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const requestedPath = decodeURIComponent(req.url.split("?")[0]);
      let filePath = path.join(DIST_DIR, requestedPath);
      if (!filePath.startsWith(DIST_DIR)) {
        filePath = path.join(DIST_DIR, "index.html"); // path-traversal guard
      }

      fs.readFile(filePath, (err, data) => {
        if (err) {
          fs.readFile(path.join(DIST_DIR, "index.html"), (fallbackErr, fallbackData) => {
            if (fallbackErr) {
              res.writeHead(500, { "Content-Type": "text/plain" });
              res.end("frontend/dist not found -- run `npm run build` first.");
              return;
            }
            res.writeHead(200, { "Content-Type": "text/html" });
            res.end(fallbackData);
          });
          return;
        }
        const contentType = MIME_TYPES[path.extname(filePath)] || "application/octet-stream";
        res.writeHead(200, { "Content-Type": contentType });
        res.end(data);
      });
    });
    server.on("error", reject);
    server.listen(STATIC_PORT, "127.0.0.1", resolve);
  });
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    title: "Algo Trading Platform",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadURL(`http://127.0.0.1:${STATIC_PORT}/`);
}

app.whenReady().then(async () => {
  await ensureBackendRunning();
  await startStaticServer();
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// Deliberately does NOT stop the backend on window/app close -- it's
// the same PID-tracked set of background processes start_platform.ps1
// always leaves running independent of any dashboard window (Celery
// Beat and the live feed are meant to keep working with no window
// open). Run scripts\stop_platform.ps1 to actually stop them.
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
