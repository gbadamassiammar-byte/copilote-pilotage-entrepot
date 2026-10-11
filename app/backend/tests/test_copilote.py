"""
Tests automatiques (sans réseau) : python -m pytest -q   depuis backend/

Ils vérifient la boucle de l'agent, les garde-fous, les outils, l'API REST et
la conversion vers le format Gemini, avec des moteurs factices.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["COPILOTE_MODE"] = "demo"

import pytest

import config as C
import outils as O
from agent import Agent, ErreurModele, ReponseModele
from app import creer_app
from demo import EntrepotDemo, MoteurDemo
from entrepot import ErreurDonnees, normaliser_commande
from historique import HistoriqueResilient


class MoteurScripte:
    """Moteur factice : rejoue une liste de réponses et mémorise ce qu'il reçoit."""
    modele = "scripte"

    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.recus = []

    def configurer(self, declarations):
        self.declarations = declarations

    def generer(self, messages, avec_outils=True):
        self.recus.append((list(messages), avec_outils))
        r = self.reponses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def entrepot():
    return EntrepotDemo()


def test_boucle_appelle_outil_puis_repond(entrepot):
    m = MoteurScripte([ReponseModele(appels=[{"nom": "synthese_fiabilite", "args": {}}], jetons_entree=1000),
                       ReponseModele(texte="Voici le bilan.", jetons_entree=2000, jetons_sortie=100)])
    r = Agent(m, O.Outils(entrepot), O.DECLARATIONS).repondre("Mes données sont-elles fiables ?")
    assert r["reponse"] == "Voici le bilan."
    assert [t["outil"] for t in r["outils"]] == ["synthese_fiabilite"]
    assert {a["type"] for a in r["artefacts"]} == {"indicateurs", "barres", "tableau"}
    assert r["usage"]["jetons_entree"] == 3000 and r["usage"]["cout_estime_usd"] > 0
    # le résultat de l'outil a bien été renvoyé au modèle
    assert m.recus[1][0][-1]["role"] == "outil"


def test_plusieurs_outils_dans_le_meme_tour(entrepot):
    m = MoteurScripte([ReponseModele(appels=[{"nom": "detail_anomalies", "args": {"regle_id": "DQ_CNF_002"}},
                                             {"nom": "tracer_lot", "args": {"identifiant": "LOT005075"}}]),
                       ReponseModele(texte="ok")])
    r = Agent(m, O.Outils(entrepot), O.DECLARATIONS).repondre("lot rappelé ?")
    assert len(r["outils"]) == 2 and all(t["erreur"] is None for t in r["outils"])


def test_limite_d_etapes_force_une_reponse_texte(entrepot):
    boucle = [ReponseModele(appels=[{"nom": "synthese_fiabilite", "args": {}}])] * (C.MAX_ETAPES_AGENT - 1)
    m = MoteurScripte(boucle + [ReponseModele(texte="Réponse finale.")])
    r = Agent(m, O.Outils(entrepot), O.DECLARATIONS).repondre("question")
    assert r["reponse"] == "Réponse finale."
    assert m.recus[-1][1] is False          # dernière étape : outils désactivés
    assert len(m.recus) == C.MAX_ETAPES_AGENT


def test_erreur_d_outil_renvoyee_au_modele_sans_planter(entrepot):
    m = MoteurScripte([ReponseModele(appels=[{"nom": "tracer_commande", "args": {"identifiant": "'; DROP TABLE x"}}]),
                       ReponseModele(texte="Identifiant invalide.")])
    r = Agent(m, O.Outils(entrepot), O.DECLARATIONS).repondre("trace")
    assert r["outils"][0]["erreur"] and "n'est pas un numéro de commande valide" in r["outils"][0]["erreur"]
    assert "erreur" in m.recus[1][0][-1]["reponses"][0]["resultat"]


def test_outil_inconnu(entrepot):
    res, arts, err = O.Outils(entrepot).executer("supprimer_tout", {})
    assert err and arts == []


def test_validation_identifiants():
    assert normaliser_commande(" cmd00001057-00010 ") == "CMD00001057"
    for mauvais in ["CMD1", "LOT005075", "1 OR 1=1", ""]:
        with pytest.raises(ErreurDonnees):
            normaliser_commande(mauvais)


def test_historique_transmis_et_borne(entrepot):
    m = MoteurScripte([ReponseModele(texte="ok")])
    histo = [{"role": "user" if i % 2 == 0 else "model", "texte": f"m{i}"} for i in range(40)]
    Agent(m, O.Outils(entrepot), O.DECLARATIONS).repondre("suite", histo)
    envoyes = m.recus[0][0]
    assert len(envoyes) == 2 * C.HISTORIQUE_TOURS + 1 and envoyes[-1]["texte"] == "suite"


def test_moteur_demo_de_bout_en_bout(entrepot):
    a = Agent(MoteurDemo(), O.Outils(entrepot), O.DECLARATIONS)
    assert "règles sur" in a.repondre("Mes données sont-elles fiables ?")["reponse"]
    r = a.repondre("Que sais-tu du lot rappelé ?")
    assert r["outils"][0]["outil"] == "tracer_lot" and "rappelé" in r["reponse"]


def test_synthese_coherente(entrepot):
    s = entrepot.synthese()
    i = s["indicateurs"]
    assert i["nb_regles"] == 21 and 0 < i["lignes_commande_fiables_pct"] < 100
    assert i["nb_anomalies_critiques"] == sum(r["nb_anomalies"] for r in s["regles"] if r["severite"] == "CRITIQUE")


# ---------------------------------------------------------------- API REST
@pytest.fixture
def client(entrepot):
    app = creer_app(entrepot=entrepot, moteur=MoteurDemo(), historique=HistoriqueResilient(essayer_firestore=False))
    return app.test_client()


def test_api_chat_et_historique(client):
    r = client.post("/api/chat", json={"question": "Mes données sont-elles fiables ?"})
    assert r.status_code == 200
    d = r.get_json()
    cid = d["conversation_id"]
    r2 = client.post("/api/chat", json={"question": "Et le FEFO ?", "conversation_id": cid}).get_json()
    assert r2["conversation_id"] == cid
    msgs = client.get(f"/api/conversations/{cid}").get_json()
    assert [m["role"] for m in msgs] == ["user", "model", "user", "model"]
    assert client.get("/api/conversations").get_json()[0]["id"] == cid


def test_api_validations(client):
    assert client.post("/api/chat", json={"question": "  "}).status_code == 400
    assert client.post("/api/chat", json={"question": "x" * 5000}).status_code == 400
    assert client.get("/api/fiabilite/anomalies?regle=XX").status_code == 400
    assert client.get("/api/tracer/lot/LOT999999").status_code == 400
    assert client.get("/api/inexistant").status_code == 404


def test_api_endpoints_donnees(client):
    assert len(client.get("/api/fiabilite/synthese").get_json()["regles"]) == 21
    assert client.get("/api/fiabilite/anomalies?regle=DQ_CNF_002&limite=5").get_json()["regle"]["nb_anomalies"] > 0
    assert client.get("/api/tracer/lot/LOT005075").get_json()["lot"]["rappele"] is True
    assert client.post("/api/fiabilite/lancer", json={}).status_code == 200


def test_code_acces(entrepot, monkeypatch):
    monkeypatch.setattr(C, "CODE_ACCES", "secret")
    app = creer_app(entrepot=entrepot, moteur=MoteurDemo(), historique=HistoriqueResilient(essayer_firestore=False))
    c = app.test_client()
    assert c.get("/api/etat").status_code == 200                       # l'état reste public
    assert c.get("/api/fiabilite/synthese").status_code == 401
    assert c.get("/api/fiabilite/synthese", headers={"X-Code-Acces": "secret"}).status_code == 200


def test_quota_journalier(entrepot, monkeypatch):
    monkeypatch.setattr(C, "MAX_QUESTIONS_PAR_JOUR", 1)
    app = creer_app(entrepot=entrepot, moteur=MoteurDemo(), historique=HistoriqueResilient(essayer_firestore=False))
    c = app.test_client()
    assert c.post("/api/chat", json={"question": "bilan"}).status_code == 200
    assert c.post("/api/chat", json={"question": "bilan"}).status_code == 429


def test_erreur_modele_renvoie_503(entrepot):
    m = MoteurScripte([ErreurModele("Gemini saturé", 429)])
    app = creer_app(entrepot=entrepot, moteur=m, historique=HistoriqueResilient(essayer_firestore=False))
    r = app.test_client().post("/api/chat", json={"question": "bilan"})
    assert r.status_code == 503 and "saturé" in r.get_json()["erreur"]


# ---------------------------------------------------------------- format Gemini
def test_conversion_vers_gemini():
    """Vérifie, sans appel réseau, la traduction des messages vers les objets google-genai."""
    from google.genai import types
    from agent import MoteurGemini
    m = MoteurGemini.__new__(MoteurGemini)
    m.types = types
    m.configurer(O.DECLARATIONS)
    brut = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="synthese_fiabilite", args={}))])
    contenus = m._contenus([
        {"role": "user", "texte": "bonjour"},
        {"role": "model", "appels": [{"nom": "synthese_fiabilite", "args": {}}], "brut": brut},
        {"role": "outil", "reponses": [{"nom": "synthese_fiabilite", "resultat": {"a": 1}}]},
    ])
    assert [c.role for c in contenus] == ["user", "model", "user"]
    assert contenus[1] is brut                                 # contenu d'origine conservé tel quel
    assert contenus[2].parts[0].function_response.name == "synthese_fiabilite"
    assert len(m.outils[0].function_declarations) == len(O.DECLARATIONS)


# ---------------------------------------------------------------- KPI
import kpi as K


def test_kpi_somme_des_numerateurs_pas_moyenne_des_taux(entrepot):
    r = K.calculer(entrepot, ["OTIF"], "2026-01-01", "2026-02-28", "mois", "global")
    total = r["resultats"]["OTIF"]["par_perimetre"][0]
    mois = [K.calculer(entrepot, ["OTIF"], d, f, "mois", "global")["resultats"]["OTIF"]["par_perimetre"][0]
            for d, f in (("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"))]
    attendu = 100 * sum(m["numerateur"] for m in mois) / sum(m["denominateur"] for m in mois)
    assert total["valeur"] == pytest.approx(attendu, abs=1e-3)
    moyenne_naive = (mois[0]["valeur"] + mois[1]["valeur"]) / 2
    assert total["valeur"] != pytest.approx(moyenne_naive, abs=1e-4)


def test_kpi_periode_precedente_et_semaines_a_cheval(entrepot):
    # le 1er janvier 2026 est un jeudi : la première semaine chevauche les deux périodes
    r = K.calculer(entrepot, ["OTIF"], "2026-01-01", "2026-01-31", "semaine", "global")
    assert r["periode_precedente"] == {"debut": "2025-12-01", "fin": "2025-12-31"}
    serie = r["resultats"]["OTIF"]["serie"]
    assert serie[0]["periode"] == "2026-01-01"
    p = r["resultats"]["OTIF"]["par_perimetre"][0]
    dec = K.calculer(entrepot, ["OTIF"], "2025-12-01", "2025-12-31", "mois", "global")["resultats"]["OTIF"]["par_perimetre"][0]
    assert p["valeur_precedente"] == pytest.approx(dec["valeur"], abs=1e-3)
    assert p["valeur"] < p["valeur_precedente"]          # scénario S3 : baisse de janvier


def test_kpi_validations(entrepot):
    with pytest.raises(ErreurDonnees):
        K.calculer(entrepot, ["INCONNU"])
    with pytest.raises(ErreurDonnees):
        K.calculer(entrepot, ["RECLAMATIONS_ENTREPOT"], axe="donneur")
    with pytest.raises(ErreurDonnees):
        K.calculer(entrepot, None, "2030-01-01", "2030-02-01")
    with pytest.raises(ErreurDonnees):
        K.calculer(entrepot, None, "2026-13-01")
    r = K.calculer(entrepot, ["OTIF", "PRODUCTIVITE_PICKING"], axe="donneur")
    assert r["kpi_non_disponibles_pour_cet_axe"] == ["PRODUCTIVITE_PICKING"]
    assert r["periode"]["debut"] == "2026-09-01"          # dernier mois complet par défaut
    assert {p["perimetre"] for p in r["resultats"]["OTIF"]["par_perimetre"]} == {"DO-01", "DO-02", "DO-03"}
    assert all(p["objectif"] for p in r["resultats"]["OTIF"]["par_perimetre"])


def test_kpi_otif_brut_surestime(entrepot):
    r = K.calculer(entrepot, ["OTIF", "OTIF_BRUT"], "2026-01-01", "2026-01-31")
    v = {k: x["par_perimetre"][0]["valeur"] for k, x in r["resultats"].items()}
    assert v["OTIF_BRUT"] - v["OTIF"] > 2


def test_outil_lignes_en_echec(entrepot):
    o = O.Outils(entrepot)
    res, art, err = o.executer("lignes_en_echec", {"kpi": "ERREUR_PREPARATION", "date_debut": "2026-03-02",
                                                   "date_fin": "2026-03-08", "axe": "equipe", "valeur": "nuit"})
    assert err is None and res["resume"][0]["equipe"] == "NUIT"
    assert [a["type"] for a in art] == ["barres", "tableau"]
    _, _, err = o.executer("lignes_en_echec", {"kpi": "OTIF", "axe": "equipe", "valeur": "NUIT"})
    assert err and "donneur" in err
    _, _, err = o.executer("lignes_en_echec", {"kpi": "PRODUCTIVITE_PICKING"})
    assert err


def test_moteur_demo_kpi(entrepot):
    agent = Agent(MoteurDemo(), O.Outils(entrepot), O.DECLARATIONS)
    r = agent.repondre("Quel est l'OTIF en janvier ?", [])
    assert r["outils"][0]["outil"] == "indicateurs_kpi"
    assert "OTIF v1" in r["reponse"] and "courbes" in [a["type"] for a in r["artefacts"]]


def test_api_kpi(client):
    b = client.get("/api/kpi/bornes").get_json()
    assert b["fin"] == "2026-09-30" and "OTIF" in b["axes"]["donneur"]
    t = client.get("/api/kpi/tableau?debut=2026-01-01&fin=2026-03-31&granularite=mois&axe=transporteur&kpis=OTIF,RESPECT_CRENEAUX")
    d = t.get_json()
    assert t.status_code == 200 and set(d["resultats"]) == {"OTIF", "RESPECT_CRENEAUX"}
    assert d["artefacts"][0]["action"]["type"] == "echecs"
    e = client.get("/api/kpi/echecs?kpi=OTIF&debut=2026-01-01&fin=2026-01-31&axe=donneur&valeur=DO-01").get_json()
    assert e["filtre"] == {"axe": "DONNEUR", "valeur": "DO-01"} and all(l["donneur_ordre_id"] == "DO-01" for l in e["lignes"])
    assert len(client.get("/api/kpi/definitions").get_json()["definitions"]) == 10
    assert client.get("/api/kpi/tableau?axe=planete").status_code == 400


# ---------------------------------------------------------------- Dérives et actions
import lean as L


def test_contributions_calcul_et_classement():
    # 2 segments : A garde son taux (1 %), B passe de 1 % à 11 % -> tout l'excès est sur B
    segs = [
        {"dimension": "Article", "segment": "A", "n_f": 900, "e_f": 9, "n_b": 9000, "e_b": 90},
        {"dimension": "Article", "segment": "B", "n_f": 100, "e_f": 11, "n_b": 1000, "e_b": 10},
        {"dimension": "Equipe", "segment": "NUIT", "n_f": 1000, "e_f": 20, "n_b": 10000, "e_b": 100},
    ]
    r = L.contributions(segs, None, L.MESURES["SERVICE"])
    art = next(c for c in r if c["dimension"] == "Article")
    assert art["exces_total"] == pytest.approx(10.0)
    assert art["segments"][0]["segment"] == "B" and art["segments"][0]["part_exces"] > 95
    assert art["segments"][0]["part_volume"] == 10.0
    # une dimension à un seul segment (le périmètre lui-même) n'explique rien et passe en dernier
    assert r[0]["dimension"] == "Article" and r[-1]["pouvoir_explicatif"] == 0


def test_contributions_petit_segment_retreci():
    # un opérateur sans historique ne reçoit pas un taux de référence de 0 %
    segs = [{"dimension": "Opérateur", "segment": "OP1", "n_f": 50, "e_f": 5, "n_b": 0, "e_b": 0},
            {"dimension": "Opérateur", "segment": "OP2", "n_f": 1000, "e_f": 10, "n_b": 10000, "e_b": 100}]
    r = L.contributions(segs, None, L.MESURES["ERREUR_PREPARATION"])[0]
    op1 = next(s for s in r["segments"] if s["segment"] == "OP1")
    assert op1["attendu"] == pytest.approx(0.5, abs=0.01)


def test_derives_et_analyse_demo(entrepot):
    ds = L.tableau_derives(entrepot)
    assert len(ds) >= 15 and all(d["derive_id"].startswith("DER-") for d in ds)
    s3 = next(d for d in ds if d["kpi_code"] == "TAUX_RUPTURE" and d["periode_debut"].startswith("2026-01"))
    r = L.analyser(entrepot, s3["derive_id"])
    art = next(c for c in r["contributions"] if c["dimension"] == "Article")
    assert art["segments"][0]["segment"].startswith("ART-00121")
    assert r["carte"] and any(p["alerte"] for p in r["carte"])
    assert r["actions"][0]["efficacite"]["verdict"] == "RETOUR_SOUS_CONTROLE"
    types = [a["type"] for a in O.artefacts_analyse(r)]
    assert types[:2] == ["indicateurs", "carte_controle"] and "barres" in types


def test_actions_saisie_et_validation(entrepot):
    d = L.tableau_derives(entrepot)[0]
    with pytest.raises(ErreurDonnees):
        L.creer_action(entrepot, {"derive_id": d["derive_id"], "action": "x", "responsable": "y", "echeance": "2026-10-30"})
    with pytest.raises(ErreurDonnees):
        L.creer_action(entrepot, {"derive_id": "DER-XYZ", "cause_identifiee": "c", "action": "a", "responsable": "r",
                                  "echeance": "2026-10-30"})
    a = L.creer_action(entrepot, {"derive_id": d["derive_id"], "cause_identifiee": "c", "action": "a", "responsable": "r",
                                  "echeance": "2026-10-30"})
    assert a["statut"] == "A_FAIRE" and a["date_effet"]           # date d'effet par défaut : lendemain de la dérive
    assert entrepot.lean_derive(d["derive_id"])["statut"] in ("EN_ANALYSE", "TRAITEE")
    with pytest.raises(ErreurDonnees):
        L.modifier_action(entrepot, a["action_id"], {"statut": "INCONNU"})
    a2 = L.modifier_action(entrepot, a["action_id"], {"statut": "FAITE", "commentaire": "ok"})
    assert a2["cloturee_le"] and a2["commentaire"] == "ok"
    assert L.modifier_action(entrepot, a["action_id"], {"statut": "EN_COURS"})["cloturee_le"] is None


def test_creer_action_par_le_copilote_trace(entrepot):
    o = O.Outils(entrepot)
    d = L.tableau_derives(entrepot)[0]
    res, art, err = o.executer("creer_action", {"derive_id": d["derive_id"], "cause_identifiee": "c", "action": "a",
                                                "responsable": "r", "echeance": "2026-11-15"})
    assert err is None and res["action_creee"]["cree_par"] == "copilote"


def test_api_lean(client):
    ds = client.get("/api/lean/derives").get_json()
    assert ds["artefacts"][1]["action"]["type"] == "derive"
    did = ds["derives"][0]["derive_id"]
    r = client.get(f"/api/lean/derives/{did}")
    assert r.status_code == 200 and r.get_json()["artefacts"][1]["type"] == "carte_controle"
    assert client.get("/api/lean/derives/DER-00000000").status_code == 400
    assert client.patch(f"/api/lean/derives/{did}", json={"statut": "FAUSSE_ALERTE"}).get_json()["statut"] == "FAUSSE_ALERTE"
    assert client.patch(f"/api/lean/derives/{did}", json={"statut": "PEUT_ETRE"}).status_code == 400
    c = client.post("/api/lean/actions", json={"derive_id": did, "cause_identifiee": "c", "action": "a",
                                               "responsable": "r", "echeance": "2026-11-01"})
    assert c.status_code == 201
    aid = c.get_json()["action_id"]
    assert client.patch(f"/api/lean/actions/{aid}", json={"statut": "EN_COURS"}).get_json()["statut"] == "EN_COURS"
    acts = client.get("/api/lean/actions").get_json()
    assert any(a["action_id"] == aid for a in acts["actions"]) and acts["artefacts"][0]["type"] == "indicateurs"


# ---------------------------------------------------------------- Prévision de charge
import prevision as P


def test_prevision_tableau_et_renfort(entrepot):
    t = P.tableau(entrepot, "TOUS")
    assert t["periode"]["jours_ouvres"] >= 18 and t["total_lignes"] > 0
    assert 4 <= len(t["semaines"]) <= 5
    # le renfort agrégé par semaine ne dépasse jamais le pic d'un jour
    for s in t["semaines"]:
        assert s["renfort_securite_max"] >= s["renfort_max"] >= 0
    # les jours à renforcer ont bien un renfort strictement positif
    assert all(j["renfort_etp"] > 0 for j in t["jours_tension"])
    with pytest.raises(ErreurDonnees):
        P.tableau(entrepot, "WEEKEND")


def test_prevision_lundi_plus_charge(entrepot):
    # la charge du lundi doit dépasser celle du vendredi (rattrapage du week-end)
    jours = {l["jour"][:10]: l for l in entrepot.prevision_charge() if l["equipe"] == "TOUS" and l["jour_ouvre"]}
    import datetime as dt
    lundis = [v["lignes_prevues"] for k, v in jours.items() if dt.date.fromisoformat(k).weekday() == 0]
    vendredis = [v["lignes_prevues"] for k, v in jours.items() if dt.date.fromisoformat(k).weekday() == 4]
    assert sum(lundis) / len(lundis) > sum(vendredis) / len(vendredis)


def test_prevision_qualite_bat_naif(entrepot):
    q = {r["equipe"]: r for r in P.qualite(entrepot)}
    assert q["TOUS"]["wape_modele_pct"] < q["TOUS"]["wape_naive_pct"]   # le modèle bat la méthode simple
    assert q["TOUS"]["gagne_sur_naif"] is True


def test_prevision_compact_pour_modele(entrepot):
    c = P.compact(entrepot, "NUIT")
    assert c["equipe"] == "NUIT" and 4 <= len(c["par_semaine"]) <= 5
    assert "renfort_etp_max" in c["par_semaine"][0]
    assert c["fiabilite"]["wape_modele_pct"] is not None


def test_outil_et_api_prevision(entrepot, client):
    o = O.Outils(entrepot)
    res, art, err = o.executer("prevision_charge", {"equipe": "NUIT"})
    assert err is None and res["equipe"] == "NUIT"
    assert [a["type"] for a in art][:2] == ["indicateurs", "prevision"]
    r = client.get("/api/prevision?equipe=TOUS").get_json()
    assert r["total_lignes"] > 0 and any(a["type"] == "prevision" for a in r["artefacts"])
    assert client.get("/api/prevision/qualite").get_json()["modele"]


def test_moteur_demo_prevision(entrepot):
    agent = Agent(MoteurDemo(), O.Outils(entrepot), O.DECLARATIONS)
    r = agent.repondre("Combien d'intérimaires la semaine prochaine ?", [])
    assert r["outils"][0]["outil"] == "prevision_charge"
    assert "prevision" in [a["type"] for a in r["artefacts"]]
