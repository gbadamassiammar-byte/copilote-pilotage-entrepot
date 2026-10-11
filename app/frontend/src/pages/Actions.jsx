// Routine d'amélioration continue : dérives détectées par cartes de contrôle et plan d'actions.
// Même logique qu'un point OMS hebdomadaire : ce qui dérive, pourquoi, qui fait quoi, est-ce efficace.
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { ListeArtefacts } from "../components/Artefacts.jsx";
import { ActionTiroir, DeriveTiroir } from "../components/Lean.jsx";

const FILTRES = [
  { code: "ouvertes", label: "Ouvertes", garde: (d) => ["NOUVELLE", "EN_ANALYSE"].includes(d.statut) },
  { code: "toutes", label: "Toutes", garde: () => true },
  { code: "traitees", label: "Traitées", garde: (d) => d.statut === "TRAITEE" },
  { code: "fausses", label: "Fausses alertes", garde: (d) => d.statut === "FAUSSE_ALERTE" },
];
const FAMILLES = [
  { code: "", label: "Toutes familles" }, { code: "SERVICE", label: "Service client" },
  { code: "PREPARATION", label: "Préparation" }, { code: "TRANSPORT", label: "Transport" }, { code: "CLIENT", label: "Réclamations" },
];

export default function Actions() {
  const [derives, setDerives] = useState(null);
  const [actions, setActions] = useState(null);
  const [erreur, setErreur] = useState(null);
  const [filtre, setFiltre] = useState("ouvertes");
  const [famille, setFamille] = useState("");
  const [deriveOuverte, setDeriveOuverte] = useState(null);
  const [actionOuverte, setActionOuverte] = useState(null);

  const charger = useCallback(() => {
    setErreur(null);
    Promise.all([api.derives(), api.actions()])
      .then(([d, a]) => { setDerives(d); setActions(a); })
      .catch((e) => setErreur(e.message));
  }, []);
  useEffect(charger, [charger]);

  // le tableau des dérives vient de l'API ; le filtre ne garde que les lignes voulues
  const artefactsDerives = useMemo(() => {
    if (!derives) return [];
    const garde = FILTRES.find((f) => f.code === filtre).garde;
    const ids = new Set(derives.derives.filter((d) => garde(d) && (!famille || d.famille === famille)).map((d) => d.derive_id));
    return derives.artefacts.map((a) => (a.type === "tableau"
      ? { ...a, lignes: a.lignes.filter((l) => ids.has(l.derive_id)), titre: `${a.titre} : ${ids.size}` }
      : a));
  }, [derives, filtre, famille]);

  const ouvrir = (type, id) => {
    if (type === "derive") setDeriveOuverte(id);
    if (type === "action") setActionOuverte(id);
  };

  return (
    <div className="page">
      <header className="entete-page">
        <div>
          <h1>Dérives et plan d'actions</h1>
          <p className="note">
            Cartes de contrôle hebdomadaires calculées par sc_lean.detecter_derives() sur les KPI. Une dérive se traite comme
            en routine OMS : analyser où elle se concentre, vérifier la cause sur le terrain, décider d'une action, mesurer son efficacité.
          </p>
        </div>
        <button className="bouton" onClick={charger}>Actualiser</button>
      </header>
      {erreur && <div className="erreur">{erreur}</div>}
      {!derives && !erreur && <div className="chargement">Chargement des dérives...</div>}
      {derives && actions && (
        <>
          <div className="grille-kpi haut">
            <ListeArtefacts artefacts={artefactsDerives.filter((a) => a.type === "indicateurs")} />
            <ListeArtefacts artefacts={actions.artefacts.filter((a) => a.type === "indicateurs")} />
          </div>
          <div className="filtres">
            <label>Statut
              <div className="segments">
                {FILTRES.map((f) => (
                  <button key={f.code} className={filtre === f.code ? "actif" : ""} onClick={() => setFiltre(f.code)}>{f.label}</button>
                ))}
              </div>
            </label>
            <label>Famille
              <select value={famille} onChange={(e) => setFamille(e.target.value)}>
                {FAMILLES.map((f) => <option key={f.code} value={f.code}>{f.label}</option>)}
              </select>
            </label>
          </div>
          <div className="pile">
            <ListeArtefacts artefacts={artefactsDerives.filter((a) => a.type === "tableau")} onAction={ouvrir} />
            <ListeArtefacts artefacts={actions.artefacts.filter((a) => a.type === "tableau")} onAction={ouvrir} />
          </div>
        </>
      )}
      {deriveOuverte && (
        <DeriveTiroir id={deriveOuverte} onFerme={() => setDeriveOuverte(null)} onChange={charger} />
      )}
      {actionOuverte && (
        <ActionTiroir id={actionOuverte} onFerme={() => setActionOuverte(null)} onChange={charger}
                      onOuvrirDerive={(id) => { setActionOuverte(null); setDeriveOuverte(id); }} />
      )}
    </div>
  );
}
