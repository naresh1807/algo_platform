"use strict";

const { contextBridge } = require("electron");

// This is the ONLY thing config/backendOrigin.js's isElectron() checks
// for (window.electronAPI?.isElectron) -- contextIsolation is on in
// main.js, so this bridge is the one sanctioned way anything in the
// renderer can tell it's running inside Electron at all.
contextBridge.exposeInMainWorld("electronAPI", {
  isElectron: true,
});
