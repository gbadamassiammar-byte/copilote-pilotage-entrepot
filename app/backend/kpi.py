"""
Calcul des KPI à la lecture, commun à BigQuery et au mode démo.

La source (sc_kpi.kpi_jour) contient des numérateurs et dénominateurs par jour
et par périmètre. Ce module :
* valide la demande (période, granularité, axe, liste de KPI) ;
* demande à l'entrepôt les sommes regroupées par période et périmètre, sur la
  période demandée ET la période précédente de même durée (pour la tendance) ;
* calcule les valeurs (somme des numérateurs / somme des dénominateurs) ;
* ajoute les objectifs contractuels quand ils existent (par donneur d'ordre).
"""
import datetime as dt

from entrepot import ErreurDonnees

GRANULARITES = {"jour": "DAY", "semaine": "WEEK(MONDAY)", "mois": "MONTH"}
AXES = {"global": "GLOBAL", "donneur": "DONNEUR", "transporteur": "TRANSPORTEUR", "equipe": "EQUIPE"}

SERVICE = ["OTIF", "OTIF_BRUT", "TAUX_SERVICE", "TAUX_RUPTURE", "LIGNES_NON_MESURABLES"]
PREPARATION = ["ERREUR_PREPARATION", "PRODUCTIVITE_PICKING", "PERFORMANCE_PICKING"]
AXES_PAR_KPI = {
    **{k: {"GLOBAL", "DONNEUR", "TRANSPORTEUR"} for k in SERVICE},
    **{k: {"GLOBAL", "EQUIPE"} for k in PREPARATION},
    "RESPECT_CRENEAUX": {"GLOBAL", "TRANSPORTEUR"},
    "RECLAMATIONS_ENTREPOT": {"GLOBAL"},
}
TOUS_KPI = list(AXES_PAR_KPI)
FACTEUR = {"%": 100.0, "‰": 1000.0}
DUREE_MAX_JOURS = 400


def _date(v, nom):
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        raise ErreurDonnees(f"{nom} invalide : '{v}' (format attendu AAAA-MM-JJ).")


def periode_par_defaut(bornes):
    """Dernier mois complet des données."""
    fin = _date(bornes["fin"], "fin")
    debut = fin.replace(day=1)
    return debut, fin


def valider(kpis, debut, fin, granularite, axe, bornes):
    granularite = (granularite or "mois").lower()
    if granularite not in GRANULARITES:
        raise ErreurDonnees(f"Granularité '{granularite}' inconnue (jour, semaine ou mois).")
    axe_code = AXES.get((axe or "global").lower())
    if not axe_code:
        raise ErreurDonnees(f"Axe '{axe}' inconnu (global, donneur, transporteur ou equipe).")
    if not kpis:
        kpis = [k for k in TOUS_KPI if axe_code in AXES_PAR_KPI[k]]
    kpis = [str(k).upper() for k in kpis]
    inconnus = [k for k in kpis if k not in AXES_PAR_KPI]
    if inconnus:
        raise ErreurDonnees(f"KPI inconnu(s) : {', '.join(inconnus)}. Disponibles : {', '.join(TOUS_KPI)}.")
    non_dispo = [k for k in kpis if axe_code not in AXES_PAR_KPI[k]]
    kpis = [k for k in kpis if axe_code in AXES_PAR_KPI[k]]
    if not kpis:
        raise ErreurDonnees(f"Aucun des KPI demandés n'est disponible par {axe} : {', '.join(non_dispo)}.")
    d_def, f_def = periode_par_defaut(bornes)
    debut = _date(debut, "date_debut") if debut else d_def
    fin = _date(fin, "date_fin") if fin else f_def
    b_debut, b_fin = _date(bornes["debut"], "debut"), _date(bornes["fin"], "fin")
    debut, fin = max(debut, b_debut), min(fin, b_fin)
    if debut > fin:
        raise ErreurDonnees(f"Période vide : les données couvrent du {b_debut} au {b_fin}.")
    if (fin - debut).days > DUREE_MAX_JOURS:
        raise ErreurDonnees(f"Période trop longue (maximum {DUREE_MAX_JOURS} jours).")
    # une courbe d'un seul point n'apprend rien : on affine la granularité si besoin
    if granularite == "mois" and debut.replace(day=1) == fin.replace(day=1):
        granularite = "semaine"
    if granularite == "semaine" and (fin - debut).days < 7 and debut.weekday() <= fin.weekday():
        granularite = "jour"
    return kpis, debut, fin, granularite, axe_code, non_dispo


def calculer(entrepot, kpis=None, debut=None, fin=None, granularite="mois", axe="global"):
    bornes = entrepot.kpi_bornes()
    kpis, debut, fin, granularite, axe_code, non_dispo = valider(kpis, debut, fin, granularite, axe, bornes)
    duree = (fin - debut).days + 1
    debut_prec = debut - dt.timedelta(days=duree)
    lignes = entrepot.kpi_sommes(kpis, debut_prec, fin, GRANULARITES[granularite], axe_code, debut)
    defs = {d["kpi_code"]: d for d in entrepot.kpi_definitions()}
    libelles = entrepot.libelles_perimetres()
    objectifs = entrepot.objectifs() if axe_code == "DONNEUR" else {}

    resultats = {}
    for k in kpis:
        facteur = FACTEUR.get(defs.get(k, {}).get("unite"), 1.0)
        cumul, cumul_prec, serie = {}, {}, []
        for l in lignes:
            if l["kpi_code"] != k:
                continue
            p_debut = _date(l["periode"], "periode")
            cle = l["perimetre_valeur"]
            # l'entrepôt indique si les jours regroupés appartiennent à la période demandée
            # ou à la période précédente (utile quand une semaine ou un mois chevauche les deux)
            cible = cumul if l["dans_periode"] else cumul_prec
            c = cible.setdefault(cle, [0.0, 0.0])
            c[0] += l["num"]
            c[1] += l["den"]
            if cible is cumul and l["den"]:
                # une semaine ou un mois entamé avant la période est daté du premier jour de la période
                serie.append({"periode": max(p_debut, debut).isoformat(), "perimetre": cle,
                              "valeur": round(facteur * l["num"] / l["den"], 3)})
        par_perimetre = []
        for cle, (n, d) in sorted(cumul.items(), key=lambda x: -x[1][1]):
            np_, dp = cumul_prec.get(cle, (0.0, 0.0))
            par_perimetre.append({
                "perimetre": cle, "libelle": libelles.get(cle, cle),
                "valeur": round(facteur * n / d, 3) if d else None,
                "valeur_precedente": round(facteur * np_ / dp, 3) if dp else None,
                "numerateur": round(n, 2), "denominateur": round(d, 2),
                "objectif": objectifs.get(k, {}).get(cle),
            })
        resultats[k] = {"par_perimetre": par_perimetre, "serie": serie}

    return {
        "periode": {"debut": debut.isoformat(), "fin": fin.isoformat(), "granularite": granularite},
        "periode_precedente": {"debut": debut_prec.isoformat(), "fin": (debut - dt.timedelta(days=1)).isoformat()},
        "axe": axe_code, "donnees_disponibles": bornes,
        "kpi_non_disponibles_pour_cet_axe": non_dispo,
        "definitions": {k: {c: defs.get(k, {}).get(c) for c in ("libelle", "version", "unite", "sens", "formule", "description")}
                        for k in kpis},
        "resultats": resultats,
    }


def compact(r, points_max=15):
    """Version réduite pour le modèle (chaque jeton coûte)."""
    out = {k: v for k, v in r.items() if k != "resultats"}
    out["definitions"] = {k: {c: d[c] for c in ("libelle", "version", "unite", "sens")} for k, d in r["definitions"].items()}
    out["resultats"] = {}
    for k, v in r["resultats"].items():
        out["resultats"][k] = {
            "par_perimetre": [{c: p[c] for c in ("perimetre", "libelle", "valeur", "valeur_precedente", "denominateur", "objectif")}
                              for p in v["par_perimetre"]],
            "serie": v["serie"][-points_max * max(1, len(v["par_perimetre"])):],
        }
    return out


CAUSES = {"RUPTURE": "Rupture ou lot bloqué", "NON_LIVREE": "Non livrée à la fin des données",
          "RETARD_ET_INCOMPLET": "En retard et incomplète", "RETARD": "En retard", "INCOMPLET": "Incomplète"}
