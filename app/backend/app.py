"""
API REST du copilote (Flask).

Endpoints :
  GET  /api/etat                         état de l'application (mode, modèle, historique)
  POST /api/chat                         {question, conversation_id?} -> réponse de l'agent
  GET  /api/conversations                liste des conversations
  GET  /api/conversations/<id>           messages d'une conversation
  GET  /api/fiabilite/synthese           bilan des contrôles
  GET  /api/fiabilite/anomalies?regle=   détail d'une règle
  POST /api/fiabilite/lancer             {regle_id?} relance les contrôles
  GET  /api/tracer/commande/<id>         traçabilité d'une commande
  GET  /api/tracer/lot/<id>              traçabilité d'un lot
  GET  /api/kpi/bornes                   période couverte par les KPI
  GET  /api/kpi/tableau?debut&fin&granularite&axe&kpis   indicateurs + artefacts
  GET  /api/kpi/echecs?kpi&debut&fin&axe&valeur          lignes en échec d'un KPI
  GET  /api/kpi/definitions              définitions versionnées
  GET  /api/lean/derives                 dérives détectées
  GET  /api/lean/derives/<id>            analyse d'une dérive (carte de contrôle, contributions, actions)
  PATCH /api/lean/derives/<id>           {statut?, analyse_causes?}
  GET  /api/lean/actions                 plan d'actions avec efficacité
  POST /api/lean/actions                 {derive_id, cause_identifiee, action, responsable, echeance, date_effet?}
  PATCH /api/lean/actions/<id>           {statut?, commentaire?, responsable?, echeance?, date_effet?}
  GET  /api/prevision?equipe=            prévision de charge + besoin en renfort
  GET  /api/prevision/qualite            backtest (WAPE modèle contre méthode naïve)

En production, Flask sert aussi l'application React compilée (dossier static/).
Lancement local : python app.py  (ou : flask --app app run --port 8080)
"""
import datetime as dt
import logging
import os
import threading

from flask import Flask, jsonify, request, send_from_directory

import config as C
import kpi as K
import lean as L
import prevision as P
import outils as O
from agent import Agent, ErreurModele
from entrepot import ErreurDonnees
from historique import HistoriqueResilient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

DOSSIER_FRONT = os.getenv("DOSSIER_FRONT", os.path.join(os.path.dirname(__file__), "static"))


def creer_app(entrepot=None, moteur=None, historique=None):
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False

    if entrepot is None or moteur is None:
        if C.MODE == "demo":
            from demo import EntrepotDemo, MoteurDemo
            entrepot = entrepot or EntrepotDemo()
            moteur = moteur or MoteurDemo()
        else:
            from agent import MoteurGemini
            from entrepot import EntrepotBigQuery
            entrepot = entrepot or EntrepotBigQuery()
            moteur = moteur or MoteurGemini()
    historique = historique or HistoriqueResilient(essayer_firestore=(C.MODE != "demo"))
    outils = O.Outils(entrepot)
    agent = Agent(moteur, outils, O.DECLARATIONS)

    compteur = {"jour": dt.date.today(), "n": 0}
    verrou = threading.Lock()

    def consommer_quota():
        with verrou:
            if compteur["jour"] != dt.date.today():
                compteur.update(jour=dt.date.today(), n=0)
            if compteur["n"] >= C.MAX_QUESTIONS_PAR_JOUR:
                return False
            compteur["n"] += 1
            return True

    def erreur(message, code):
        return jsonify({"erreur": message}), code

    @app.before_request
    def controle_acces():
        if request.path.startswith("/api/") and request.path != "/api/etat" and C.CODE_ACCES:
            if request.headers.get("X-Code-Acces") != C.CODE_ACCES:
                return erreur("Code d'accès manquant ou incorrect.", 401)

    @app.errorhandler(ErreurDonnees)
    def _err_donnees(ex):
        return erreur(str(ex), 400)

    @app.errorhandler(Exception)
    def _err(ex):
        if hasattr(ex, "code") and isinstance(getattr(ex, "code"), int) and 400 <= ex.code < 500:
            return erreur(getattr(ex, "description", str(ex)), ex.code)
        log.exception("Erreur non gérée")
        return erreur(f"Erreur interne : {type(ex).__name__}: {str(ex)[:300]}", 500)

    # ------------------------------------------------------------------
    @app.get("/api/etat")
    def etat():
        return jsonify({
            "mode": C.MODE, "modele": getattr(moteur, "modele", "?"), "projet": C.PROJET or None,
            "historique": historique.nom, "historique_secours": historique.raison_secours,
            "code_acces_requis": bool(C.CODE_ACCES),
            "questions_restantes_aujourdhui": max(0, C.MAX_QUESTIONS_PAR_JOUR - compteur["n"]),
            "outils": [d["name"] for d in O.DECLARATIONS],
        })

    @app.post("/api/chat")
    def chat():
        corps = request.get_json(silent=True) or {}
        question = (corps.get("question") or "").strip()
        conv_id = corps.get("conversation_id") or None
        if not question:
            return erreur("La question est vide.", 400)
        if len(question) > C.MAX_LONGUEUR_QUESTION:
            return erreur(f"Question trop longue (maximum {C.MAX_LONGUEUR_QUESTION} caractères).", 400)
        if not consommer_quota():
            return erreur("Le nombre maximal de questions pour aujourd'hui est atteint (protection des coûts).", 429)
        passe = []
        if conv_id:
            msgs = historique.lire(conv_id)
            if msgs is None:
                conv_id = None
            else:
                passe = [{"role": m["role"], "texte": m["texte"]} for m in msgs]
        try:
            rep = agent.repondre(question, passe)
        except ErreurModele as ex:
            return erreur(str(ex), 503 if ex.code in (429, None) else 502)
        conv_id = historique.ajouter_echange(conv_id, question, rep)
        return jsonify({"conversation_id": conv_id, **rep})

    @app.get("/api/conversations")
    def conversations():
        return jsonify(historique.lister())

    @app.get("/api/conversations/<conv_id>")
    def conversation(conv_id):
        msgs = historique.lire(conv_id)
        if msgs is None:
            return erreur("Conversation introuvable.", 404)
        return jsonify(msgs)

    @app.get("/api/fiabilite/synthese")
    def synthese():
        s = entrepot.synthese()
        return jsonify({**s, "artefacts": O.artefacts_synthese(s)})

    @app.get("/api/fiabilite/anomalies")
    def anomalies():
        d = entrepot.anomalies(request.args.get("regle", ""), request.args.get("limite", 100, type=int))
        return jsonify({**d, "artefacts": O.artefacts_anomalies(d)})

    @app.post("/api/fiabilite/lancer")
    def lancer():
        corps = request.get_json(silent=True) or {}
        d = entrepot.lancer_controles(corps.get("regle_id") or None)
        return jsonify({**d, "artefacts": O.artefacts_controles(d)})

    @app.get("/api/tracer/commande/<identifiant>")
    def tracer_commande(identifiant):
        d = entrepot.tracer_commande(identifiant)
        return jsonify({**d, "artefacts": O.artefacts_commande(d)})

    @app.get("/api/tracer/lot/<identifiant>")
    def tracer_lot(identifiant):
        d = entrepot.tracer_lot(identifiant)
        return jsonify({**d, "artefacts": O.artefacts_lot(d)})

    @app.get("/api/kpi/bornes")
    def kpi_bornes():
        b = entrepot.kpi_bornes()
        d, f = K.periode_par_defaut(b)
        return jsonify({"debut": str(b["debut"])[:10], "fin": str(b["fin"])[:10],
                        "defaut": {"debut": d.isoformat(), "fin": f.isoformat()},
                        "axes": {a: sorted(k for k in K.TOUS_KPI if c in K.AXES_PAR_KPI[k]) for a, c in K.AXES.items()}})

    @app.get("/api/kpi/tableau")
    def kpi_tableau():
        a = request.args
        kpis = [k for k in (a.get("kpis") or "").split(",") if k.strip()] or None
        r = K.calculer(entrepot, kpis, a.get("debut") or None, a.get("fin") or None,
                       a.get("granularite") or "mois", a.get("axe") or "global")
        return jsonify({**r, "artefacts": O.artefacts_kpi(r)})

    @app.get("/api/kpi/echecs")
    def kpi_echecs():
        a = request.args
        d = outils.echecs({"kpi": a.get("kpi"), "date_debut": a.get("debut"), "date_fin": a.get("fin"),
                           "axe": a.get("axe"), "valeur": a.get("valeur"), "limite": a.get("limite", 50, type=int)})
        return jsonify({**d, "artefacts": O.artefacts_echecs(d)})

    @app.get("/api/kpi/definitions")
    def kpi_definitions():
        defs = entrepot.kpi_definitions()
        return jsonify({"definitions": defs, "artefacts": O.artefacts_definitions(defs)})

    @app.get("/api/lean/derives")
    def lean_derives():
        ds = L.tableau_derives(entrepot)
        return jsonify({"derives": ds, "artefacts": O.artefacts_derives(ds)})

    @app.get("/api/lean/derives/<derive_id>")
    def lean_derive(derive_id):
        r = L.analyser(entrepot, derive_id)
        return jsonify({**r, "artefacts": O.artefacts_analyse(r)})

    @app.patch("/api/lean/derives/<derive_id>")
    def lean_maj_derive(derive_id):
        return jsonify(L.modifier_derive(entrepot, derive_id, request.get_json(silent=True) or {}))

    @app.get("/api/lean/actions")
    def lean_actions():
        acts = L.actions_avec_efficacite(entrepot)
        return jsonify({"actions": acts, "artefacts": O.artefacts_actions(acts)})

    @app.post("/api/lean/actions")
    def lean_creer_action():
        return jsonify(L.creer_action(entrepot, request.get_json(silent=True) or {}, cree_par="interface")), 201

    @app.patch("/api/lean/actions/<action_id>")
    def lean_maj_action(action_id):
        return jsonify(L.modifier_action(entrepot, action_id, request.get_json(silent=True) or {}))

    @app.get("/api/prevision")
    def prevision():
        equipe = request.args.get("equipe", "TOUS")
        t = P.tableau(entrepot, equipe)
        s = P.serie(entrepot, equipe)
        return jsonify({**t, "serie": s, "qualite": P.qualite(entrepot),
                        "artefacts": O.artefacts_prevision(t, P.qualite(entrepot), s)})

    @app.get("/api/prevision/qualite")
    def prevision_qualite():
        return jsonify({"qualite": P.qualite(entrepot), "modele": entrepot.prevision_modele()})

    # ------------------------------------------------------------------
    # Application React compilée (production). En développement, c'est le
    # serveur Vite (npm run dev) qui sert l'interface et relaie /api ici.
    @app.get("/")
    @app.get("/<path:chemin>")
    def front(chemin=""):
        if chemin.startswith("api/"):
            return erreur("Endpoint inconnu.", 404)
        if chemin and os.path.isfile(os.path.join(DOSSIER_FRONT, chemin)):
            return send_from_directory(DOSSIER_FRONT, chemin)
        if os.path.isfile(os.path.join(DOSSIER_FRONT, "index.html")):
            return send_from_directory(DOSSIER_FRONT, "index.html")
        return ("Interface non compilée. En développement, lancez 'npm run dev' dans frontend/ "
                "et ouvrez http://localhost:5173", 200)

    return app


if __name__ == "__main__":
    creer_app().run(host="0.0.0.0", port=C.PORT, debug=False)
