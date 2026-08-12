import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev-server proxy so the React app can call /api/... without CORS
// headaches during local development; in production this would instead
// be handled by whatever reverse proxy sits in front of Django.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
});
