// Mise en forme française des valeurs (nombres, dates, horodatages).

const nf = new Intl.NumberFormat("fr-FR");
const df = new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "2-digit", year: "numeric" });
const dtf = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Paris",
});

export function formater(valeur, format) {
  if (valeur === null || valeur === undefined || valeur === "") return "";
  switch (format) {
    case "nombre":
      return typeof valeur === "number" ? nf.format(valeur) : String(valeur);
    case "date": {
      const d = new Date(String(valeur).length === 10 ? `${valeur}T12:00:00` : valeur);
      return isNaN(d) ? String(valeur) : df.format(d);
    }
    case "datetime": {
      const d = new Date(valeur);
      return isNaN(d) ? String(valeur) : dtf.format(d);
    }
    default:
      if (typeof valeur === "number") return nf.format(valeur);
      if (typeof valeur === "boolean") return valeur ? "Oui" : "Non";
      return String(valeur);
  }
}

// Reconnaît une donnée traçable : commande, ligne de commande ou lot (éventuellement "LOT...|date").
export function cibleTrace(valeur) {
  if (typeof valeur !== "string") return null;
  let m = valeur.match(/^(CMD\d{8})(-\d{5})?$/);
  if (m) return { type: "commande", id: m[1] };
  m = valeur.match(/^(LOT\d{6})(\|.*)?$/);
  if (m) return { type: "lot", id: m[1] };
  return null;
}

// Valeur de KPI avec son unité : 96,04 %, 0,30 ‰, 46,1 lignes/h.
export function formaterKpi(v, unite) {
  if (v === null || v === undefined || v === "") return "n.d.";
  if (typeof v !== "number") return String(v);
  const dec = unite === "%" || unite === "‰" ? 2 : 1;
  return `${v.toLocaleString("fr-FR", { minimumFractionDigits: dec, maximumFractionDigits: dec })} ${unite || ""}`.trim();
}

const MOIS_COURTS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."];
// Etiquette d'une période de courbe selon la granularité (date ISO du début de période).
export function etiquettePeriode(iso, granularite) {
  const [a, m, j] = String(iso).split("-").map(Number);
  if (granularite === "mois") return `${MOIS_COURTS[m - 1]} ${String(a).slice(2)}`;
  return `${String(j).padStart(2, "0")}/${String(m).padStart(2, "0")}`;
}

export const LIBELLES_OUTILS = {
  synthese_fiabilite: "Synthèse de fiabilité",
  detail_anomalies: "Détail des anomalies",
  tracer_commande: "Traçabilité commande",
  tracer_lot: "Traçabilité lot",
  lancer_controles: "Exécution des contrôles",
  indicateurs_kpi: "Indicateurs KPI",
  lignes_en_echec: "Lignes en échec",
  definitions_kpi: "Définitions des KPI",
  derives_detectees: "Dérives détectées",
  analyser_derive: "Analyse d'une dérive",
  plan_actions: "Plan d'actions",
  creer_action: "Création d'action",
};
