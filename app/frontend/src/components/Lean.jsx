// Tiroirs de la routine d'amélioration continue : détail d'une dérive (carte de contrôle,
// où se concentre l'excès, analyse du copilote, nouvelle action) et détail d'une action
// (suivi, efficacité). Utilisés par la page Dérives et actions et par le panneau du copilote.
import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "../api.js";
import { formater, formaterKpi } from "../format.js";
import { ListeArtefacts } from "./Artefacts.jsx";

export const STATUTS_DERIVE = [
  { code: "NOUVELLE", label: "Nouvelle" }, { code: "EN_ANALYSE", label: "En analyse" },
  { code: "TRAITEE", label: "Traitée" }, { code: "FAUSSE_ALERTE", label: "Fausse alerte" },
];
export const STATUTS_ACTION = [
  { code: "A_FAIRE", label: "A faire" }, { code: "EN_COURS", label: "En cours" }, { code: "FAITE", label: "Faite" },
  { code: "EFFICACE", label: "Efficace" }, { code: "NON_EFFICACE", label: "Non efficace" },
];
const VERDICTS = {
  RETOUR_SOUS_CONTROLE: { label: "Retour sous contrôle", ton: "ok" },
  TOUJOURS_HORS_CONTROLE: { label: "Toujours hors contrôle", ton: "critique" },
  NON_MESURABLE: { label: "Non mesurable", ton: "neutre" },
};

function Tiroir({ surtitre, titre, onFerme, children }) {
  useEffect(() => {
    const echap = (e) => e.key === "Escape" && onFerme();
    window.addEventListener("keydown", echap);
    return () => window.removeEventListener("keydown", echap);
  }, [onFerme]);
  return (
    <div className="voile" onClick={onFerme}>
      <aside className="tiroir" onClick={(e) => e.stopPropagation()}>
        <header>
          <div><span className="surtitre">{surtitre}</span><h2>{titre}</h2></div>
          <button className="fermer" onClick={onFerme} aria-label="Fermer">×</button>
        </header>
        {children}
      </aside>
    </div>
  );
}

function lendemain(iso) {
  const d = new Date(`${String(iso).slice(0, 10)}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

// ----------------------------------------------------------------------
export function DeriveTiroir({ id, onFerme, onChange }) {
  const [r, setR] = useState(null);
  const [erreur, setErreur] = useState(null);
  const [analyse, setAnalyse] = useState({ etat: "repos" });   // repos | attente | ok | erreur
  const [message, setMessage] = useState(null);

  const charger = useCallback(() => {
    api.derive(id).then(setR).catch((e) => setErreur(e.message));
  }, [id]);
  useEffect(charger, [charger]);

  const d = r?.derive;

  async function changerStatut(statut) {
    try {
      await api.majDerive(id, { statut });
      charger(); onChange?.();
    } catch (e) { setMessage(e.message); }
  }

  async function demanderAnalyse() {
    setAnalyse({ etat: "attente" });
    try {
      const rep = await api.chat(
        `Analyse la dérive ${id} : constat chiffré, où l'excès se concentre, hypothèses de cause racine à vérifier ` +
        `sur le terrain (5 pourquoi) et une action corrective proposée avec son indicateur d'efficacité. Ne crée pas d'action.`);
      setAnalyse({ etat: "ok", texte: rep.reponse });
    } catch (e) {
      setAnalyse({ etat: "erreur", texte: e.message });
    }
  }

  async function enregistrerAnalyse() {
    try {
      await api.majDerive(id, { analyse_causes: analyse.texte, statut: d.statut === "NOUVELLE" ? "EN_ANALYSE" : undefined });
      setMessage("Analyse enregistrée dans la dérive.");
      setAnalyse({ etat: "repos" });
      charger(); onChange?.();
    } catch (e) { setMessage(e.message); }
  }

  return (
    <Tiroir surtitre="Dérive détectée" titre={id} onFerme={onFerme}>
      {erreur && <div className="erreur">{erreur}</div>}
      {!r && !erreur && <div className="chargement">Analyse en cours...</div>}
      {d && (
        <>
          <section className="carte">
            <div className="entete-carte">
              <h3>Statut</h3>
              <div className="segments">
                {STATUTS_DERIVE.map((s) => (
                  <button key={s.code} className={d.statut === s.code ? "actif" : ""} onClick={() => changerStatut(s.code)}>{s.label}</button>
                ))}
              </div>
            </div>
            <p className="note">
              Détectée par la règle {d.regle === "R1" ? "R1 (un point au-delà de 3 sigma)" : "R2 (2 points sur 3 au-delà de 2 sigma)"} sur {d.nb_semaines} semaine(s).
              Gravité {String(d.gravite).toLowerCase()}.
            </p>
          </section>
          {message && <div className="info">{message}</div>}
          <ListeArtefacts artefacts={r.artefacts.filter((a) => !(a.type === "tableau" && a.titre === "Actions rattachées"))} />

          <section className="carte">
            <div className="entete-carte">
              <h3>Analyse des causes</h3>
              <button className="bouton" onClick={demanderAnalyse} disabled={analyse.etat === "attente"}>
                {analyse.etat === "attente" ? "Logix analyse..." : "Demander a Logix"}
              </button>
            </div>
            {d.analyse_causes && analyse.etat !== "ok" && <div className="analyse"><ReactMarkdown>{d.analyse_causes}</ReactMarkdown></div>}
            {!d.analyse_causes && analyse.etat === "repos" && (
              <p className="note">Logix lit la carte de contrôle et les contributions, puis propose des hypothèses a verifier sur le terrain.</p>
            )}
            {analyse.etat === "erreur" && <div className="erreur">{analyse.texte}</div>}
            {analyse.etat === "ok" && (
              <>
                <div className="analyse"><ReactMarkdown>{analyse.texte}</ReactMarkdown></div>
                <button className="bouton principal" onClick={enregistrerAnalyse}>Enregistrer cette analyse dans la dérive</button>
              </>
            )}
          </section>

          <ActionsRattachees actions={r.actions} unite={d.unite} />
          <FormulaireAction derive={d} onCree={() => { setMessage("Action ajoutée au plan."); charger(); onChange?.(); }} />
        </>
      )}
    </Tiroir>
  );
}

function ActionsRattachees({ actions, unite }) {
  if (!actions?.length) return null;
  return (
    <section className="carte">
      <h3>Actions rattachées</h3>
      <ul className="liste-actions">
        {actions.map((a) => {
          const v = VERDICTS[a.efficacite?.verdict] || VERDICTS.NON_MESURABLE;
          return (
            <li key={a.action_id}>
              <div><b>{a.action}</b></div>
              <div className="note">
                {a.responsable}, échéance {formater(a.echeance, "date")}, {STATUTS_ACTION.find((s) => s.code === a.statut)?.label || a.statut}
              </div>
              <span className={`badge badge-${v.ton}`}>{v.label}</span>
              {a.efficacite?.valeur_apres != null && (
                <span className="note"> {formaterKpi(a.efficacite.valeur_apres, unite)} du {formater(a.efficacite.periode.debut, "date")} au {formater(a.efficacite.periode.fin, "date")} (limite {formaterKpi(a.efficacite.limite_controle, unite)})</span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function FormulaireAction({ derive, onCree }) {
  const vide = { cause_identifiee: "", action: "", responsable: "", echeance: "", date_effet: lendemain(derive.periode_fin) };
  const [f, setF] = useState(vide);
  const [envoi, setEnvoi] = useState(false);
  const [erreur, setErreur] = useState(null);
  const champ = (cle) => ({ value: f[cle], onChange: (e) => setF((x) => ({ ...x, [cle]: e.target.value })) });

  async function soumettre(e) {
    e.preventDefault();
    setEnvoi(true); setErreur(null);
    try {
      await api.creerAction({ derive_id: derive.derive_id, ...f });
      setF(vide); onCree();
    } catch (ex) { setErreur(ex.message); } finally { setEnvoi(false); }
  }

  return (
    <form className="carte formulaire" onSubmit={soumettre}>
      <h3>Nouvelle action corrective</h3>
      <label className="large">Cause identifiée<textarea rows={2} required maxLength={600} {...champ("cause_identifiee")}
        placeholder="Ex : intérimaires intégrés de nuit sans formation au poste" /></label>
      <label className="large">Action<textarea rows={2} required maxLength={600} {...champ("action")}
        placeholder="Ex : parcours d'intégration de 3 jours et binôme avec un préparateur confirmé" /></label>
      <label>Responsable<input required maxLength={120} {...champ("responsable")} /></label>
      <label>Echéance<input type="date" required {...champ("echeance")} /></label>
      <label title="Date de mise en oeuvre : l'efficacité est mesurée sur les 4 semaines qui suivent">
        Date d'effet<input type="date" {...champ("date_effet")} />
      </label>
      {erreur && <div className="erreur large">{erreur}</div>}
      <div className="large"><button className="bouton principal" disabled={envoi}>{envoi ? "Enregistrement..." : "Ajouter au plan d'actions"}</button></div>
    </form>
  );
}

// ----------------------------------------------------------------------
export function ActionTiroir({ id, onFerme, onChange, onOuvrirDerive }) {
  const [a, setA] = useState(null);
  const [f, setF] = useState(null);
  const [erreur, setErreur] = useState(null);
  const [message, setMessage] = useState(null);

  const charger = useCallback(() => {
    api.actions()
      .then((r) => {
        const x = r.actions.find((y) => y.action_id === id);
        if (!x) throw new Error(`Action ${id} introuvable.`);
        setA(x);
        setF({ statut: x.statut, commentaire: x.commentaire || "", echeance: String(x.echeance || "").slice(0, 10),
               date_effet: String(x.date_effet || "").slice(0, 10) });
      })
      .catch((e) => setErreur(e.message));
  }, [id]);
  useEffect(charger, [charger]);

  async function enregistrer(e) {
    e.preventDefault();
    setMessage(null);
    try {
      await api.majAction(id, f);
      setMessage("Action mise à jour.");
      charger(); onChange?.();
    } catch (ex) { setMessage(ex.message); }
  }

  const v = a && (VERDICTS[a.efficacite?.verdict] || VERDICTS.NON_MESURABLE);
  const unite = a?.derive?.unite;
  return (
    <Tiroir surtitre="Action corrective" titre={id} onFerme={onFerme}>
      {erreur && <div className="erreur">{erreur}</div>}
      {!a && !erreur && <div className="chargement">Chargement...</div>}
      {a && (
        <>
          <section className="carte">
            <h3>{a.action}</h3>
            <p><span className="note">Cause : </span>{a.cause_identifiee}</p>
            <p className="note">
              Créée par {a.cree_par === "copilote" ? "Logix" : "l'interface"} le {formater(a.cree_le, "datetime")}
              {a.derive && <> pour la dérive <button className="lien-trace" onClick={() => onOuvrirDerive?.(a.derive_id)}>{a.derive_id}</button></>}
            </p>
          </section>
          <section className={`carte efficacite ton-${v.ton}`}>
            <h3>Efficacité : {v.label}</h3>
            {a.efficacite?.valeur_apres != null ? (
              <p>
                {formaterKpi(a.efficacite.valeur_apres, unite)} du {formater(a.efficacite.periode.debut, "date")} au {formater(a.efficacite.periode.fin, "date")},
                pour une limite de contrôle de {formaterKpi(a.efficacite.limite_controle, unite)} (référence {formaterKpi(a.efficacite.valeur_reference, unite)},
                {" "}{formaterKpi(a.efficacite.valeur_pendant_derive, unite)} pendant la dérive).
              </p>
            ) : <p className="note">{a.efficacite?.message}</p>}
            <p className="note">Mesure automatique sur les 4 semaines suivant la date d'effet. Le statut « Efficace » reste une décision du responsable.</p>
          </section>
          {message && <div className="info">{message}</div>}
          <form className="carte formulaire" onSubmit={enregistrer}>
            <h3>Suivi</h3>
            <label>Statut
              <select value={f.statut} onChange={(e) => setF((x) => ({ ...x, statut: e.target.value }))}>
                {STATUTS_ACTION.map((s) => <option key={s.code} value={s.code}>{s.label}</option>)}
              </select>
            </label>
            <label>Echéance<input type="date" value={f.echeance} onChange={(e) => setF((x) => ({ ...x, echeance: e.target.value }))} /></label>
            <label>Date d'effet<input type="date" value={f.date_effet} onChange={(e) => setF((x) => ({ ...x, date_effet: e.target.value }))} /></label>
            <label className="large">Commentaire<textarea rows={3} maxLength={1000} value={f.commentaire}
              onChange={(e) => setF((x) => ({ ...x, commentaire: e.target.value }))} /></label>
            <div className="large"><button className="bouton principal">Enregistrer</button></div>
          </form>
        </>
      )}
    </Tiroir>
  );
}
