"""
Mode démo hors ligne (COPILOTE_MODE=demo).

* EntrepotDemo : sert un instantané réel des résultats (fichier demo/instantane.json,
  extrait des données simulées), sans BigQuery.
* MoteurDemo : imite Gemini avec des règles simples (mots-clés) pour choisir
  un outil, puis rédige une réponse type à partir du résultat.

Sert à développer et tester l'interface sans réseau ni coût, et de solution
de secours si le projet Google Cloud n'est plus disponible. L'interface
affiche clairement qu'on est en mode démo.
"""
import copy
import datetime as dt
import json
import threading
import os
import re
from collections import Counter, defaultdict

from agent import ReponseModele
import kpi as K
from outils import _date_fr, fmt_kpi
from entrepot import (ErreurDonnees, construire_synthese, normaliser_action, normaliser_commande,
                      normaliser_derive, normaliser_lot, normaliser_regle)

FICHIER = os.path.join(os.path.dirname(__file__), "demo", "instantane.json")


class EntrepotDemo:
    def __init__(self, fichier=FICHIER):
        with open(fichier, encoding="utf-8") as f:
            self.d = json.load(f)

    def synthese(self):
        return construire_synthese(self.d["regles"], self.d["lignes_commande_total"],
                                   self.d["lignes_commande_en_anomalie"])

    def anomalies(self, regle_id, limite=50):
        regle_id = normaliser_regle(regle_id)
        regle = next((r for r in self.d["regles"] if r["regle_id"] == regle_id), None)
        if not regle:
            raise ErreurDonnees(f"La règle {regle_id} n'existe pas ou n'est pas active.")
        return {"regle": regle, "anomalies": self.d["anomalies"].get(regle_id, [])[:limite], "limite": limite}

    def lancer_controles(self, regle_id=None):
        regles = [r for r in self.d["regles"] if not regle_id or r["regle_id"] == normaliser_regle(regle_id)]
        return {"regles_executees": [{"regle_id": r["regle_id"], "libelle": r["libelle"], "severite": r["severite"],
                                      "dimension": r["dimension"], "nb_anomalies": r["nb_anomalies"],
                                      "statut_execution": "SUCCES", "message_erreur": None} for r in regles],
                "duree_s": 0.0}

    def tracer_commande(self, identifiant):
        cmd = normaliser_commande(identifiant)
        if cmd not in self.d["commandes"]:
            dispo = ", ".join(sorted(self.d["commandes"]))
            raise ErreurDonnees(f"La commande {cmd} n'est pas dans l'instantané de démo (disponibles : {dispo}).")
        return self.d["commandes"][cmd]

    def tracer_lot(self, identifiant):
        lot = normaliser_lot(identifiant)
        if lot not in self.d["lots"]:
            dispo = ", ".join(sorted(self.d["lots"]))
            raise ErreurDonnees(f"Le lot {lot} n'est pas dans l'instantané de démo (disponibles : {dispo}).")
        return self.d["lots"][lot]

    # ------------------------------------------------------------------
    # KPI : mêmes signatures que EntrepotBigQuery, calculs faits en Python
    # sur les lignes kpi_jour de l'instantané.
    # ------------------------------------------------------------------
    def _kpi(self):
        if "kpi" not in self.d:
            raise ErreurDonnees("L'instantané de démo ne contient pas les KPI.")
        return self.d["kpi"]

    def kpi_bornes(self):
        return self._kpi()["bornes"]

    def kpi_definitions(self):
        return self._kpi()["definitions"]

    def libelles_perimetres(self):
        return self._kpi()["libelles"]

    def objectifs(self):
        return self._kpi()["objectifs"]

    @staticmethod
    def _tronquer(jour, troncature):
        if troncature == "DAY":
            return jour
        if troncature == "MONTH":
            return jour.replace(day=1)
        return jour - dt.timedelta(days=jour.weekday())          # WEEK(MONDAY)

    def kpi_sommes(self, kpis, debut, fin, troncature, axe, debut_courant):
        d0, d1, dc = debut.isoformat(), fin.isoformat(), debut_courant.isoformat()
        cumuls = defaultdict(lambda: [0.0, 0.0])
        for jour, k, ptype, pval, num, den in self._kpi()["jour"]:
            if ptype != axe or k not in kpis or not (d0 <= jour <= d1):
                continue
            periode = self._tronquer(dt.date.fromisoformat(jour), troncature)
            c = cumuls[(periode, k, pval, jour >= dc)]
            c[0] += num
            c[1] += den
        return [{"periode": p, "kpi_code": k, "perimetre_valeur": v, "dans_periode": dans, "num": n, "den": de}
                for (p, k, v, dans), (n, de) in sorted(cumuls.items(), key=lambda x: (x[0][0], x[0][1], x[0][2]))]

    def lignes_en_echec(self, kpi, debut, fin, axe, valeur, limite):
        d0, d1 = debut.isoformat(), fin.isoformat()
        x = self._kpi()
        if kpi == "OTIF":
            col = {"DONNEUR": 1, "TRANSPORTEUR": 2}.get(axe)
            compte = Counter()
            for r in x["otif_causes"]:
                if d0 <= r[0] <= d1 and (col is None or r[col] == valeur):
                    compte[r[3]] += r[4]
            champ = {"DONNEUR": "donneur_ordre_id", "TRANSPORTEUR": "transporteur_id"}.get(axe)
            lignes = [l for l in x["otif_lignes"] if d0 <= l["date_promise"] <= d1 and (champ is None or l[champ] == valeur)]
            lignes.sort(key=lambda l: l["ligne_id"])
            lignes.sort(key=lambda l: l["date_promise"], reverse=True)
            return {"kpi": kpi, "resume": [{"cause": c, "nb": n} for c, n in compte.most_common()], "lignes": lignes[:limite],
                    "note": "Mode démo : lignes tirées d'un échantillon (les plus récentes de chaque mois)."}
        if kpi == "ERREUR_PREPARATION":
            taches = [t for t in x["erreurs_preparation"] if d0 <= t["jour_prod"] <= d1
                      and (axe != "EQUIPE" or t["equipe"] == valeur)]
            compte = Counter((t["equipe"], t["type_contrat"],
                              "moins de 30 jours" if (t["anciennete_jours"] or 0) < 30 else "30 jours et plus") for t in taches)
            lignes = sorted(taches, key=lambda t: t["debut"], reverse=True)[:limite]
            return {"kpi": kpi, "resume": [{"equipe": e, "type_contrat": c, "anciennete": a, "nb": n}
                                           for (e, c, a), n in compte.most_common()],
                    "lignes": [{k: v for k, v in t.items() if k != "jour_prod"} for t in lignes]}
        raise ErreurDonnees("Le détail ligne à ligne est disponible pour OTIF et ERREUR_PREPARATION.")


    # ------------------------------------------------------------------
    # Dérives et actions : lecture de l'instantané, écritures en mémoire
    # (perdues au redémarrage, comme l'indique le bandeau du mode démo)
    # ------------------------------------------------------------------
    def _lean(self):
        if "lean" not in self.d:
            raise ErreurDonnees("L'instantané de démo ne contient pas les dérives.")
        if not hasattr(self, "_etat_lean"):
            self._etat_lean = copy.deepcopy({"derives": self.d["lean"]["derives"], "actions": self.d["lean"]["actions"]})
            self._verrou_lean = threading.Lock()
        return self._etat_lean

    def lean_derives(self):
        etat = self._lean()
        nb = Counter(a["derive_id"] for a in etat["actions"])
        return [{**copy.deepcopy(d), "nb_actions": nb.get(d["derive_id"], 0)} for d in etat["derives"]]

    def lean_derive(self, derive_id):
        derive_id = normaliser_derive(derive_id)
        d = next((x for x in self.lean_derives() if x["derive_id"] == derive_id), None)
        if not d:
            raise ErreurDonnees(f"La dérive {derive_id} n'existe pas.")
        return d

    def lean_carte(self, kpi, perimetre_type, perimetre_valeur, debut, fin):
        d0, d1 = debut.isoformat(), fin.isoformat()
        cols = ("semaine", "kpi_code", "perimetre_type", "perimetre_valeur", "valeur", "centre", "lcs", "lci", "z_defav",
                "alerte", "denominateur")
        return [dict(zip(cols, r)) for r in self.d["lean"]["cartes"]
                if r[1] == kpi and r[2] == perimetre_type and r[3] == perimetre_valeur and d0 <= r[0] <= d1]

    def lean_segments(self, famille, kpi, perimetre_type, perimetre_valeur, d1, d2, b1, b2):
        derive = next((d for d in self._lean()["derives"] if d["kpi_code"] == kpi and d["perimetre_type"] == perimetre_type
                       and d["perimetre_valeur"] == perimetre_valeur and d["periode_debut"][:10] == d1.isoformat()), None)
        if not derive:
            raise ErreurDonnees("Analyse non disponible dans l'instantané de démo.")
        x = self.d["lean"]["segments"][derive["derive_id"]]
        return copy.deepcopy(x["segments"]), x["totaux"]

    def lean_actions(self):
        return copy.deepcopy(sorted(self._lean()["actions"], key=lambda a: a["cree_le"], reverse=True))

    @staticmethod
    def _maintenant():
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def lean_creer_action(self, a):
        etat = self._lean()
        ligne = {**{k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in a.items()},
                 "statut": "A_FAIRE", "commentaire": None, "cree_le": self._maintenant(), "cloturee_le": None,
                 "maj_le": self._maintenant()}
        with self._verrou_lean:
            etat["actions"].append(ligne)
        return copy.deepcopy(ligne)

    def lean_maj_action(self, action_id, champs):
        action_id = normaliser_action(action_id)
        etat = self._lean()
        with self._verrou_lean:
            a = next((x for x in etat["actions"] if x["action_id"] == action_id), None)
            if not a:
                raise ErreurDonnees(f"L'action {action_id} est introuvable.")
            a.update({k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in champs.items() if v is not None})
            if a["statut"] in ("FAITE", "EFFICACE", "NON_EFFICACE"):
                a["cloturee_le"] = a.get("cloturee_le") or self._maintenant()
            else:
                a["cloturee_le"] = None
            a["maj_le"] = self._maintenant()
            return copy.deepcopy(a)

    def lean_maj_derive(self, derive_id, champs):
        derive_id = normaliser_derive(derive_id)
        etat = self._lean()
        with self._verrou_lean:
            d = next((x for x in etat["derives"] if x["derive_id"] == derive_id), None)
            if not d:
                raise ErreurDonnees(f"La dérive {derive_id} est introuvable.")
            d.update({k: v for k, v in champs.items() if v is not None})
        return self.lean_derive(derive_id)

MOTS_REGLES = [
    (("fefo", "périm", "perim"), "DQ_CNF_001"), (("rappel",), "DQ_CNF_002"),
    (("décision", "decision", "personne responsable"), "DQ_CNF_003"), (("retour",), "DQ_CNF_004"),
    (("zone", "chambre froide"), "DQ_CNF_005"), (("excursion", "températ", "temperat"), "DQ_CNF_006"),
    (("écart de stock", "ecart de stock", "stock"), "DQ_COH_005"), (("doublon", "double"), "DQ_UNI_002"),
    (("date de livraison",), "DQ_COM_001"),
]


MOTS_KPI = [
    (("otif",), "OTIF"), (("taux de service",), "TAUX_SERVICE"), (("rupture",), "TAUX_RUPTURE"),
    (("non mesurable",), "LIGNES_NON_MESURABLES"), (("erreur de prépa", "erreurs de prépa", "erreur de prepa", "erreurs de prepa",
                                                     "erreur prépa", "qualité de prépa"), "ERREUR_PREPARATION"),
    (("productivit",), "PRODUCTIVITE_PICKING"), (("performance picking", "taux de performance", "cadence"), "PERFORMANCE_PICKING"),
    (("créneau", "creneau", "enlèvement", "enlevement"), "RESPECT_CRENEAUX"), (("réclamation", "reclamation"), "RECLAMATIONS_ENTREPOT"),
]
MOTS_AXES = [(("donneur", "laboratoire", "par labo"), "donneur"), (("transporteur",), "transporteur"), (("équipe", "equipe", "nuit"), "equipe")]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


class MoteurDemo:
    modele = "démo (sans Gemini)"

    def configurer(self, declarations):
        pass

    def generer(self, messages, avec_outils=True):
        dernier = messages[-1]
        if dernier["role"] == "user" and avec_outils:
            return ReponseModele(appels=[self._choisir(dernier["texte"])])
        if dernier["role"] == "outil":
            return ReponseModele(texte=self._rediger(dernier["reponses"][0]))
        return ReponseModele(texte="Mode démo : je ne peux répondre qu'aux questions sur la fiabilité des données, "
                                   "une commande (CMD...), un lot (LOT...) ou les indicateurs (OTIF, erreurs de préparation...).")

    def _choisir(self, q):
        ql = q.lower()
        m = re.search(r"\bCMD\d+(?:-\d+)?\b", q.upper())
        if m:
            return {"nom": "tracer_commande", "args": {"identifiant": m.group(0)}}
        m = re.search(r"\bLOT\d+\b", q.upper())
        if m:
            return {"nom": "tracer_lot", "args": {"identifiant": m.group(0)}}
        if "relanc" in ql or "lance les contr" in ql:
            return {"nom": "lancer_controles", "args": {}}
        m = re.search(r"DQ_[A-Z]{3}_\d{3}", q.upper())
        if m:
            return {"nom": "detail_anomalies", "args": {"regle_id": m.group(0)}}
        m = re.search(r"\bDER-[0-9A-F]{8}\b", q.upper())
        if m:
            return {"nom": "analyser_derive", "args": {"derive_id": m.group(0)}}
        if any(x in ql for x in ("plan d'action", "plan d’action", "actions correctives", "mes actions", "efficacité des actions")):
            return {"nom": "plan_actions", "args": {}}
        if any(x in ql for x in ("dérive", "derive", "dérapage", "hors contrôle", "anormal", "routine")):
            return {"nom": "derives_detectees", "args": {}}
        kpi = self._choisir_kpi(ql)
        if kpi:
            return kpi
        if "lot rappel" in ql:
            return {"nom": "tracer_lot", "args": {"identifiant": "LOT005075"}}
        for mots, regle in MOTS_REGLES:
            if any(x in ql for x in mots):
                return {"nom": "detail_anomalies", "args": {"regle_id": regle}}
        return {"nom": "synthese_fiabilite", "args": {}}

    def _choisir_kpi(self, ql):
        if "définition" in ql or "definition" in ql or "comment est calcul" in ql:
            return {"nom": "definitions_kpi", "args": {}}
        kpis = [k for mots, k in MOTS_KPI if any(m in ql for m in mots)]
        if not kpis and not any(m in ql for m in ("kpi", "indicateur", "performance", "tableau de bord")):
            return None
        args = {}
        mois = next((i for i, m in enumerate(MOIS, 1) if m in ql), None)
        if mois:
            annee = 2025 if mois >= 10 else 2026
            debut = dt.date(annee, mois, 1)
            fin = (debut.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            args.update(date_debut=debut.isoformat(), date_fin=fin.isoformat())
        elif "3 derniers mois" in ql or "trois derniers mois" in ql or "trimestre" in ql:
            args.update(date_debut="2026-07-01", date_fin="2026-09-30")
        elif "année" in ql or "annee" in ql or "12 mois" in ql or "évolution" in ql or "evolution" in ql:
            args.update(date_debut="2025-10-01", date_fin="2026-09-30")
        axe = next((a for mots, a in MOTS_AXES if any(m in ql for m in mots)), None)
        if any(m in ql for m in ("pourquoi", "cause", "en échec", "en echec", "explique", "détail des lignes")):
            k = "ERREUR_PREPARATION" if "ERREUR_PREPARATION" in kpis else "OTIF"
            return {"nom": "lignes_en_echec", "args": {"kpi": k, **args}}
        if kpis:
            if "OTIF" in kpis and "OTIF_BRUT" not in kpis:
                kpis.append("OTIF_BRUT")
            args["kpis"] = kpis
        if axe:
            args["axe"] = axe
        args["granularite"] = "semaine" if mois else "mois"
        return {"nom": "indicateurs_kpi", "args": args}

    def _rediger(self, rep):
        r = rep["resultat"]
        if "erreur" in r:
            return f"Je n'ai pas pu obtenir l'information : {r['erreur']}"
        nom = rep["nom"]
        if nom == "synthese_fiabilite":
            i = r["indicateurs"]
            crit = [x for x in r["regles"] if x["severite"] == "CRITIQUE" and x["nb_anomalies"]]
            lignes = "\n".join(f"- **{x['regle_id']}** {x['libelle']} : {x['nb_anomalies']}" for x in crit)
            return (f"**{i['nb_regles_en_anomalie']} règles sur {i['nb_regles_executees']}** remontent des anomalies, "
                    f"dont **{i['nb_anomalies_critiques']} anomalies critiques** au regard des BPD :\n{lignes}\n\n"
                    f"Côté pilotage, **{str(i['lignes_commande_fiables_pct']).replace('.', ',')} %** des lignes de commande "
                    f"sont sans anomalie : les KPI calculés sur les autres seraient faussés. Voir le détail dans le panneau.\n\n"
                    f"*Réponse générée en mode démo, sans Gemini.*")
        if nom == "detail_anomalies":
            g = r["regle"]
            return (f"La règle **{g['regle_id']}** ({g['libelle']}, gravité {g['severite']}) signale "
                    f"**{g['nb_anomalies']} anomalies**. {g.get('description') or ''}\n\nLes premières sont listées "
                    f"dans le panneau ; cliquez sur une donnée pour la tracer.\n\n*Réponse générée en mode démo, sans Gemini.*")
        if nom == "tracer_commande":
            c = r["commande"]
            a = r["anomalies"]
            s = (f"La commande **{c['commande_id']}** ({c.get('donneur_ordre')}, pour {c.get('destinataire')}) est au statut "
                 f"**{c['statut_commande']}**, avec {len(r['lignes'])} ligne(s).")
            s += (f" Elle est concernée par **{len(a)} anomalie(s)** : " + ", ".join(sorted({x['regle_id'] for x in a})) + "."
                  if a else " Aucune anomalie de fiabilité ne la concerne.")
            return s + "\n\n*Réponse générée en mode démo, sans Gemini.*"
        if nom == "tracer_lot":
            l = r["lot"]
            f = r["diffusion"]
            s = f"Le lot **{l['lot_id']}** ({l['libelle']}) est au statut **{l['statut_qualite']}**."
            if l["rappele"]:
                s += (f" Il a été **rappelé le {_date_fr(l['date_rappel'])}**. Il a servi {f['nb_commandes']} commandes "
                      f"pour {f['nb_destinataires']} destinataires, et {len(r['anomalies'])} prélèvement(s) ou anomalie(s) "
                      f"sont signalés après le rappel : chaque boîte concernée doit être retrouvée.")
            return s + "\n\n*Réponse générée en mode démo, sans Gemini.*"
        if nom == "indicateurs_kpi":
            p = r["periode"]
            lignes = []
            for k, v in r["resultats"].items():
                d = r["definitions"][k]
                for pp in v["par_perimetre"][:4]:
                    s = f"- **{d['libelle']}** ({k} v{d['version']})"
                    if r["axe"] != "GLOBAL":
                        s += f", {pp['libelle']}"
                    s += f" : {fmt_kpi(pp['valeur'], d['unite'])}"
                    if pp["valeur_precedente"] is not None:
                        s += f" (période précédente : {fmt_kpi(pp['valeur_precedente'], d['unite'])})"
                    if pp.get("objectif") is not None:
                        s += f", objectif {fmt_kpi(pp['objectif'], d['unite'])}"
                    lignes.append(s)
            texte = (f"Du {_date_fr(p['debut'])} au {_date_fr(p['fin'])} :\n" + "\n".join(lignes))
            res = r["resultats"]
            if "OTIF" in res and "OTIF_BRUT" in res and res["OTIF"]["par_perimetre"] and res["OTIF_BRUT"]["par_perimetre"]:
                a, b = res["OTIF"]["par_perimetre"][0]["valeur"], res["OTIF_BRUT"]["par_perimetre"][0]["valeur"]
                if a is not None and b is not None and abs(b - a) >= 0.5:
                    texte += (f"\n\nL'OTIF brut surestime la performance de **{abs(b - a):.2f} point(s)**".replace(".", ",")
                              + " : il compte les doublons et ignore les ruptures.")
            return texte + "\n\n*Réponse générée en mode démo, sans Gemini.*"
        if nom == "lignes_en_echec":
            res = r["resume"]
            total = sum(x["nb"] for x in res) or 1
            if r["kpi"] == "OTIF":
                det = ", ".join(f"{K.CAUSES.get(x['cause'], x['cause']).lower()} {100 * x['nb'] / total:.0f} %" for x in res[:3])
                texte = f"Sur la période, **{total} lignes** ont manqué l'OTIF : {det}."
            else:
                top = res[0] if res else None
                texte = f"**{total} tâches** de préparation en erreur sur la période."
                if top:
                    texte += (f" Première catégorie : équipe {top['equipe']}, contrat {top['type_contrat']}, "
                              f"ancienneté {top['anciennete']} ({100 * top['nb'] / total:.0f} %).")
            return texte + " Le détail ligne à ligne est dans le panneau.\n\n*Réponse générée en mode démo, sans Gemini.*"
        if nom == "definitions_kpi":
            return (f"{len(r['definitions'])} KPI sont définis, chacun avec une version : voir le tableau."
                    "\n\n*Réponse générée en mode démo, sans Gemini.*")
        if nom == "derives_detectees":
            ds = r["derives"]
            ouvertes = [d for d in ds if d["statut"] in ("Nouvelle", "En analyse")]
            crit = [d for d in ouvertes if d["gravite"] == "CRITIQUE"][:3]
            lignes = "\n".join(f"- **{d['derive_id']}** {d['kpi']} ({d['perimetre']}) du {_date_fr(d['debut'])} au {_date_fr(d['fin'])} : "
                               f"{d['valeur']} contre {d['reference']} ({str(d['sigma']).replace('.', ',')} sigma)" for d in crit)
            return (f"**{len(ouvertes)} dérives ouvertes** sur {r['nb_derives']} détectées. Les plus graves :\n{lignes}\n\n"
                    "Cliquez sur une dérive pour voir sa carte de contrôle et où elle se concentre."
                    "\n\n*Réponse générée en mode démo, sans Gemini.*")
        if nom == "analyser_derive":
            d = r["derive"]
            v = lambda x, n=2: f"{x:.{n}f}".replace(".", ",")
            texte = (f"**{d['derive_id']}** : {d['kpi_code']} à {v(d['valeur_observee'])} {d['unite']} contre "
                     f"{v(d['valeur_reference'])} {d['unite']} de référence, du {_date_fr(d['periode_debut'])} au "
                     f"{_date_fr(d['periode_fin'])} ({v(d['z_max'], 1)} sigma).")
            c = next((x for x in r["contributions"] if x["segments"] and x["part_du_premier_segment_pct"]), None)
            if c:
                s0 = c["segments"][0]
                texte += (f"\n\nL'excès se concentre sur **{c['dimension'].lower()} = {s0['segment']}** : "
                          f"{v(s0['part_exces'], 0)} % de l'excès ({v(s0['echecs_fenetre'], 0)} observés pour {v(s0['attendu'], 1)} attendus). "
                          "C'est une piste à confirmer sur le terrain, pas encore une cause prouvée.")
            return texte + "\n\n*Réponse générée en mode démo, sans Gemini.*"
        if nom == "plan_actions":
            return (f"{r['nb_actions']} action(s) au plan. L'efficacité est mesurée sur les 4 semaines suivant la date d'effet : "
                    "voir le tableau.\n\n*Réponse générée en mode démo, sans Gemini.*")
        if nom == "creer_action":
            return "Action enregistrée (en mémoire, mode démo).\n\n*Réponse générée en mode démo, sans Gemini.*"
        if nom == "lancer_controles":
            return (f"{len(r['regles_executees'])} règles exécutées (simulation en mode démo, aucune requête réelle).")
        return "Résultat disponible dans le panneau."
