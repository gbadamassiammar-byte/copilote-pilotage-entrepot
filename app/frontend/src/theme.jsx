// Thème clair / sombre.
// * Les couleurs de l'interface sont des variables CSS (styles.css) : changer
//   l'attribut data-theme sur <html> suffit à tout repeindre.
// * Les graphiques (Recharts) dessinent en SVG avec des couleurs passées en
//   JavaScript : ils lisent le thème courant via useTheme().
// * Le choix est retenu dans le navigateur (localStorage). Sans choix enregistré,
//   on suit le réglage du système (clair ou sombre).
import { createContext, useCallback, useContext, useEffect, useState } from "react";

const CLE = "copilote_theme";
const Ctx = createContext({ theme: "clair", basculer: () => {} });
export const useTheme = () => useContext(Ctx);

function lireChoix() {
  try { return localStorage.getItem(CLE); } catch { return null; }
}
function themeSysteme() {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "sombre" : "clair";
}

export function ThemeProvider({ children }) {
  const [theme, setTheme] = useState(() => lireChoix() || themeSysteme());

  useEffect(() => {
    document.documentElement.dataset.theme = theme === "sombre" ? "dark" : "light";
  }, [theme]);

  // Tant que l'utilisateur n'a rien choisi, on suit les changements du système.
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return undefined;
    const suivre = (e) => { if (!lireChoix()) setTheme(e.matches ? "sombre" : "clair"); };
    mq.addEventListener("change", suivre);
    return () => mq.removeEventListener("change", suivre);
  }, []);

  const basculer = useCallback(() => {
    setTheme((t) => {
      const nouveau = t === "sombre" ? "clair" : "sombre";
      try { localStorage.setItem(CLE, nouveau); } catch { /* navigation privée */ }
      return nouveau;
    });
  }, []);

  return <Ctx.Provider value={{ theme, basculer }}>{children}</Ctx.Provider>;
}

// Couleurs des graphiques pour chaque thème (contraste vérifié sur le fond des cartes).
const GRAPHES = {
  clair: {
    axe: "#64748b", grille: "#f1f5f9", infobulle: { fond: "#ffffff", bord: "#e2e8f0", texte: "#0f172a" },
    tons: { critique: "#dc2626", alerte: "#ea580c", info: "#2563eb", ok: "#0f766e" },
    palette: ["#0f766e", "#94a3b8", "#2563eb", "#ea580c", "#7c3aed", "#db2777", "#65a30d"],
  },
  sombre: {
    axe: "#94a3b8", grille: "#1e293b", infobulle: { fond: "#0f172a", bord: "#334155", texte: "#e2e8f0" },
    tons: { critique: "#f87171", alerte: "#fb923c", info: "#60a5fa", ok: "#2dd4bf" },
    palette: ["#2dd4bf", "#94a3b8", "#60a5fa", "#fb923c", "#a78bfa", "#f472b6", "#a3e635"],
  },
};
export const couleursGraphes = (theme) => GRAPHES[theme] || GRAPHES.clair;
