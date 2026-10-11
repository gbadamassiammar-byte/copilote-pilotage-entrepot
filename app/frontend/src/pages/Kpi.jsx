// Tableau de bord des indicateurs OMS : mêmes données et mêmes calculs que l'outil
// indicateurs_kpi de l'agent (API /api/kpi/*). Chaque chiffre mène aux lignes en échec.
import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { ListeArtefacts } from "../components/Artefacts.jsx";
import { formater } from "../format.js";

const AXES = [
  { code: "global", label: "Site" }, { code: "donneur", label: "Donneur d'ordre" },
  { code: "transporteur", label: "Transporteur" }, { code: "equipe", label: "Equipe" },
];
// KPI détaillable ligne à ligne selon l'axe de ventilation
const KPI_ECHECS = { donneur: "OTIF", transporteur: "OTIF", equipe: "ERREUR_PREPARATION" };

function decaler(iso, mois) {
  const d = new Date(`${iso}T12:00:00Z`);
  d.setUTCDate(1);
  d.setUTCMonth(d.getUTCMonth() + mois);
  return d.toISOString().slice(0, 10);
}

function periodes(fin) {
  const debutMois = `${fin.slice(0, 7)}-01`;
  return {
    mois: { label: "Dernier mois", debut: debutMois, fin, granularite: "semaine" },
    trimestre: { label: "3 derniers mois", debut: decaler(debutMois, -2), fin, granularite: "semaine" },
    annee: { label: "12 mois", debut: decaler(debutMois, -11), fin, granularite: "mois" },
  };
}

export default function Kpi() {
  const [bornes, setBornes] = useState(null);
  const [filtre, setFiltre] = useState(null);       // {preset, debut, fin, granularite, axe}
  const [global, setGlobal] = useState(null);
  const [ventilation, setVentilation] = useState(null);
  const [chargement, setChargement] = useState(false);
  const [erreur, setErreur] = useState(null);
  const [tiroir, setTiroir] = useState(null);

  useEffect(() => {
    api.kpiBornes()
      .then((b) => {
        setBornes(b);
        setFiltre({ preset: "annee", ...periodes(b.fin).annee, axe: "global" });
      })
      .catch((e) => setErreur(e.message));
  }, []);

  useEffect(() => {
    if (!filtre) return;
    let actif = true;
    setChargement(true); setErreur(null);
    const base = { debut: filtre.debut, fin: filtre.fin, granularite: filtre.granularite };
    Promise.all([
      api.kpiTableau({ ...base, axe: "global" }),
      filtre.axe === "global" ? Promise.resolve(null) : api.kpiTableau({ ...base, axe: filtre.axe }),
    ])
      .then(([g, v]) => { if (actif) { setGlobal(g); setVentilation(v); } })
      .catch((e) => actif && setErreur(e.message))
      .finally(() => actif && setChargement(false));
    return () => { actif = false; };
  }, [filtre]);

  const presets = useMemo(() => (bornes ? periodes(bornes.fin) : {}), [bornes]);

  function choisirPreset(cle) {
    if (cle === "perso") setFiltre((f) => ({ ...f, preset: "perso" }));
    else setFiltre((f) => ({ ...f, preset: cle, ...presets[cle] }));
  }

  async function ouvrirEchecs(kpi, axe, valeur, libelle) {
    const titre = `${kpi === "OTIF" ? "Lignes en échec OTIF" : "Erreurs de préparation"}${libelle ? ` - ${libelle}` : ""}`;
    setTiroir({ titre, chargement: true });
    try {
      const d = await api.kpiEchecs({ kpi, debut: filtre.debut, fin: filtre.fin, axe, valeur, limite: 50 });
      setTiroir({ titre, artefacts: d.artefacts, note: d.note });
    } catch (e) {
      setTiroir({ titre, erreur: e.message });
    }
  }

  async function ouvrirDefinitions() {
    setTiroir({ titre: "Définitions des KPI", chargement: true });
    try {
      const d = await api.kpiDefinitions();
      setTiroir({ titre: "Définitions des KPI", artefacts: d.artefacts });
    } catch (e) {
      setTiroir({ titre: "Définitions des KPI", erreur: e.message });
    }
  }

  function actionGlobal(type, kpi) {
    if (type === "echecs") ouvrirEchecs(kpi, "global", null, "site");
  }

  function actionVentilation(type, perimetre) {
    const kpi = KPI_ECHECS[filtre.axe];
    if (type !== "echecs" || !kpi) return;
    const libelle = ventilation?.resultats?.[kpi]?.par_perimetre.find((p) => p.perimetre === perimetre)?.libelle;
    ouvrirEchecs(kpi, filtre.axe, perimetre, libelle || perimetre);
  }

  const cartes = global?.artefacts.filter((a) => a.type === "indicateurs") || [];
  const courbesGlobales = global?.artefacts.filter((a) => a.type === "courbes") || [];

  return (
    <div className="page">
      <header className="entete-page">
        <div>
          <h1>Indicateurs de performance</h1>
          <p className="note">
            Calculés par la procédure sc_kpi.calculer_kpi() : numérateur et dénominateur par jour, agrégés par somme.
            {global && ` Période du ${formater(global.periode.debut, "date")} au ${formater(global.periode.fin, "date")}, comparée à la période précédente de même durée quand les données existent.`}
          </p>
        </div>
        <button className="bouton" onClick={ouvrirDefinitions}>Définitions</button>
      </header>

      {filtre && (
        <div className="filtres">
          <label>Période
            <div className="segments">
              {Object.entries(presets).map(([cle, p]) => (
                <button key={cle} className={filtre.preset === cle ? "actif" : ""} onClick={() => choisirPreset(cle)}>{p.label}</button>
              ))}
              <button className={filtre.preset === "perso" ? "actif" : ""} onClick={() => choisirPreset("perso")}>Personnalisée</button>
            </div>
          </label>
          {filtre.preset === "perso" && (
            <>
              <label>Du <input type="date" value={filtre.debut} min={bornes.debut} max={bornes.fin}
                               onChange={(e) => e.target.value && setFiltre((f) => ({ ...f, debut: e.target.value }))} /></label>
              <label>Au <input type="date" value={filtre.fin} min={bornes.debut} max={bornes.fin}
                               onChange={(e) => e.target.value && setFiltre((f) => ({ ...f, fin: e.target.value }))} /></label>
            </>
          )}
          <label>Granularité
            <select value={filtre.granularite} onChange={(e) => setFiltre((f) => ({ ...f, granularite: e.target.value }))}>
              <option value="jour">Jour</option><option value="semaine">Semaine</option><option value="mois">Mois</option>
            </select>
          </label>
          <label>Ventilation
            <div className="segments">
              {AXES.map((a) => (
                <button key={a.code} className={filtre.axe === a.code ? "actif" : ""}
                        onClick={() => setFiltre((f) => ({ ...f, axe: a.code }))}>{a.label}</button>
              ))}
            </div>
          </label>
          {chargement && <span className="note">Calcul...</span>}
        </div>
      )}

      {erreur && <div className="erreur">{erreur}</div>}
      {!global && !erreur && <div className="chargement">Chargement des indicateurs...</div>}
      {global && (
        <>
          <ListeArtefacts artefacts={cartes} onAction={actionGlobal} />
          <div className="grille-kpi">
            <div className="colonne"><ListeArtefacts artefacts={courbesGlobales} /></div>
            <div className="colonne">
              {ventilation
                ? <ListeArtefacts artefacts={ventilation.artefacts} onAction={actionVentilation} />
                : <section className="carte">
                    <h3>Lire ce tableau de bord</h3>
                    <ul className="aide">
                      <li><b>OTIF fiabilisé</b> : lignes dédoublonnées et mesurables ; une rupture compte comme un échec.</li>
                      <li><b>OTIF brut</b> : ce qu'afficherait un tableau branché directement sur les données. L'écart entre les deux courbes est le coût de la non-qualité des données.</li>
                      <li>Cliquez sur la carte OTIF ou Erreurs de préparation pour voir les lignes en échec, ou choisissez une ventilation pour comparer donneurs, transporteurs ou équipes.</li>
                      <li>La flèche compare à la période précédente de même durée ; vert = évolution favorable.</li>
                    </ul>
                  </section>}
            </div>
          </div>
        </>
      )}

      {tiroir && (
        <div className="voile" onClick={() => setTiroir(null)}>
          <aside className="tiroir" onClick={(e) => e.stopPropagation()}>
            <header>
              <div><span className="surtitre">Traçabilité du KPI</span><h2 className="titre-texte">{tiroir.titre}</h2></div>
              <button className="fermer" onClick={() => setTiroir(null)} aria-label="Fermer">×</button>
            </header>
            {tiroir.chargement && <div className="chargement">Chargement...</div>}
            {tiroir.erreur && <div className="erreur">{tiroir.erreur}</div>}
            {tiroir.note && <div className="info">{tiroir.note}</div>}
            <ListeArtefacts artefacts={tiroir.artefacts} />
          </aside>
        </div>
      )}
    </div>
  );
}
