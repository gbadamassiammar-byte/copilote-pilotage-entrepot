"""
Dérives et plan d'actions (amélioration continue), commun à BigQuery et au mode démo.

* analyser(entrepot, derive_id) : carte de contrôle autour de la dérive, puis
  analyse des contributions. Pour chaque segment (équipe, contrat, article,
  créneau...), on compare les échecs observés pendant la dérive aux échecs
  attendus si le segment s'était comporté comme pendant la référence (les 12
  semaines précédentes). L'excès d'échecs dit OU la dérive se concentre.
  Exemple : 120 erreurs de préparation en trop, dont 95 chez les intérimaires
  de moins de 30 jours (79 %).
* creer_action / modifier_action / modifier_derive : validation des saisies.
* efficacite(entrepot, action, derive) : valeur du KPI sur les 4 semaines qui
  suivent la date d'effet de l'action, comparée à la limite de contrôle.

Le taux de référence d'un petit segment est "rétréci" vers le taux global
(estimateur bayésien empirique) : un opérateur qui n'avait fait que 3 tâches
pendant la référence n'obtient pas un taux de référence de 0 % ou 33 %.
"""
import datetime as dt
import uuid

from entrepot import ErreurDonnees, normaliser_derive

SEMAINES_REFERENCE = 12
JOURS_EFFICACITE = 28
STATUTS_DERIVE = ["NOUVELLE", "EN_ANALYSE", "TRAITEE", "FAUSSE_ALERTE"]
STATUTS_ACTION = ["A_FAIRE", "EN_COURS", "FAITE", "EFFICACE", "NON_EFFICACE"]
LIBELLES_STATUT = {
    "NOUVELLE": "Nouvelle", "EN_ANALYSE": "En analyse", "TRAITEE": "Traitée", "FAUSSE_ALERTE": "Fausse alerte",
    "A_FAIRE": "A faire", "EN_COURS": "En cours", "FAITE": "Faite", "EFFICACE": "Efficace", "NON_EFFICACE": "Non efficace",
}
ORDRE_GRAVITE = {"CRITIQUE": 0, "MAJEURE": 1, "MINEURE": 2}
JOURS_FR = {"Monday": "lundi", "Tuesday": "mardi", "Wednesday": "mercredi", "Thursday": "jeudi",
            "Friday": "vendredi", "Saturday": "samedi", "Sunday": "dimanche"}
# ce que représentent n et e pour chaque famille, et comment afficher le taux
MESURES = {
    "SERVICE": {"echecs": "lignes en échec", "volume": "lignes", "facteur": 100, "unite": "%"},
    "ERREUR_PREPARATION": {"echecs": "erreurs", "volume": "tâches contrôlées", "facteur": 100, "unite": "%"},
    "PERFORMANCE_PICKING": {"echecs": "heures perdues", "volume": "heures standard", "facteur": None, "unite": "%"},
    "TRANSPORT": {"echecs": "enlèvements en retard", "volume": "enlèvements", "facteur": 100, "unite": "%"},
    "CLIENT": {"echecs": "réclamations", "volume": "lignes livrées", "facteur": 1000, "unite": "‰"},
}
LIBELLES_KPI = {
    "OTIF": "OTIF", "TAUX_RUPTURE": "Taux de rupture", "LIGNES_NON_MESURABLES": "Lignes non mesurables",
    "ERREUR_PREPARATION": "Erreurs de préparation", "PERFORMANCE_PICKING": "Performance picking",
    "RESPECT_CRENEAUX": "Respect des créneaux", "RECLAMATIONS_ENTREPOT": "Réclamations entrepôt",
}


def _date(v):
    if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def cle_mesure(derive):
    return derive["kpi_code"] if derive["famille"] == "PREPARATION" else derive["famille"]


def fenetres(derive):
    d1, d2 = _date(derive["periode_debut"]), _date(derive["periode_fin"])
    b2 = d1 - dt.timedelta(days=1)
    b1 = d1 - dt.timedelta(weeks=SEMAINES_REFERENCE)
    return d1, d2, b1, b2


def contributions(segments, totaux, mesure, top=5):
    """Excès d'échecs par segment, regroupé par dimension et trié."""
    if totaux:                                   # réclamations : même dénominateur pour tous les segments
        for s in segments:
            s["n_f"], s["n_b"] = totaux["n_f"], totaux["n_b"]
    par_dim = {}
    for s in segments:
        for k in ("n_f", "e_f", "n_b", "e_b"):
            s[k] = s.get(k) or 0.0
        par_dim.setdefault(s["dimension"], []).append(s)
    sortie = []
    for dim, segs in par_dim.items():
        if totaux:
            N_f, N_b = totaux["n_f"], totaux["n_b"]
        else:
            N_f, N_b = sum(s["n_f"] for s in segs), sum(s["n_b"] for s in segs)
        E_f, E_b = sum(s["e_f"] for s in segs), sum(s["e_b"] for s in segs)
        if not N_b or not N_f:
            continue
        taux_global = E_b / N_b
        poids = max(N_b * 0.01, 1e-9)            # force du rétrécissement : 1 % du volume de référence
        exces_total = E_f - N_f * taux_global
        lignes = []
        for s in segs:
            if totaux:      # réclamations : taux du segment rapporté au volume total de lignes
                taux_ref = s["e_b"] / N_b
            else:           # taux de référence du segment, rétréci vers le taux global
                taux_ref = (s["e_b"] + poids * taux_global) / (s["n_b"] + poids)
            attendu = s["n_f"] * taux_ref
            exces = s["e_f"] - attendu
            lignes.append({
                "segment": JOURS_FR.get(s["segment"], s["segment"]),
                "volume_fenetre": round(s["n_f"], 1), "echecs_fenetre": round(s["e_f"], 1),
                "attendu": round(attendu, 1), "exces": round(exces, 1),
                "taux_fenetre": _taux(s["e_f"], s["n_f"], mesure), "taux_reference": _taux(s["e_b"], s["n_b"], mesure),
                "part_exces": round(100 * exces / exces_total, 1) if exces_total > 0 else None,
                "part_volume": round(100 * s["n_f"] / N_f, 1) if N_f and not totaux else None,
            })
        lignes.sort(key=lambda l: -l["exces"])
        positifs = [l for l in lignes if l["exces"] > 0]
        top1 = positifs[0] if positifs and exces_total > 0 else None
        # pouvoir explicatif : part de l'excès portée par le premier segment, pondérée par sa rareté.
        # "92 % de l'excès sur un article qui pèse 1 % des lignes" explique beaucoup ;
        # "100 % sur l'équipe de nuit" quand le périmètre EST l'équipe de nuit n'apprend rien.
        score = 0.0
        if top1:
            part_vol = (top1["part_volume"] if top1["part_volume"] is not None else 100 / max(len(lignes), 1)) / 100
            score = min(top1["part_exces"], 100) / 100 * (1 - part_vol)
        sortie.append({
            "dimension": dim,
            "exces_total": round(exces_total, 1),
            "concentration_top1": top1["part_exces"] if top1 else None,
            "pouvoir_explicatif": round(score, 3),
            "segments": lignes[:top],
            "nb_segments": len(lignes),
        })
    sortie.sort(key=lambda d: -d["pouvoir_explicatif"])
    return sortie


def _taux(e, n, mesure):
    if not n:
        return None
    if mesure["facteur"] is None:                # performance = heures standard / heures réelles
        return round(100 * n / e, 2) if e else None
    return round(mesure["facteur"] * e / n, 3)


def analyser(entrepot, derive_id):
    d = entrepot.lean_derive(derive_id)
    d1, d2, b1, b2 = fenetres(d)
    mesure = MESURES[cle_mesure(d)]
    segments, totaux = entrepot.lean_segments(d["famille"], d["kpi_code"], d["perimetre_type"], d["perimetre_valeur"],
                                              d1, d2, b1, b2)
    carte = entrepot.lean_carte(d["kpi_code"], d["perimetre_type"], d["perimetre_valeur"],
                                b1, d2 + dt.timedelta(weeks=8))
    actions = [a for a in entrepot.lean_actions() if a["derive_id"] == d["derive_id"]]
    for a in actions:
        a["efficacite"] = efficacite(entrepot, a, d)
    return {
        "derive": d,
        "fenetre": {"debut": d1.isoformat(), "fin": d2.isoformat()},
        "reference": {"debut": b1.isoformat(), "fin": b2.isoformat()},
        "mesure": mesure,
        "contributions": contributions([dict(s) for s in segments], totaux, mesure),
        "carte": carte,
        "actions": actions,
    }


def compact_analyse(r):
    """Version réduite pour le modèle : les 3 premiers segments de chaque dimension."""
    d = r["derive"]
    return {
        "derive": {k: d.get(k) for k in ("derive_id", "kpi_code", "perimetre_type", "perimetre_valeur", "famille", "gravite",
                                         "periode_debut", "periode_fin", "valeur_observee", "valeur_reference",
                                         "limite_controle", "unite", "z_max", "nb_semaines", "statut", "analyse_causes")},
        "signaux_lies": d.get("signaux") or [],
        "fenetre": r["fenetre"], "reference": r["reference"],
        "unite_des_echecs": r["mesure"]["echecs"],
        "contributions": [{"dimension": c["dimension"], "exces_total": c["exces_total"],
                           "part_du_premier_segment_pct": c["concentration_top1"],
                           "pouvoir_explicatif": c["pouvoir_explicatif"],
                           "segments": [{k: s[k] for k in ("segment", "echecs_fenetre", "attendu", "exces", "part_exces",
                                                           "part_volume", "taux_fenetre", "taux_reference")} for s in c["segments"][:3]]}
                          for c in r["contributions"]],
        "actions": [{k: a.get(k) for k in ("action_id", "action", "responsable", "echeance", "statut", "efficacite")}
                    for a in r["actions"]],
    }


def efficacite(entrepot, action, derive):
    """Le KPI est-il revenu sous la limite de contrôle après la date d'effet ?"""
    if not action.get("date_effet"):
        return {"verdict": "NON_MESURABLE", "message": "Pas de date d'effet renseignée."}
    debut = _date(action["date_effet"])
    fin_donnees = _date(entrepot.kpi_bornes()["fin"])
    fin = min(debut + dt.timedelta(days=JOURS_EFFICACITE - 1), fin_donnees)
    if debut > fin_donnees or (fin - debut).days < 6:
        return {"verdict": "NON_MESURABLE", "message": "Pas encore assez de données après la date d'effet (une semaine minimum)."}
    lignes = entrepot.kpi_sommes([derive["kpi_code"]], debut, fin, "MONTH", derive["perimetre_type"], debut)
    num = sum(l["num"] for l in lignes if l["perimetre_valeur"] == derive["perimetre_valeur"])
    den = sum(l["den"] for l in lignes if l["perimetre_valeur"] == derive["perimetre_valeur"])
    if not den:
        return {"verdict": "NON_MESURABLE", "message": "Aucune donnée sur le périmètre après la date d'effet."}
    facteur = 1000 if derive.get("unite") == "‰" else 100
    valeur = round(facteur * num / den, 3)
    limite = derive.get("limite_controle")
    favorable_haut = derive.get("sens") == "HAUSSE_FAVORABLE"
    sous_controle = limite is None or (valeur >= limite if favorable_haut else valeur <= limite)
    return {
        "verdict": "RETOUR_SOUS_CONTROLE" if sous_controle else "TOUJOURS_HORS_CONTROLE",
        "valeur_apres": valeur, "periode": {"debut": debut.isoformat(), "fin": fin.isoformat()},
        "limite_controle": limite, "valeur_reference": derive.get("valeur_reference"),
        "valeur_pendant_derive": derive.get("valeur_observee"),
    }


def tableau_derives(entrepot):
    derives = entrepot.lean_derives()
    derives.sort(key=lambda d: (d["statut"] in ("TRAITEE", "FAUSSE_ALERTE"), ORDRE_GRAVITE.get(d["gravite"], 9),
                                -_date(d["periode_debut"]).toordinal()))
    return derives


def actions_avec_efficacite(entrepot):
    derives = {d["derive_id"]: d for d in entrepot.lean_derives()}
    actions = entrepot.lean_actions()
    for a in actions:
        d = derives.get(a["derive_id"])
        a["derive"] = {k: d.get(k) for k in ("kpi_code", "perimetre_type", "perimetre_valeur", "gravite", "unite")} if d else None
        a["efficacite"] = efficacite(entrepot, a, d) if d else {"verdict": "NON_MESURABLE", "message": "Dérive supprimée."}
    return actions


# ----------------------------------------------------------------------
# Saisies
# ----------------------------------------------------------------------
def _texte(v, nom, maxi=600, obligatoire=True):
    v = (v or "").strip()
    if obligatoire and not v:
        raise ErreurDonnees(f"Le champ « {nom} » est obligatoire.")
    if len(v) > maxi:
        raise ErreurDonnees(f"Le champ « {nom} » dépasse {maxi} caractères.")
    return v or None


def _date_saisie(v, nom, obligatoire=True):
    if not v:
        if obligatoire:
            raise ErreurDonnees(f"Le champ « {nom} » est obligatoire (AAAA-MM-JJ).")
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ErreurDonnees(f"« {nom} » invalide : '{v}' (format attendu AAAA-MM-JJ).")


def creer_action(entrepot, donnees, cree_par="interface"):
    d = entrepot.lean_derive(normaliser_derive(donnees.get("derive_id")))
    date_effet = _date_saisie(donnees.get("date_effet"), "date d'effet", obligatoire=False)
    if date_effet is None:                       # par défaut : lendemain de la fin de la dérive
        date_effet = _date(d["periode_fin"]) + dt.timedelta(days=1)
    action = {
        "action_id": f"ACT-{uuid.uuid4().hex[:8].upper()}",
        "derive_id": d["derive_id"],
        "cause_identifiee": _texte(donnees.get("cause_identifiee"), "cause identifiée"),
        "action": _texte(donnees.get("action"), "action"),
        "responsable": _texte(donnees.get("responsable"), "responsable", 120),
        "echeance": _date_saisie(donnees.get("echeance"), "échéance"),
        "date_effet": date_effet,
        "cree_par": cree_par,
    }
    a = entrepot.lean_creer_action(action)
    if d["statut"] == "NOUVELLE":
        entrepot.lean_maj_derive(d["derive_id"], {"statut": "EN_ANALYSE"})
    return a


def modifier_action(entrepot, action_id, donnees):
    champs = {}
    if donnees.get("statut"):
        if donnees["statut"] not in STATUTS_ACTION:
            raise ErreurDonnees(f"Statut inconnu (possibles : {', '.join(STATUTS_ACTION)}).")
        champs["statut"] = donnees["statut"]
    if "commentaire" in donnees:
        champs["commentaire"] = _texte(donnees.get("commentaire"), "commentaire", 1000, obligatoire=False)
    if donnees.get("responsable"):
        champs["responsable"] = _texte(donnees["responsable"], "responsable", 120)
    if donnees.get("echeance"):
        champs["echeance"] = _date_saisie(donnees["echeance"], "échéance")
    if donnees.get("date_effet"):
        champs["date_effet"] = _date_saisie(donnees["date_effet"], "date d'effet")
    if not champs:
        raise ErreurDonnees("Rien à modifier.")
    return entrepot.lean_maj_action(action_id, champs)


def modifier_derive(entrepot, derive_id, donnees):
    champs = {}
    if donnees.get("statut"):
        if donnees["statut"] not in STATUTS_DERIVE:
            raise ErreurDonnees(f"Statut inconnu (possibles : {', '.join(STATUTS_DERIVE)}).")
        champs["statut"] = donnees["statut"]
    if donnees.get("analyse_causes"):
        champs["analyse_causes"] = _texte(donnees["analyse_causes"], "analyse des causes", 6000)
    if not champs:
        raise ErreurDonnees("Rien à modifier.")
    return entrepot.lean_maj_derive(derive_id, champs)
