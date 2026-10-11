import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En développement, Vite sert l'interface sur http://localhost:5173 et relaie
// les appels /api vers le serveur Flask (port 8080) : pas de problème de CORS.
// "npm run build" produit le dossier dist/, que Flask sert en production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8080" },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 900 },
});
