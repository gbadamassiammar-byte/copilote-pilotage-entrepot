// Prévision de charge : volume de lignes à préparer sur 4 semaines et besoin en renfort (intérim).
// Même source que l'outil prevision_charge de l'agent (API /api/prevision).
import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { ListeArtefacts } from "../components/Artefacts.jsx";

const EQUIPES = [
  { code: "TOUS", label: "Site entier" }, { code: "MATIN", label: "Matin" },
  { code: "APRES_MIDI", label: "Après-midi" }, { code: "NUIT", label: "Nuit" },
];

export default function Prevision() {
  const [equipe, setEquipe] = useState("TOUS");
  const [data, setData] = useState(null);
  const [erreur, setErreur] = useState(null);
  const [chargement, setChargement] = useState(false);

  const charger = useCallback(() => {
    setChargement(true); setErreur(null);
    api.prevision(equipe)
      .then(setData)
      .catch((e) => setErreur(e.message))
      .finally(() => setChargement(false));
  }, [equipe]);
  useEffect(charger, [charger]);

  const q = data?.qualite?.find((r) => r.equipe === equipe);

  return (
    <div className="page">
      <header className="entete-page">
        <div>
          <h1>Prévision de charge</h1>
          <p className="note">
            Lignes à préparer sur les 4 prochaines semaines, par un modèle ARIMA_PLUS (BigQuery ML) qui apprend la tendance,
            le rythme de la semaine et les jours fériés. Traduit en heures de picking puis en renfort par rapport à l'effectif habituel.
          </p>
        </div>
      </header>

      <div className="filtres">
        <label>Périmètre
          <div className="segments">
            {EQUIPES.map((e) => (
              <button key={e.code} className={equipe === e.code ? "actif" : ""} onClick={() => setEquipe(e.code)}>{e.label}</button>
            ))}
          </div>
        </label>
        {q && (
          <p className="note" style={{ marginLeft: "auto" }}>
            Fiabilité : erreur de {String(q.wape_modele_pct).replace(".", ",")} % sur les 4 dernières semaines
            {q.wape_naive_pct != null && <>, contre {String(q.wape_naive_pct).replace(".", ",")} % pour une méthode simple</>}.
            {q.gagne_sur_naif ? " Le modèle fait mieux." : " Le modèle ne fait pas mieux : prudence."}
          </p>
        )}
      </div>

      {erreur && <div className="erreur">{erreur}</div>}
      {chargement && !data && <div className="chargement">Chargement de la prévision...</div>}
      {data && <ListeArtefacts artefacts={data.artefacts} />}
    </div>
  );
}
