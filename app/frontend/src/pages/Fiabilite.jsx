// Tableau de bord de fiabilité : les résultats des 21 contrôles, sans passer par le chat.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { ListeArtefacts } from "../components/Artefacts.jsx";

export default function Fiabilite() {
  const [synthese, setSynthese] = useState(null);
  const [erreur, setErreur] = useState(null);
  const [detail, setDetail] = useState(null);
  const [lancement, setLancement] = useState(false);
  const [message, setMessage] = useState(null);

  const charger = useCallback(() => {
    setErreur(null);
    api.synthese().then(setSynthese).catch((e) => setErreur(e.message));
  }, []);
  useEffect(charger, [charger]);

  async function ouvrirRegle(type, regleId) {
    if (type !== "regle") return;
    setDetail({ regleId, chargement: true });
    try {
      const d = await api.anomalies(regleId, 200);
      setDetail({ regleId, artefacts: d.artefacts });
    } catch (e) {
      setDetail({ regleId, erreur: e.message });
    }
  }

  async function relancer() {
    if (!window.confirm("Relancer les 21 contrôles dans BigQuery ? L'exécution prend une à plusieurs minutes.")) return;
    setLancement(true); setMessage(null);
    try {
      const d = await api.lancerControles(null);
      setMessage(`${d.regles_executees.length} règles exécutées en ${d.duree_s} s.`);
      charger();
    } catch (e) {
      setMessage(e.message);
    } finally {
      setLancement(false);
    }
  }

  return (
    <div className="page">
      <header className="entete-page">
        <div>
          <h1>Fiabilité des données</h1>
          <p className="note">Résultat de la dernière exécution des règles de contrôle stockées dans sc_dq.dq_regles.</p>
        </div>
        <button className="bouton" onClick={relancer} disabled={lancement}>
          {lancement ? "Exécution en cours..." : "Relancer les contrôles"}
        </button>
      </header>
      {message && <div className="info">{message}</div>}
      {erreur && <div className="erreur">{erreur}</div>}
      {!synthese && !erreur && <div className="chargement">Chargement...</div>}
      {synthese && (
        <div className="grille-fiabilite">
          <div className="colonne">
            <ListeArtefacts artefacts={synthese.artefacts.filter((a) => a.type !== "tableau")} />
          </div>
          <div className="colonne">
            <ListeArtefacts artefacts={synthese.artefacts.filter((a) => a.type === "tableau")} onAction={ouvrirRegle} />
          </div>
        </div>
      )}
      {detail && (
        <div className="voile" onClick={() => setDetail(null)}>
          <aside className="tiroir" onClick={(e) => e.stopPropagation()}>
            <header>
              <div><span className="surtitre">Détail de la règle</span><h2>{detail.regleId}</h2></div>
              <button className="fermer" onClick={() => setDetail(null)} aria-label="Fermer">×</button>
            </header>
            {detail.chargement && <div className="chargement">Chargement des anomalies...</div>}
            {detail.erreur && <div className="erreur">{detail.erreur}</div>}
            <ListeArtefacts artefacts={detail.artefacts} />
          </aside>
        </div>
      )}
    </div>
  );
}
