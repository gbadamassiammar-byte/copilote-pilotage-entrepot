import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, enregistrerCode } from "./api.js";
import { TraceProvider } from "./components/Trace.jsx";
import { ThemeProvider, useTheme } from "./theme.jsx";
import Actions from "./pages/Actions.jsx";
import Bientot from "./pages/Bientot.jsx";
import Copilote from "./pages/Copilote.jsx";
import Fiabilite from "./pages/Fiabilite.jsx";
import Kpi from "./pages/Kpi.jsx";

const NAV = [
  { to: "/copilote", label: "Copilote", icone: "◆" },
  { to: "/fiabilite", label: "Fiabilité", icone: "✓" },
  { to: "/kpi", label: "KPI", icone: "▤" },
  { to: "/actions", label: "Dérives et actions", icone: "↯" },
  { to: "/prevision", label: "Prévision", icone: "◷", bientot: true },
];

export default function App() {
  return <ThemeProvider><Application /></ThemeProvider>;
}

function BasculeTheme() {
  const { theme, basculer } = useTheme();
  const sombre = theme === "sombre";
  return (
    <button className="bascule-theme" onClick={basculer} aria-pressed={sombre}
            title={sombre ? "Passer en mode clair" : "Passer en mode sombre"}>
      <span className="piste" />{sombre ? "Mode sombre" : "Mode clair"}
    </button>
  );
}

function Application() {
  const [etat, setEtat] = useState(null);
  const [erreurEtat, setErreurEtat] = useState(null);
  const [demandeCode, setDemandeCode] = useState(false);
  const [code, setCode] = useState("");

  useEffect(() => {
    api.etat().then(setEtat).catch((e) => setErreurEtat(e.message));
    const ouvrir = () => setDemandeCode(true);
    window.addEventListener("code-acces-requis", ouvrir);
    return () => window.removeEventListener("code-acces-requis", ouvrir);
  }, []);

  return (
    <TraceProvider>
      <div className="app">
        <nav className="barre">
          <div className="marque">
            <span className="logo">⌂</span>
            <div><strong>Copilote</strong><small>Plateforme Loire Santé</small></div>
          </div>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} className={({ isActive }) => `lien ${isActive ? "actif" : ""} ${n.bientot ? "bientot" : ""}`}>
              <span className="icone">{n.icone}</span>{n.label}{n.bientot && <em>bientôt</em>}
            </NavLink>
          ))}
          <div className="etat">
            {erreurEtat && <span className="pastille rouge">API injoignable</span>}
            {etat && (
              <>
                <span className={`pastille ${etat.mode === "demo" ? "orange" : "verte"}`}>
                  {etat.mode === "demo" ? "Mode démo hors ligne" : "Connecté à Google Cloud"}
                </span>
                <span className="detail-etat">Modèle : {etat.modele}</span>
                <span className="detail-etat" title={etat.historique_secours || ""}>
                  Historique : {etat.historique === "firestore" ? "Firestore" : "mémoire (non conservé)"}
                </span>
              </>
            )}
            <BasculeTheme />
          </div>
        </nav>
        <main className="contenu">
          {etat?.mode === "demo" && (
            <div className="bandeau">Mode démo : données d'un instantané embarqué et réponses générées sans Gemini.</div>
          )}
          <Routes>
            <Route path="/" element={<Navigate to="/copilote" replace />} />
            <Route path="/copilote" element={<Copilote />} />
            <Route path="/fiabilite" element={<Fiabilite />} />
            <Route path="/kpi" element={<Kpi />} />
            <Route path="/actions" element={<Actions />} />
            <Route path="/prevision" element={<Bientot titre="Prévision de charge" couche={6}
              contenu={["Volume de lignes prévu pour les prochains jours (BigQuery ML)", "Heures et effectif nécessaires d'après les standards de productivité",
                        "Risques de rupture à partir du stock, des réceptions attendues et de la demande prévue"]} />} />
            <Route path="*" element={<Navigate to="/copilote" replace />} />
          </Routes>
        </main>
      </div>
      {demandeCode && (
        <div className="voile">
          <form className="modale" onSubmit={(e) => { e.preventDefault(); enregistrerCode(code); setDemandeCode(false); window.location.reload(); }}>
            <h2>Code d'accès</h2>
            <p className="note">Cette démo est protégée pour éviter toute consommation non autorisée.</p>
            <input type="password" autoFocus value={code} onChange={(e) => setCode(e.target.value)} />
            <button className="bouton principal">Valider</button>
          </form>
        </div>
      )}
    </TraceProvider>
  );
}
