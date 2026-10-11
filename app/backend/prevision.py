"""
Prévision de charge (couche 7), commun à BigQuery et au mode démo.

Le modèle ARIMA_PLUS tourne dans BigQuery (sql/09_prevision.sql). Ce module se
contente de LIRE les tables produites et de les mettre en forme :
* tableau(entrepot)   : prévision à 28 jours par jour et par équipe, agrégée par
  semaine, avec le besoin en renfort (intérim) ;
* qualite(entrepot)   : le backtest (WAPE du modèle contre méthode naïve) ;
* serie(entrepot, e)  : historique récent + prévision avec intervalle, pour la courbe.

Rien n'est recalculé ici : la vérité vient de BigQuery. On ne fait que choisir ce
qu'on montre au responsable (les jours en tension, le renfort par semaine).
"""
import datetime as dt

from entrepot import ErreurDonnees

EQUIPES = ["TOUS", "MATIN", "APRES_MIDI", "NUIT"]
LIBELLES_EQUIPE = {"TOUS": "Site", "MATIN": "Equipe du matin", "APRES_MIDI": "Equipe d'après-midi", "NUIT": "Equipe de nuit"}
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _date(v):
    if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def _semaine(jour):
    d = _date(jour)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def tableau(entrepot, equipe="TOUS"):
    equipe = (equipe or "TOUS").upper()
    if equipe not in EQUIPES:
        raise ErreurDonnees(f"Equipe inconnue : {equipe}. Valeurs : {', '.join(EQUIPES)}.")
    lignes = [l for l in entrepot.prevision_charge() if l["equipe"] == equipe]
    if not lignes:
        raise ErreurDonnees("La prévision n'a pas encore été calculée : exécutez CALL sc_ml.prevoir_charge().")
    ouvres = [l for l in lignes if l["jour_ouvre"]]
    # agrégation par semaine : lignes, heures, et renfort maximum d'un jour de la semaine
    semaines = {}
    for l in ouvres:
        s = semaines.setdefault(_semaine(l["jour"]), {"semaine": _semaine(l["jour"]), "lignes": 0.0, "heures": 0.0,
                                                      "heures_habituelles": 0.0, "renfort_max": 0, "renfort_securite_max": 0, "jours": 0})
        s["lignes"] += l["lignes_prevues"]
        s["heures"] += l["heures_necessaires"] or 0
        s["heures_habituelles"] += l["heures_habituelles"] or 0
        s["renfort_max"] = max(s["renfort_max"], l["renfort_etp"] or 0)
        s["renfort_securite_max"] = max(s["renfort_securite_max"], l["renfort_securite_etp"] or 0)
        s["jours"] += 1
    jours_tension = [{
        "jour": l["jour"], "jour_semaine": JOURS_FR[_date(l["jour"]).weekday()],
        "lignes_prevues": round(l["lignes_prevues"]), "borne_haute": round(l["borne_haute"]),
        "heures_necessaires": l["heures_necessaires"], "heures_habituelles": l["heures_habituelles"],
        "ecart_heures": l["ecart_heures"], "renfort_etp": l["renfort_etp"], "renfort_securite_etp": l["renfort_securite_etp"],
    } for l in ouvres if (l["renfort_etp"] or 0) > 0]
    return {
        "equipe": equipe, "libelle_equipe": LIBELLES_EQUIPE[equipe],
        "periode": {"debut": ouvres[0]["jour"], "fin": ouvres[-1]["jour"], "jours_ouvres": len(ouvres)},
        "total_lignes": round(sum(l["lignes_prevues"] for l in ouvres)),
        "productivite": round(ouvres[0]["productivite"], 1) if ouvres[0]["productivite"] else None,
        "semaines": sorted(semaines.values(), key=lambda x: x["semaine"]),
        "jours_tension": jours_tension,
        "feries": [{"jour": l["jour"], "libelle": l["libelle_ferie"]} for l in lignes
                   if not l["jour_ouvre"] and l["libelle_ferie"]],
    }


def qualite(entrepot):
    q = entrepot.prevision_qualite()
    for r in q:
        r["gagne_sur_naif"] = (r["wape_modele_pct"] is not None and r["wape_naive_pct"] is not None
                               and r["wape_modele_pct"] < r["wape_naive_pct"])
    return q


def serie(entrepot, equipe="TOUS", jours_historique=90):
    equipe = (equipe or "TOUS").upper()
    if equipe not in EQUIPES:
        raise ErreurDonnees(f"Equipe inconnue : {equipe}.")
    hist = entrepot.prevision_historique(equipe, jours_historique)
    prev = [l for l in entrepot.prevision_charge() if l["equipe"] == equipe]
    return {
        "equipe": equipe, "libelle_equipe": LIBELLES_EQUIPE[equipe],
        "historique": [{"jour": h["jour"], "reel": round(h["lignes"])} for h in hist],
        "prevision": [{"jour": p["jour"], "prevu": round(p["lignes_prevues"]),
                       "borne_basse": round(p["borne_basse"]), "borne_haute": round(p["borne_haute"])}
                      for p in prev if p["jour_ouvre"]],
    }


def compact(entrepot, equipe="TOUS"):
    """Version réduite pour le modèle : totaux par semaine et jours en tension, pas le détail jour par jour."""
    t = tableau(entrepot, equipe)
    q = {r["equipe"]: r for r in qualite(entrepot)}.get(t["equipe"], {})
    return {
        "equipe": t["equipe"], "periode": t["periode"], "total_lignes_prevues": t["total_lignes"],
        "productivite_lignes_par_heure": t["productivite"],
        "par_semaine": [{"semaine": s["semaine"], "lignes": round(s["lignes"]),
                         "heures_necessaires": round(s["heures"]), "heures_habituelles": round(s["heures_habituelles"]),
                         "renfort_etp_max": s["renfort_max"]} for s in t["semaines"]],
        "jours_a_renforcer": [{"jour": j["jour"], "jour_semaine": j["jour_semaine"], "lignes": j["lignes_prevues"],
                               "renfort_etp": j["renfort_etp"], "renfort_securite_etp": j["renfort_securite_etp"]}
                              for j in t["jours_tension"]],
        "jours_feries_sans_activite": t["feries"],
        "fiabilite": {"wape_modele_pct": q.get("wape_modele_pct"), "wape_naive_pct": q.get("wape_naive_pct"),
                      "biais_pct": q.get("biais_pct")},
        "note": "Renfort = postes à ajouter à l'équipe habituelle pour tenir la charge prévue. Prévision indicative, à confirmer la veille.",
    }
