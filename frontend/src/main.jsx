import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App.jsx";
import "./index.css";
// Side-effect import: sets <html data-theme="..."> from the saved/OS
// preference immediately, before any component (including Login, which
// doesn't render TopBar) paints -- avoids a flash of the wrong theme.
import "./store/themeStore.js";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
