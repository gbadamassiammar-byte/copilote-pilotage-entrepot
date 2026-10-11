"""
Les outils de l'agent.

Chaque outil a :
* une DECLARATION (nom, description, paramètres) envoyée à Gemini : c'est en
  lisant les descriptions que le modèle choisit quel outil appeler ;
* une EXECUTION qui renvoie deux choses :
    - un résultat compact pour le modèle (limité en taille : chaque jeton coûte) ;
    - des "artefacts" pour l'interface : indicateurs, tableaux, graphiques que le
      panneau de droite affiche à côté de la réponse.

Ajouter un outil = ajouter une déclaration et une fonction ici. L'agent et
l'interface n'ont pas à changer.
"""
import datetime as dt
import time

import kpi as K
import lean as L
from entrepot import ErreurDonnees

DECLARATIONS = [
    {
        "name": "synthese_fiabilite",
        "description": (
            "Bilan de la dernière exécution des contrôles de fiabilité des données : pour chacune des règles, "
            "le nombre d'anomalies, la gravité (CRITIQUE, MAJEURE, MINEURE) et la dimension (unicité, complétude, "
            "cohérence, exactitude, conformité BPD), plus des indicateurs globaux dont la part des lignes de commande "
            "sans anomalie. A utiliser pour toute question générale sur la fiabilité ou la qualité des données."),
        "parameters_json_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "detail_anomalies",
        "description": (
            "Liste détaillée des anomalies trouvées par UNE règle de contrôle, avec la clé de chaque donnée en "
            "anomalie (ligne de commande, commande, lot|jour, tâche de préparation...) et les valeurs constatées. "
            "Utiliser après synthese_fiabilite pour creuser une règle précise."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "regle_id": {"type": "string", "description": "Identifiant de la règle, par exemple DQ_CNF_002."},
                "limite": {"type": "integer", "description": "Nombre maximum d'anomalies à renvoyer (1 à 50, défaut 20)."},
            },
            "required": ["regle_id"],
        },
    },
    {
        "name": "tracer_commande",
        "description": (
            "Traçabilité complète d'une commande : en-tête (donneur d'ordre, destinataire, transporteur), lignes et "
            "quantités, tâches de préparation (lot prélevé, opérateur, équipe, résultat du contrôle), historique des "
            "statuts, et anomalies de fiabilité qui la concernent. Accepte un numéro de commande (CMD00012345) "
            "ou de ligne (CMD00012345-00010)."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {"identifiant": {"type": "string", "description": "Numéro de commande ou de ligne."}},
            "required": ["identifiant"],
        },
    },
    {
        "name": "tracer_lot",
        "description": (
            "Traçabilité d'un lot de médicament : article, péremption, statut qualité, rappel éventuel, stock actuel "
            "par emplacement, historique des mouvements, nombre de commandes et de destinataires servis avec ce lot, "
            "et anomalies associées. Indispensable pour une question de rappel de lot."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {"identifiant": {"type": "string", "description": "Identifiant du lot, par exemple LOT005075."}},
            "required": ["identifiant"],
        },
    },
    {
        "name": "indicateurs_kpi",
        "description": (
            "Indicateurs de performance de l'entrepôt sur une période : OTIF (fiabilisé), OTIF_BRUT (calcul naïf, "
            "sert à mesurer l'effet des données non fiables), TAUX_SERVICE, TAUX_RUPTURE, LIGNES_NON_MESURABLES, "
            "ERREUR_PREPARATION, PRODUCTIVITE_PICKING (lignes/h), PERFORMANCE_PICKING (% du standard), RESPECT_CRENEAUX, "
            "RECLAMATIONS_ENTREPOT (pour 1000 lignes). Renvoie la valeur sur la période, la valeur de la période "
            "précédente de même durée, l'évolution par jour, semaine ou mois, et la ventilation selon un axe. "
            "Axes : global ; donneur (KPI de service) ; transporteur (service et créneaux) ; equipe (préparation). "
            "Sans dates, prend le dernier mois complet des données."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "kpis": {"type": "array", "items": {"type": "string", "enum": K.TOUS_KPI},
                         "description": "KPI voulus ; vide = tous ceux disponibles pour l'axe."},
                "date_debut": {"type": "string", "description": "AAAA-MM-JJ"},
                "date_fin": {"type": "string", "description": "AAAA-MM-JJ"},
                "granularite": {"type": "string", "enum": list(K.GRANULARITES)},
                "axe": {"type": "string", "enum": list(K.AXES)},
            },
        },
    },
    {
        "name": "lignes_en_echec",
        "description": (
            "Traçabilité d'un KPI jusqu'à la donnée : pour OTIF, les lignes de commande en échec sur la période avec "
            "leur cause (retard, incomplète, rupture, non livrée) et la répartition des causes ; pour "
            "ERREUR_PREPARATION, les tâches de prélèvement en erreur avec l'opérateur, son équipe, son contrat et son "
            "ancienneté, et la répartition par équipe / contrat / ancienneté. Indispensable pour expliquer une baisse."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "kpi": {"type": "string", "enum": ["OTIF", "ERREUR_PREPARATION"]},
                "date_debut": {"type": "string", "description": "AAAA-MM-JJ"},
                "date_fin": {"type": "string", "description": "AAAA-MM-JJ"},
                "axe": {"type": "string", "enum": list(K.AXES), "description": "Filtre optionnel."},
                "valeur": {"type": "string", "description": "Valeur du filtre : DO-01, T01, NUIT..."},
                "limite": {"type": "integer", "description": "Nombre de lignes détaillées (défaut 20, max 50)."},
            },
            "required": ["kpi"],
        },
    },
    {
        "name": "definitions_kpi",
        "description": "Définitions officielles et versionnées des KPI : formule, règles de calcul, unité, sens favorable.",
        "parameters_json_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "derives_detectees",
        "description": (
            "Dérives de performance détectées automatiquement par cartes de contrôle hebdomadaires (méthode Lean / "
            "Shewhart) sur les KPI : OTIF, ruptures, lignes non mesurables, erreurs de préparation, performance picking, "
            "respect des créneaux, réclamations. Chaque dérive (identifiant DER-XXXXXXXX) a une période, une gravité, "
            "un écart à la référence en nombre de sigma, un statut (NOUVELLE, EN_ANALYSE, TRAITEE, FAUSSE_ALERTE) et "
            "des signaux liés (autres KPI touchés en même temps). A utiliser pour « qu'est-ce qui dérive », « quels "
            "problèmes traiter en priorité », la routine hebdomadaire."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "statut": {"type": "string", "enum": L.STATUTS_DERIVE, "description": "Filtre optionnel."},
                "famille": {"type": "string", "enum": ["SERVICE", "PREPARATION", "TRANSPORT", "CLIENT"]},
            },
        },
    },
    {
        "name": "analyser_derive",
        "description": (
            "Analyse d'une dérive : carte de contrôle autour de la période, puis analyse des contributions. Pour chaque "
            "dimension (équipe, type de contrat, ancienneté, opérateur, article, donneur d'ordre, transporteur, heure "
            "de créneau...), compare les échecs observés pendant la dérive à ceux attendus d'après les 12 semaines "
            "précédentes, et indique quels segments portent l'excès. Renvoie aussi les actions déjà rattachées et leur "
            "efficacité mesurée. Indispensable avant d'expliquer une dérive ou de proposer une cause racine."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {"derive_id": {"type": "string", "description": "Identifiant, par exemple DER-48734335."}},
            "required": ["derive_id"],
        },
    },
    {
        "name": "plan_actions",
        "description": (
            "Plan d'actions de la routine OMS : actions correctives rattachées aux dérives (cause identifiée, action, "
            "responsable, échéance, statut) avec leur efficacité mesurée : le KPI est-il revenu sous la limite de "
            "contrôle dans les 4 semaines suivant la date d'effet ?"),
        "parameters_json_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "creer_action",
        "description": (
            "Enregistre une action corrective dans le plan d'actions. Opération d'ECRITURE : ne l'utiliser QUE si "
            "l'utilisateur demande explicitement d'enregistrer ou de créer l'action, avec un responsable et une "
            "échéance donnés ou validés par lui. Pour simplement proposer une action, répondre en texte."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "derive_id": {"type": "string"},
                "cause_identifiee": {"type": "string", "description": "Cause racine retenue, en une phrase."},
                "action": {"type": "string", "description": "Action concrète (quoi, comment, où)."},
                "responsable": {"type": "string"},
                "echeance": {"type": "string", "description": "AAAA-MM-JJ"},
                "date_effet": {"type": "string", "description": "AAAA-MM-JJ, date de mise en oeuvre (optionnelle)."},
            },
            "required": ["derive_id", "cause_identifiee", "action", "responsable", "echeance"],
        },
    },
    {
        "name": "lancer_controles",
        "description": (
            "Relance l'exécution des règles de contrôle dans BigQuery (une seule règle si regle_id est fourni). "
            "Opération longue (une à plusieurs minutes) : ne l'utiliser QUE si l'utilisateur demande explicitement "
            "de relancer ou de rafraîchir les contrôles. Pour consulter les résultats, utiliser synthese_fiabilite."),
        "parameters_json_schema": {
            "type": "object",
            "properties": {"regle_id": {"type": "string", "description": "Optionnel : une seule règle à exécuter."}},
        },
    },
]

TON_SEVERITE = {"CRITIQUE": "critique", "MAJEURE": "alerte", "MINEURE": "info"}
RANG_SEVERITE = {"CRITIQUE": 0, "MAJEURE": 1, "MINEURE": 2}


def _date_fr(iso):
    """'2026-06-22' -> '22/06/2026' (pour les textes composés côté serveur)."""
    if not iso or len(str(iso)) < 10:
        return iso
    a, m, j = str(iso)[:10].split("-")
    return f"{j}/{m}/{a}"


def _fmt_pct(v):
    return None if v is None else f"{v:.2f} %".replace(".", ",")


# ----------------------------------------------------------------------
# Construction des artefacts (format commun lu par le frontend React)
# ----------------------------------------------------------------------
def artefacts_synthese(s):
    i = s["indicateurs"]
    regles = s["regles"]
    return [
        {"type": "indicateurs", "titre": "Fiabilité des données", "items": [
            {"label": "Lignes de commande fiables", "valeur": _fmt_pct(i["lignes_commande_fiables_pct"]),
             "ton": "ok" if (i["lignes_commande_fiables_pct"] or 0) >= 99 else "alerte"},
            {"label": "Anomalies critiques", "valeur": i["nb_anomalies_critiques"],
             "ton": "critique" if i["nb_anomalies_critiques"] else "ok"},
            {"label": "Règles en anomalie", "valeur": f"{i['nb_regles_en_anomalie']} / {i['nb_regles_executees']}",
             "ton": "alerte" if i["nb_regles_en_anomalie"] else "ok"},
            {"label": "Dernier contrôle", "valeur": i["dernier_controle"], "format": "datetime"},
        ]},
        {"type": "barres", "titre": "Anomalies par règle", "echelle": "log", "donnees": [
            {"label": r["regle_id"], "description": r["libelle"], "valeur": r["nb_anomalies"] or 0,
             "ton": TON_SEVERITE.get(r["severite"], "info")}
            for r in regles if (r.get("nb_anomalies") or 0) > 0]},
        {"type": "tableau", "titre": "Détail des règles", "colonnes": [
            {"cle": "regle_id", "label": "Règle"}, {"cle": "libelle", "label": "Contrôle"},
            {"cle": "severite", "label": "Gravité", "format": "badge"},
            {"cle": "dimension", "label": "Dimension"},
            {"cle": "nb_anomalies", "label": "Anomalies", "format": "nombre"}],
         "lignes": [{k: r.get(k) for k in ("regle_id", "libelle", "severite", "dimension", "nb_anomalies")}
                    for r in regles], "action": {"type": "regle", "cle": "regle_id"}},
    ]


def artefacts_anomalies(d):
    r = d["regle"]
    lignes = [{"cle_ligne": a["cle_ligne"], **({"detail": a["detail"]} if a.get("detail") is not None else {})}
              for a in d["anomalies"]]
    return [
        {"type": "indicateurs", "titre": f"{r['regle_id']} : {r['libelle']}", "items": [
            {"label": "Anomalies", "valeur": r["nb_anomalies"], "ton": TON_SEVERITE.get(r["severite"], "info")},
            {"label": "Gravité", "valeur": r["severite"], "ton": TON_SEVERITE.get(r["severite"], "info")},
            {"label": "Table contrôlée", "valeur": r["table_cible"]},
        ], "note": r.get("description")},
        {"type": "tableau", "titre": f"Anomalies ({len(lignes)} affichées sur {r['nb_anomalies']})",
         "colonnes": [{"cle": "cle_ligne", "label": "Donnée en anomalie", "format": "trace"},
                      {"cle": "detail", "label": "Valeurs constatées", "format": "json"}],
         "lignes": lignes},
    ]


def artefacts_commande(d):
    c = d["commande"]
    nb_anom = len(d["anomalies"])
    items = [
        {"label": "Statut", "valeur": c["statut_commande"]},
        {"label": "Donneur d'ordre", "valeur": c.get("donneur_ordre") or c["donneur_ordre_id"]},
        {"label": "Destinataire", "valeur": c.get("destinataire") or c["destinataire_id"]},
        {"label": "Anomalies", "valeur": nb_anom, "ton": "critique" if nb_anom else "ok"},
    ]
    if d["nb_entetes"] > 1:
        items.append({"label": "En-têtes en double", "valeur": d["nb_entetes"], "ton": "alerte"})
    sortie = [
        {"type": "indicateurs", "titre": f"Commande {c['commande_id']}", "items": items},
        {"type": "tableau", "titre": "Lignes de commande", "colonnes": [
            {"cle": "ligne_id", "label": "Ligne"}, {"cle": "libelle", "label": "Article"},
            {"cle": "qte_commandee", "label": "Commandé", "format": "nombre"},
            {"cle": "qte_livree", "label": "Livré", "format": "nombre"},
            {"cle": "date_promise", "label": "Promis le", "format": "date"},
            {"cle": "date_livraison_reelle", "label": "Livré le", "format": "datetime"},
            {"cle": "statut_ligne", "label": "Statut"}, {"cle": "motif_ecart", "label": "Motif"}],
         "lignes": d["lignes"]},
        {"type": "tableau", "titre": "Préparation", "colonnes": [
            {"cle": "tache_id", "label": "Tâche"}, {"cle": "lot_id", "label": "Lot", "format": "trace"},
            {"cle": "operateur_id", "label": "Opérateur"}, {"cle": "equipe", "label": "Equipe"},
            {"cle": "type_contrat", "label": "Contrat"}, {"cle": "debut", "label": "Début", "format": "datetime"},
            {"cle": "resultat_controle", "label": "Contrôle"}],
         "lignes": d["preparations"]},
        {"type": "chronologie", "titre": "Historique des statuts",
         "etapes": [{"label": s["statut"], "date": s["horodatage"], "source": s["source_systeme"]}
                    for s in d["historique"]]},
    ]
    if d["anomalies"]:
        sortie.append(_tableau_anomalies_liees(d["anomalies"]))
    return sortie


def artefacts_lot(d):
    l = d["lot"]
    f = d["diffusion"] or {}
    stock = sum(s["qte_physique"] or 0 for s in d["stock_actuel"])
    sortie = [
        {"type": "indicateurs", "titre": f"Lot {l['lot_id']} ({l['num_lot']})", "items": [
            {"label": "Statut qualité", "valeur": l["statut_qualite"],
             "ton": "ok" if l["statut_qualite"] == "LIBRE" else "alerte"},
            {"label": "Rappel", "valeur": f"Oui, le {_date_fr(l['date_rappel'])}" if l["rappele"] else "Non",
             "ton": "critique" if l["rappele"] else "ok"},
            {"label": "Péremption", "valeur": l["date_peremption"], "format": "date"},
            {"label": "Stock actuel", "valeur": stock, "format": "nombre"},
            {"label": "Commandes servies", "valeur": f.get("nb_commandes"), "format": "nombre"},
            {"label": "Destinataires", "valeur": f.get("nb_destinataires"), "format": "nombre"},
        ], "note": f"{l['libelle']} ; conservation {l['conservation']}"},
        {"type": "tableau", "titre": "Mouvements de stock", "colonnes": [
            {"cle": "type_mouvement", "label": "Type"}, {"cle": "code_mvt_sap", "label": "Code (SAP indicatif)"},
            {"cle": "nb", "label": "Nombre", "format": "nombre"},
            {"cle": "quantite", "label": "Quantité nette", "format": "nombre"},
            {"cle": "premier", "label": "Premier", "format": "datetime"},
            {"cle": "dernier", "label": "Dernier", "format": "datetime"}],
         "lignes": d["mouvements"]},
    ]
    if d["anomalies"]:
        sortie.append(_tableau_anomalies_liees(d["anomalies"]))
    return sortie


def _tableau_anomalies_liees(anomalies):
    anomalies = sorted(anomalies, key=lambda a: (RANG_SEVERITE.get(a.get("severite"), 9), a.get("regle_id") or ""))
    return {"type": "tableau", "titre": "Anomalies de fiabilité liées", "colonnes": [
        {"cle": "regle_id", "label": "Règle"}, {"cle": "libelle", "label": "Contrôle"},
        {"cle": "severite", "label": "Gravité", "format": "badge"},
        {"cle": "cle_ligne", "label": "Donnée", "format": "trace"},
        {"cle": "detail", "label": "Valeurs", "format": "json"}],
        "lignes": anomalies}


def artefacts_controles(d):
    res = d["regles_executees"]
    echecs = [r for r in res if r.get("statut_execution") == "ECHEC"]
    return [
        {"type": "indicateurs", "titre": "Contrôles exécutés", "items": [
            {"label": "Règles exécutées", "valeur": len(res)},
            {"label": "En échec", "valeur": len(echecs), "ton": "critique" if echecs else "ok"},
            {"label": "Durée", "valeur": f"{d['duree_s']} s"},
        ]},
        {"type": "tableau", "titre": "Résultat de l'exécution", "colonnes": [
            {"cle": "regle_id", "label": "Règle"}, {"cle": "libelle", "label": "Contrôle"},
            {"cle": "severite", "label": "Gravité", "format": "badge"},
            {"cle": "nb_anomalies", "label": "Anomalies", "format": "nombre"},
            {"cle": "statut_execution", "label": "Statut"}, {"cle": "message_erreur", "label": "Erreur"}],
         "lignes": res},
    ]


# ----------------------------------------------------------------------
# Artefacts KPI
# ----------------------------------------------------------------------
def fmt_kpi(v, unite):
    if v is None:
        return "n.d."
    if unite == "%":
        return f"{v:.2f} %".replace(".", ",")
    if unite == "‰":
        return f"{v:.2f} ‰".replace(".", ",")
    return f"{v:.1f} {unite}".replace(".", ",")


def tendance(v, prec, unite, sens):
    if v is None or prec is None:
        return None, "neutre"
    delta = v - prec
    favorable = delta >= 0 if sens == "HAUSSE_FAVORABLE" else delta <= 0
    seuil = 0.05 if unite in ("%", "‰") else 0.005 * abs(prec or 1)
    ton = "neutre" if abs(delta) < seuil else ("ok" if favorable else "alerte")
    if unite in ("%", "‰"):
        txt = f"{delta:+.2f} pt".replace(".", ",")
    else:
        txt = f"{delta:+.1f}".replace(".", ",")
    return txt, ton


def _jour_fr(iso):
    return _date_fr(iso)


def artefacts_kpi(r):
    p = r["periode"]
    defs = r["definitions"]
    res = r["resultats"]
    titre_periode = f"du {_jour_fr(p['debut'])} au {_jour_fr(p['fin'])}"
    sortie = []
    if r["axe"] == "GLOBAL":
        items = []
        for k, v in res.items():
            if not v["par_perimetre"]:
                continue
            g = v["par_perimetre"][0]
            d = defs[k]
            txt, ton = tendance(g["valeur"], g["valeur_precedente"], d["unite"], d["sens"])
            if k == "OTIF_BRUT":
                ton = "neutre"          # indicateur de diagnostic : sa hausse n'est pas une bonne nouvelle en soi
            items.append({"label": d["libelle"] or k, "valeur": fmt_kpi(g["valeur"], d["unite"]), "ton": ton,
                          "detail": f"{txt} vs période précédente" if txt else None, "kpi": k,
                          "action": "echecs" if k in ("OTIF", "ERREUR_PREPARATION") else None})
        sortie.append({"type": "indicateurs", "titre": f"Indicateurs {titre_periode}", "items": items,
                       "note": "Valeur de la période = somme des numérateurs / somme des dénominateurs (pas de moyenne de taux)."})
        if "OTIF" in res and "OTIF_BRUT" in res:
            sortie.append({"type": "courbes", "titre": "OTIF fiabilisé et OTIF brut", "unite": "%",
                           "granularite": p["granularite"], "series": [
                               {"nom": "OTIF fiabilisé", "points": [{"x": s["periode"], "y": s["valeur"]} for s in res["OTIF"]["serie"]]},
                               {"nom": "OTIF brut (non fiabilisé)", "points": [{"x": s["periode"], "y": s["valeur"]} for s in res["OTIF_BRUT"]["serie"]]}],
                           "note": "L'écart entre les deux courbes mesure l'effet des données non fiables sur l'indicateur."})
        autres = [k for k in res if k not in ("OTIF", "OTIF_BRUT")][:1] if "OTIF" not in res else []
        for k in autres:
            sortie.append(_courbe_kpi(k, res[k], defs[k], p["granularite"], {}))
    else:
        libelles = {pp["perimetre"]: pp["libelle"] for v in res.values() for pp in v["par_perimetre"]}
        kpis = list(res)
        lignes = {}
        for k in kpis:
            for pp in res[k]["par_perimetre"]:
                l = lignes.setdefault(pp["perimetre"], {"perimetre": pp["perimetre"], "libelle": pp["libelle"]})
                l[k] = pp["valeur"]                 # nombre brut : le tableau trie correctement
                obj = pp.get("objectif")
                if obj is not None and pp["valeur"] is not None:
                    l[f"objectif_{k}"] = obj
                    atteint = pp["valeur"] >= obj if defs[k]["sens"] == "HAUSSE_FAVORABLE" else pp["valeur"] <= obj
                    l[f"_ton_{k}"] = "ok" if atteint else "critique"
        colonnes = [{"cle": "libelle", "label": "Périmètre"}]
        for k in kpis:
            colonnes.append({"cle": k, "label": defs[k]["libelle"] or k, "format": "kpi", "unite": defs[k]["unite"],
                             "ton": f"_ton_{k}"})
            if any(f"objectif_{k}" in l for l in lignes.values()):
                colonnes.append({"cle": f"objectif_{k}", "label": f"Objectif {k}", "format": "kpi", "unite": defs[k]["unite"]})
        sortie.append({"type": "tableau", "titre": f"Ventilation par {r['axe'].lower()} {titre_periode}",
                       "colonnes": colonnes, "lignes": list(lignes.values()),
                       "action": {"type": "echecs", "cle": "perimetre", "axe": r["axe"]},
                       "note": "En rouge : objectif contractuel non atteint." if any("_ton_" in c for l in lignes.values() for c in l) else None})
        principal = next((k for k in ("OTIF", "ERREUR_PREPARATION", "RESPECT_CRENEAUX") if k in res), kpis[0])
        sortie.append(_courbe_kpi(principal, res[principal], defs[principal], p["granularite"], libelles))
    if r.get("kpi_non_disponibles_pour_cet_axe"):
        sortie.append({"type": "indicateurs", "titre": "KPI non disponibles pour cet axe", "items": [
            {"label": k, "valeur": "-"} for k in r["kpi_non_disponibles_pour_cet_axe"]]})
    return sortie


def _courbe_kpi(k, v, d, granularite, libelles):
    series = {}
    for s in v["serie"]:
        series.setdefault(s["perimetre"], []).append({"x": s["periode"], "y": s["valeur"]})
    return {"type": "courbes", "titre": f"{d['libelle'] or k} ({d['unite']})", "unite": d["unite"], "granularite": granularite,
            "series": [{"nom": libelles.get(p, "Site" if p == "TOUS" else p), "points": pts} for p, pts in series.items()]}


def artefacts_echecs(d, libelles=None):
    libelles = libelles or {}
    if d["kpi"] == "OTIF":
        return [
            {"type": "barres", "titre": "Causes des lignes en échec OTIF", "donnees": [
                {"label": K.CAUSES.get(c["cause"], c["cause"]), "valeur": c["nb"],
                 "ton": "critique" if c["cause"] in ("RUPTURE", "NON_LIVREE") else "alerte"} for c in d["resume"]]},
            {"type": "tableau", "titre": f"Lignes en échec ({len(d['lignes'])} plus récentes)", "colonnes": [
                {"cle": "ligne_id", "label": "Ligne", "format": "trace"}, {"cle": "donneur_ordre_id", "label": "Donneur"},
                {"cle": "transporteur_id", "label": "Transp."}, {"cle": "date_promise", "label": "Promis le", "format": "date"},
                {"cle": "date_livraison", "label": "Livré le", "format": "date"},
                {"cle": "retard_jours", "label": "Retard (j)", "format": "nombre"},
                {"cle": "qte_commandee", "label": "Cdé", "format": "nombre"}, {"cle": "qte_livree", "label": "Livré", "format": "nombre"},
                {"cle": "cause", "label": "Cause"}], "lignes": d["lignes"]},
        ]
    return [
        {"type": "barres", "titre": "Erreurs de préparation par équipe, contrat et ancienneté", "donnees": [
            {"label": f"{r['equipe']} / {r['type_contrat']} / {r['anciennete']}", "valeur": r["nb"],
             "ton": "critique" if r["anciennete"] == "moins de 30 jours" else "alerte"} for r in d["resume"]]},
        {"type": "tableau", "titre": f"Tâches en erreur ({len(d['lignes'])} plus récentes)", "colonnes": [
            {"cle": "tache_id", "label": "Tâche"}, {"cle": "commande_id", "label": "Commande", "format": "trace"},
            {"cle": "lot_id", "label": "Lot", "format": "trace"}, {"cle": "operateur_id", "label": "Opérateur"},
            {"cle": "equipe", "label": "Equipe"}, {"cle": "type_contrat", "label": "Contrat"},
            {"cle": "anciennete_jours", "label": "Ancienneté (j)", "format": "nombre"},
            {"cle": "debut", "label": "Date", "format": "datetime"}, {"cle": "resultat_controle", "label": "Erreur"}],
         "lignes": d["lignes"]},
    ]


def artefacts_definitions(defs):
    return [{"type": "tableau", "titre": "Définitions des KPI", "colonnes": [
        {"cle": "kpi_code", "label": "Code"}, {"cle": "version", "label": "Version"}, {"cle": "libelle", "label": "KPI"},
        {"cle": "formule", "label": "Formule"}, {"cle": "unite", "label": "Unité"}, {"cle": "description", "label": "Règles"}],
        "lignes": defs}]


# ----------------------------------------------------------------------
# Artefacts dérives et actions
# ----------------------------------------------------------------------
def fmt_valeur(v, unite):
    if v is None:
        return "n.d."
    return fmt_kpi(float(v), unite or "%")


def _libelle_perimetre(d):
    if d.get("perimetre_type") == "GLOBAL":
        return "Site"
    return d.get("perimetre_valeur")


def lignes_derives(derives):
    return [{
        "derive_id": d["derive_id"], "gravite": d["gravite"],
        "kpi": L.LIBELLES_KPI.get(d["kpi_code"], d["kpi_code"]), "perimetre": _libelle_perimetre(d),
        "debut": d["periode_debut"], "fin": d["periode_fin"],
        "valeur": fmt_valeur(d["valeur_observee"], d["unite"]), "reference": fmt_valeur(d["valeur_reference"], d["unite"]),
        "sigma": round(d["z_max"], 1) if d.get("z_max") is not None else None,
        "signaux": len(d.get("signaux") or []), "actions": d.get("nb_actions", 0),
        "statut": L.LIBELLES_STATUT.get(d["statut"], d["statut"]),
    } for d in derives]


def artefacts_derives(derives):
    ouvertes = [d for d in derives if d["statut"] in ("NOUVELLE", "EN_ANALYSE")]
    return [
        {"type": "indicateurs", "titre": "Dérives détectées", "items": [
            {"label": "Dérives ouvertes", "valeur": len(ouvertes), "ton": "alerte" if ouvertes else "ok"},
            {"label": "dont critiques", "valeur": sum(1 for d in ouvertes if d["gravite"] == "CRITIQUE"),
             "ton": "critique" if any(d["gravite"] == "CRITIQUE" for d in ouvertes) else "ok"},
            {"label": "Sans action", "valeur": sum(1 for d in ouvertes if not d.get("nb_actions")), "ton": "alerte"},
            {"label": "Traitées", "valeur": sum(1 for d in derives if d["statut"] == "TRAITEE"), "ton": "ok"},
        ], "note": "Cartes de contrôle hebdomadaires (p' de Laney, règles 3 sigma et 2 sur 3 au-delà de 2 sigma)."},
        {"type": "tableau", "titre": "Dérives (les plus graves et les plus récentes d'abord)", "colonnes": [
            {"cle": "gravite", "label": "Gravité", "format": "badge"}, {"cle": "kpi", "label": "KPI"},
            {"cle": "perimetre", "label": "Périmètre"}, {"cle": "debut", "label": "Du", "format": "date"},
            {"cle": "fin", "label": "Au", "format": "date"}, {"cle": "valeur", "label": "Valeur"},
            {"cle": "reference", "label": "Référence"}, {"cle": "sigma", "label": "Sigma", "format": "nombre"},
            {"cle": "signaux", "label": "Signaux liés", "format": "nombre"}, {"cle": "actions", "label": "Actions", "format": "nombre"},
            {"cle": "statut", "label": "Statut"}],
         "lignes": lignes_derives(derives), "action": {"type": "derive", "cle": "derive_id"}},
    ]


def artefact_carte(r):
    d = r["derive"]
    return {"type": "carte_controle", "unite": d["unite"], "sens": d["sens"],
            "titre": f"Carte de contrôle : {L.LIBELLES_KPI.get(d['kpi_code'], d['kpi_code'])} ({_libelle_perimetre(d)})",
            "fenetre": r["fenetre"],
            "points": [{"x": p["semaine"][:10], "valeur": p["valeur"], "centre": p["centre"], "lcs": p["lcs"], "lci": p["lci"],
                        "alerte": bool(p["alerte"])} for p in r["carte"]],
            "note": "Ligne centrale : référence des 12 semaines précédentes. Bande : limites à 3 sigma. Points rouges : alertes."}


def artefacts_analyse(r):
    d = r["derive"]
    m = r["mesure"]
    sortie = [
        {"type": "indicateurs", "titre": f"{d['derive_id']} du {_date_fr(r['fenetre']['debut'])} au {_date_fr(r['fenetre']['fin'])}",
         "items": [
             {"label": "Valeur pendant la dérive", "valeur": fmt_valeur(d["valeur_observee"], d["unite"]), "ton": "critique"},
             {"label": "Référence", "valeur": fmt_valeur(d["valeur_reference"], d["unite"])},
             {"label": "Limite de contrôle", "valeur": fmt_valeur(d["limite_controle"], d["unite"])},
             {"label": "Ecart maximal", "valeur": f"{d['z_max']:.1f} sigma".replace(".", ","),
              "ton": "critique" if d["gravite"] == "CRITIQUE" else "alerte"},
         ]},
        artefact_carte(r),
    ]
    for c in r["contributions"][:3]:
        if c["nb_segments"] < 2 or not c["exces_total"] or c["exces_total"] <= 0:
            continue
        sortie.append({"type": "barres", "titre": f"Où se concentre l'excès : {c['dimension']}", "donnees": [
            {"label": str(s["segment"])[:40], "valeur": round(s["exces"], 1),
             "ton": "critique" if (s["part_exces"] or 0) >= 50 else "alerte",
             "description": (f"{s['echecs_fenetre']:g} {m['echecs']} observés, {s['attendu']:g} attendus"
                             + (f" ({s['part_exces']:g} % de l'excès)" if s["part_exces"] is not None else ""))}
            for s in c["segments"] if s["exces"] > 0],
            "note": f"Excès = {m['echecs']} observés moins attendus au taux de référence du segment. Total : {c['exces_total']:g}."})
    if d.get("signaux"):
        sortie.append({"type": "tableau", "titre": "Signaux liés (même incident)", "colonnes": [
            {"cle": "kpi", "label": "KPI"}, {"cle": "perimetre", "label": "Périmètre"}, {"cle": "debut", "label": "Du", "format": "date"},
            {"cle": "valeur", "label": "Valeur"}, {"cle": "reference", "label": "Référence"}, {"cle": "z_max", "label": "Sigma"}],
            "lignes": [{"kpi": L.LIBELLES_KPI.get(x["kpi_code"], x["kpi_code"]),
                        "perimetre": "Site" if x["perimetre_type"] == "GLOBAL" else x["perimetre_valeur"],
                        "debut": x["debut"], "valeur": fmt_valeur(x["valeur_observee"], x.get("unite")),
                        "reference": fmt_valeur(x["valeur_reference"], x.get("unite")), "z_max": x["z_max"]} for x in d["signaux"]]})
    if r["actions"]:
        sortie.append(_tableau_actions(r["actions"], "Actions rattachées"))
    return sortie


VERDICTS = {"RETOUR_SOUS_CONTROLE": "Retour sous contrôle", "TOUJOURS_HORS_CONTROLE": "Toujours hors contrôle",
            "NON_MESURABLE": "Non mesurable"}


def _tableau_actions(actions, titre):
    lignes = []
    for a in actions:
        e = a.get("efficacite") or {}
        lignes.append({**{k: a.get(k) for k in ("action_id", "derive_id", "cause_identifiee", "action", "responsable", "echeance", "date_effet")},
                       "statut": L.LIBELLES_STATUT.get(a["statut"], a["statut"]),
                       "efficacite": VERDICTS.get(e.get("verdict"), "-"),
                       "valeur_apres": fmt_valeur(e.get("valeur_apres"), (a.get("derive") or {}).get("unite")) if e.get("valeur_apres") is not None else None})
    return {"type": "tableau", "titre": titre, "colonnes": [
        {"cle": "action", "label": "Action"}, {"cle": "cause_identifiee", "label": "Cause"},
        {"cle": "responsable", "label": "Responsable"}, {"cle": "echeance", "label": "Echéance", "format": "date"},
        {"cle": "statut", "label": "Statut"}, {"cle": "efficacite", "label": "Efficacité"},
        {"cle": "valeur_apres", "label": "KPI après"}], "lignes": lignes, "action": {"type": "action", "cle": "action_id"}}


def artefacts_actions(actions):
    aujourdhui = dt.date.today().isoformat()
    ouvertes = [a for a in actions if a["statut"] in ("A_FAIRE", "EN_COURS")]
    en_retard = [a for a in ouvertes if a.get("echeance") and str(a["echeance"])[:10] < aujourdhui]
    efficaces = [a for a in actions if (a.get("efficacite") or {}).get("verdict") == "RETOUR_SOUS_CONTROLE"]
    return [
        {"type": "indicateurs", "titre": "Plan d'actions", "items": [
            {"label": "Actions ouvertes", "valeur": len(ouvertes)},
            {"label": "En retard", "valeur": len(en_retard), "ton": "critique" if en_retard else "ok"},
            {"label": "Retour sous contrôle", "valeur": len(efficaces), "ton": "ok"},
        ]},
        _tableau_actions(actions, "Actions"),
    ]


# ----------------------------------------------------------------------
# Résultats compacts pour le modèle
# ----------------------------------------------------------------------
def compact_synthese(s):
    return {"indicateurs": s["indicateurs"],
            "regles": [{k: r.get(k) for k in ("regle_id", "libelle", "severite", "dimension", "nb_anomalies",
                                                "table_cible")} for r in s["regles"]]}


def compact_anomalies(d, n):
    return {"regle": d["regle"], "nb_renvoyees": len(d["anomalies"]), "anomalies": d["anomalies"][:n]}


def compact_trace(d, liste_max=25):
    return {k: (v[:liste_max] if isinstance(v, list) else v) for k, v in d.items()}


# ----------------------------------------------------------------------
class Outils:
    def __init__(self, entrepot):
        self.e = entrepot

    def executer(self, nom, args):
        """Renvoie (resultat_pour_modele, artefacts, erreur)."""
        args = args or {}
        try:
            if nom == "synthese_fiabilite":
                s = self.e.synthese()
                return compact_synthese(s), artefacts_synthese(s), None
            if nom == "detail_anomalies":
                n = max(1, min(int(args.get("limite") or 20), 50))
                d = self.e.anomalies(args.get("regle_id"), limite=max(n, 50))
                return compact_anomalies(d, n), artefacts_anomalies(d), None
            if nom == "tracer_commande":
                d = self.e.tracer_commande(args.get("identifiant"))
                return compact_trace(d), artefacts_commande(d), None
            if nom == "tracer_lot":
                d = self.e.tracer_lot(args.get("identifiant"))
                return compact_trace(d), artefacts_lot(d), None
            if nom == "indicateurs_kpi":
                r = K.calculer(self.e, args.get("kpis"), args.get("date_debut"), args.get("date_fin"),
                               args.get("granularite") or "mois", args.get("axe") or "global")
                return K.compact(r), artefacts_kpi(r), None
            if nom == "lignes_en_echec":
                d = self.echecs(args)
                return d, artefacts_echecs(d), None
            if nom == "definitions_kpi":
                defs = self.e.kpi_definitions()
                return {"definitions": defs}, artefacts_definitions(defs), None
            if nom == "derives_detectees":
                ds = L.tableau_derives(self.e)
                if args.get("statut"):
                    ds = [d for d in ds if d["statut"] == args["statut"]]
                if args.get("famille"):
                    ds = [d for d in ds if d["famille"] == args["famille"]]
                return {"nb_derives": len(ds), "derives": lignes_derives(ds)[:30]}, artefacts_derives(ds), None
            if nom == "analyser_derive":
                r = L.analyser(self.e, args.get("derive_id"))
                return L.compact_analyse(r), artefacts_analyse(r), None
            if nom == "plan_actions":
                acts = L.actions_avec_efficacite(self.e)
                compact = [{k: a.get(k) for k in ("action_id", "derive_id", "action", "responsable", "echeance", "statut")}
                           | {"efficacite": (a.get("efficacite") or {}).get("verdict")} for a in acts]
                return {"nb_actions": len(acts), "actions": compact[:30]}, artefacts_actions(acts), None
            if nom == "creer_action":
                a = L.creer_action(self.e, args, cree_par="copilote")
                return {"action_creee": a}, [_tableau_actions([a], "Action enregistrée")], None
            if nom == "lancer_controles":
                d = self.e.lancer_controles(args.get("regle_id"))
                return d, artefacts_controles(d), None
            return {"erreur": f"Outil inconnu : {nom}"}, [], f"Outil inconnu : {nom}"
        except ErreurDonnees as ex:
            return {"erreur": str(ex)}, [], str(ex)
        except Exception as ex:      # erreur technique (BigQuery indisponible, droits...)
            msg = f"Erreur technique lors de l'appel à {nom} : {type(ex).__name__}: {str(ex)[:300]}"
            return {"erreur": msg}, [], msg

    def echecs(self, args):
        k = (args.get("kpi") or "OTIF").upper()
        if k not in ("OTIF", "ERREUR_PREPARATION"):
            raise ErreurDonnees("Le détail ligne à ligne est disponible pour OTIF et ERREUR_PREPARATION.")
        _, debut, fin, _, axe, _ = K.valider([k], args.get("date_debut"), args.get("date_fin"), "mois",
                                             "global", self.e.kpi_bornes())
        axe = K.AXES.get((args.get("axe") or "global").lower(), "GLOBAL")
        valeur = (args.get("valeur") or "").strip().upper() or None
        if axe != "GLOBAL" and not valeur:
            axe = "GLOBAL"
        axes_ok = {"OTIF": ("GLOBAL", "DONNEUR", "TRANSPORTEUR"), "ERREUR_PREPARATION": ("GLOBAL", "EQUIPE")}[k]
        if axe not in axes_ok:
            raise ErreurDonnees(f"Pour {k}, le filtre possible est : {', '.join(a.lower() for a in axes_ok[1:])}.")
        limite = max(1, min(int(args.get("limite") or 20), 50))
        d = self.e.lignes_en_echec(k, debut, fin, axe, valeur, limite)
        d.update({"periode": {"debut": debut.isoformat(), "fin": fin.isoformat()}, "filtre": {"axe": axe, "valeur": valeur}})
        return d

    def executer_chrono(self, nom, args):
        t0 = time.time()
        resultat, artefacts, erreur = self.executer(nom, args)
        return resultat, artefacts, erreur, int((time.time() - t0) * 1000)
