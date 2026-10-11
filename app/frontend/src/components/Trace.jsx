// Panneau latéral de traçabilité, ouvrable depuis n'importe quel tableau :
// un clic sur un numéro de commande ou de lot affiche son historique complet.
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "../api.js";
import { ListeArtefacts } from "./Artefacts.jsx";

const Ctx = createContext({ tracer: () => {} });
export const useTrace = () => useContext(Ctx);

export function TraceProvider({ children }) {
  const [cible, setCible] = useState(null);
  const [etat, setEtat] = useState({ chargement: false, artefacts: null, erreur: null });

  const tracer = useCallback((type, id) => setCible({ type, id }), []);

  useEffect(() => {
    if (!cible) return;
    let actif = true;
    setEtat({ chargement: true, artefacts: null, erreur: null });
    const f = cible.type === "lot" ? api.tracerLot : api.tracerCommande;
    f(cible.id)
      .then((d) => actif && setEtat({ chargement: false, artefacts: d.artefacts, erreur: null }))
      .catch((e) => actif && setEtat({ chargement: false, artefacts: null, erreur: e.message }));
    return () => { actif = false; };
  }, [cible]);

  useEffect(() => {
    const echap = (e) => e.key === "Escape" && setCible(null);
    window.addEventListener("keydown", echap);
    return () => window.removeEventListener("keydown", echap);
  }, []);

  return (
    <Ctx.Provider value={{ tracer }}>
      {children}
      {cible && (
        <div className="voile" onClick={() => setCible(null)}>
          <aside className="tiroir" onClick={(e) => e.stopPropagation()}>
            <header>
              <div>
                <span className="surtitre">Traçabilité {cible.type === "lot" ? "du lot" : "de la commande"}</span>
                <h2>{cible.id}</h2>
              </div>
              <button className="fermer" onClick={() => setCible(null)} aria-label="Fermer">×</button>
            </header>
            {etat.chargement && <div className="chargement">Interrogation de BigQuery...</div>}
            {etat.erreur && <div className="erreur">{etat.erreur}</div>}
            <ListeArtefacts artefacts={etat.artefacts} />
          </aside>
        </div>
      )}
    </Ctx.Provider>
  );
}
