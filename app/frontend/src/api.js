// Accès à l'API REST Flask. Toutes les pages passent par ici.

const CLE_CODE = "copilote_code_acces";

export function codeAcces() {
  try { return sessionStorage.getItem(CLE_CODE) || ""; } catch { return ""; }
}
export function enregistrerCode(code) {
  try { sessionStorage.setItem(CLE_CODE, code); } catch { /* navigation privée */ }
}

export class ErreurApi extends Error {
  constructor(message, statut) { super(message); this.statut = statut; }
}

async function appel(chemin, options = {}) {
  const entetes = { "Content-Type": "application/json" };
  const code = codeAcces();
  if (code) entetes["X-Code-Acces"] = code;
  let rep;
  try {
    rep = await fetch(chemin, { ...options, headers: { ...entetes, ...(options.headers || {}) } });
  } catch {
    throw new ErreurApi("Serveur injoignable. Le serveur Flask est-il lancé ?", 0);
  }
  let corps = null;
  try { corps = await rep.json(); } catch { /* réponse non JSON */ }
  if (!rep.ok) {
    if (rep.status === 401) window.dispatchEvent(new Event("code-acces-requis"));
    throw new ErreurApi(corps?.erreur || `Erreur ${rep.status}`, rep.status);
  }
  return corps;
}

export const api = {
  etat: () => appel("/api/etat"),
  chat: (question, conversation_id) =>
    appel("/api/chat", { method: "POST", body: JSON.stringify({ question, conversation_id }) }),
  conversations: () => appel("/api/conversations"),
  conversation: (id) => appel(`/api/conversations/${encodeURIComponent(id)}`),
  synthese: () => appel("/api/fiabilite/synthese"),
  anomalies: (regle, limite = 100) =>
    appel(`/api/fiabilite/anomalies?regle=${encodeURIComponent(regle)}&limite=${limite}`),
  lancerControles: (regle_id) =>
    appel("/api/fiabilite/lancer", { method: "POST", body: JSON.stringify({ regle_id }) }),
  tracerCommande: (id) => appel(`/api/tracer/commande/${encodeURIComponent(id)}`),
  tracerLot: (id) => appel(`/api/tracer/lot/${encodeURIComponent(id)}`),
  kpiBornes: () => appel("/api/kpi/bornes"),
  kpiTableau: (p) => appel(`/api/kpi/tableau?${parametres(p)}`),
  kpiEchecs: (p) => appel(`/api/kpi/echecs?${parametres(p)}`),
  kpiDefinitions: () => appel("/api/kpi/definitions"),
  derives: () => appel("/api/lean/derives"),
  derive: (id) => appel(`/api/lean/derives/${encodeURIComponent(id)}`),
  majDerive: (id, champs) => appel(`/api/lean/derives/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(champs) }),
  actions: () => appel("/api/lean/actions"),
  creerAction: (champs) => appel("/api/lean/actions", { method: "POST", body: JSON.stringify(champs) }),
  majAction: (id, champs) => appel(`/api/lean/actions/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(champs) }),
  prevision: (equipe) => appel(`/api/prevision?equipe=${encodeURIComponent(equipe || "TOUS")}`),
  previsionQualite: () => appel("/api/prevision/qualite"),
};

function parametres(p) {
  return new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
}
