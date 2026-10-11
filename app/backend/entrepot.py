"""
Accès aux données de l'entrepôt (BigQuery).

Règles de sécurité :
* l'agent ne peut jamais écrire de SQL lui-même : il appelle des fonctions de
  ce module, dont les requêtes sont écrites à l'avance ;
* les valeurs venant de l'utilisateur passent par des paramètres de requête
  (@param) et sont validées par expression régulière : pas d'injection SQL ;
* chaque requête a un plafond d'octets lus (maximum_bytes_billed) : une
  requête qui dépasserait est refusée par BigQuery avant d'être facturée.
"""
import datetime as dt
import decimal
import json
import re
import threading
import time

import config as C

RE_COMMANDE = re.compile(r"^CMD\d{8}$")
RE_LIGNE = re.compile(r"^(CMD\d{8})-\d{5}$")
RE_LOT = re.compile(r"^LOT\d{6}$")
RE_REGLE = re.compile(r"^DQ_[A-Z]{3}_\d{3}$")
RE_DERIVE = re.compile(r"^DER-[0-9A-F]{8}$")
RE_ACTION = re.compile(r"^ACT-[0-9A-F]{8}$")
RE_PERIMETRE = re.compile(r"^[A-Z0-9_-]{1,20}$")

# ----------------------------------------------------------------------
# Analyse des causes d'une dérive : les "unités" de chaque famille de KPI.
# Chaque unité a une date, un volume n, un nombre d'échecs e (ou, pour la
# performance, des heures réelles rapportées aux heures standard) et une liste
# de segments (dimension, segment). lean.py compare ensuite, segment par
# segment, la fenêtre de la dérive et la période de référence.
# Paramètres : @d1 @d2 (fenêtre), @b1 @b2 (référence), @v (périmètre).
# ----------------------------------------------------------------------
_ANCIENNETE = """CASE WHEN DATE_DIFF({jour}, o.date_arrivee, DAY) < 30 THEN 'moins de 30 jours'
                      WHEN DATE_DIFF({jour}, o.date_arrivee, DAY) < 180 THEN '30 à 180 jours'
                      ELSE 'plus de 180 jours' END"""
_JOUR_PROD = """IF(EXTRACT(HOUR FROM DATETIME(p.debut, 'Europe/Paris')) >= 21,
                   DATE_ADD(DATE(p.debut, 'Europe/Paris'), INTERVAL 1 DAY), DATE(p.debut, 'Europe/Paris'))"""
UNITES_SQL = {
    "SERVICE": """
        SELECT ls.date_promise AS jour, 1.0 AS n, IF(COALESCE({echec}, FALSE), 1.0, 0.0) AS e,
          [STRUCT('Donneur d’ordre' AS dimension, ls.donneur_ordre_id AS segment),
           STRUCT('Transporteur' AS dimension, ls.transporteur_id AS segment),
           STRUCT('Article' AS dimension, CONCAT(ls.article_id, ' ', COALESCE(a.libelle, '')) AS segment),
           STRUCT('Classe ABC' AS dimension, a.classe_abc AS segment),
           STRUCT('Type de destinataire' AS dimension, x.type_destinataire AS segment)] AS dims
        FROM sc_kpi.lignes_service AS ls
        LEFT JOIN sc_raw.dim_article AS a ON a.article_id = ls.article_id
        LEFT JOIN sc_raw.dim_destinataire AS x ON x.destinataire_id = ls.destinataire_id
        WHERE ls.statut_mesure IN ({statuts}) AND ls.date_promise BETWEEN @b1 AND @d2 {filtre}""",
    "ERREUR_PREPARATION": f"""
        SELECT {_JOUR_PROD} AS jour, 1.0 AS n,
          IF(p.resultat_controle IN ('ECART_QUANTITE', 'ERREUR_ARTICLE', 'ERREUR_LOT'), 1.0, 0.0) AS e,
          [STRUCT('Equipe' AS dimension, o.equipe AS segment),
           STRUCT('Type de contrat' AS dimension, o.type_contrat AS segment),
           STRUCT('Ancienneté' AS dimension, {_ANCIENNETE.format(jour="DATE(p.debut, 'Europe/Paris')")} AS segment),
           STRUCT('Opérateur' AS dimension, p.operateur_id AS segment),
           STRUCT('Type de zone' AS dimension, z.type_zone AS segment)] AS dims
        FROM sc_raw.preparations AS p
        LEFT JOIN sc_raw.dim_operateur AS o ON o.operateur_id = p.operateur_id
        LEFT JOIN sc_raw.dim_emplacement AS em ON em.emplacement_id = p.emplacement_id
        LEFT JOIN sc_raw.dim_zone AS z ON z.zone_id = em.zone_id
        WHERE p.resultat_controle != 'NON_CONTROLE'
          AND DATE(p.debut, 'Europe/Paris') BETWEEN DATE_SUB(@b1, INTERVAL 1 DAY) AND @d2
          {{filtre}}""",
    # performance : n = heures standard produites, e = heures réelles pointées (e/n élevé = moins performant)
    "PERFORMANCE_PICKING": f"""
        WITH std AS (
          SELECT p.operateur_id, {_JOUR_PROD} AS jour,
                 SUM(1 / COALESCE(sz.cadence_std_par_heure, sp.cadence_std_par_heure)) AS heures_std
          FROM sc_raw.preparations AS p
          LEFT JOIN sc_raw.dim_emplacement AS em ON em.emplacement_id = p.emplacement_id
          LEFT JOIN sc_raw.dim_zone AS z ON z.zone_id = em.zone_id
          LEFT JOIN sc_raw.standards_activite AS sz ON sz.activite = 'PICKING' AND sz.type_zone = z.type_zone
          LEFT JOIN sc_raw.standards_activite AS sp ON sp.activite = 'PICKING' AND sp.type_zone = 'PICKING'
          WHERE DATE(p.debut, 'Europe/Paris') BETWEEN DATE_SUB(@b1, INTERVAL 1 DAY) AND @d2
          GROUP BY 1, 2
        ),
        reel AS (
          SELECT operateur_id, date_travail AS jour, SUM(heures) AS heures
          FROM sc_raw.temps_travail
          WHERE activite = 'PICKING' AND date_travail BETWEEN @b1 AND @d2
          GROUP BY 1, 2
        )
        SELECT jour, COALESCE(s.heures_std, 0) AS n, COALESCE(r.heures, 0) AS e,
          [STRUCT('Equipe' AS dimension, o.equipe AS segment),
           STRUCT('Type de contrat' AS dimension, o.type_contrat AS segment),
           STRUCT('Ancienneté' AS dimension, {_ANCIENNETE.format(jour="jour")} AS segment),
           STRUCT('Opérateur' AS dimension, operateur_id AS segment)] AS dims
        FROM std AS s
        FULL OUTER JOIN reel AS r USING (operateur_id, jour)
        LEFT JOIN sc_raw.dim_operateur AS o USING (operateur_id)
        WHERE jour BETWEEN @b1 AND @d2 {{filtre}}""",
    "TRANSPORT": """
        SELECT DATE(c.debut_prevu, 'Europe/Paris') AS jour, 1.0 AS n,
          IF(c.arrivee_reelle <= TIMESTAMP_ADD(c.debut_prevu, INTERVAL 15 MINUTE), 0.0, 1.0) AS e,
          [STRUCT('Transporteur' AS dimension, c.transporteur_id AS segment),
           STRUCT('Heure du créneau' AS dimension, FORMAT_TIMESTAMP('%H:%M', c.debut_prevu, 'Europe/Paris') AS segment),
           STRUCT('Jour de la semaine' AS dimension, FORMAT_DATE('%A', DATE(c.debut_prevu, 'Europe/Paris')) AS segment),
           STRUCT('Quai' AS dimension, c.quai AS segment)] AS dims
        FROM sc_raw.creneaux AS c
        WHERE c.type_flux = 'ENLEVEMENT' AND c.arrivee_reelle IS NOT NULL
          AND DATE(c.debut_prevu, 'Europe/Paris') BETWEEN @b1 AND @d2 {filtre}""",
    # réclamations : e = réclamations ; n est remplacé par le nombre de lignes livrées (lean.py)
    "CLIENT": """
        SELECT DATE(r.date_reclamation, 'Europe/Paris') AS jour, 0.0 AS n, 1.0 AS e,
          [STRUCT('Type de réclamation' AS dimension, r.type_reclamation AS segment),
           STRUCT('Donneur d’ordre' AS dimension, c.donneur_ordre_id AS segment),
           STRUCT('Type de destinataire' AS dimension, x.type_destinataire AS segment)] AS dims
        FROM (SELECT DISTINCT reclamation_id, commande_id, destinataire_id, date_reclamation, type_reclamation, statut, cause_imputee
              FROM sc_raw.reclamations) AS r
        LEFT JOIN (SELECT DISTINCT commande_id, donneur_ordre_id FROM sc_raw.commandes) AS c ON c.commande_id = r.commande_id
        LEFT JOIN sc_raw.dim_destinataire AS x ON x.destinataire_id = r.destinataire_id
        WHERE r.cause_imputee = 'ENTREPOT' AND r.statut != 'NON_JUSTIFIEE'
          AND DATE(r.date_reclamation, 'Europe/Paris') BETWEEN @b1 AND @d2""",
}
ECHEC_SERVICE = {
    "OTIF": ("NOT ls.otif", "'MESURE'"),
    "TAUX_RUPTURE": ("ls.rupture", "'MESURE'"),
    "LIGNES_NON_MESURABLES": ("ls.statut_mesure = 'NON_MESURABLE'", "'MESURE', 'NON_MESURABLE'"),
}
FILTRES_PERIMETRE = {
    ("SERVICE", "DONNEUR"): "AND ls.donneur_ordre_id = @v",
    ("SERVICE", "TRANSPORTEUR"): "AND ls.transporteur_id = @v",
    ("ERREUR_PREPARATION", "EQUIPE"): "AND o.equipe = @v",
    ("PERFORMANCE_PICKING", "EQUIPE"): "AND o.equipe = @v",
    ("TRANSPORT", "TRANSPORTEUR"): "AND c.transporteur_id = @v",
}


def requete_unites(famille, kpi, perimetre_type):
    """Texte SQL des unités pour une dérive (partagé avec le banc de test)."""
    if famille == "SERVICE":
        echec, statuts = ECHEC_SERVICE[kpi]
        cle = "SERVICE"
        sql = UNITES_SQL["SERVICE"].replace("{echec}", echec).replace("{statuts}", statuts)
        if kpi == "TAUX_RUPTURE":   # une rupture de stock ne dépend pas du transporteur : dimension non pertinente
            sql = sql.replace("\n           STRUCT('Transporteur' AS dimension, ls.transporteur_id AS segment),", "")
    elif famille == "PREPARATION":
        cle = kpi
        sql = UNITES_SQL[kpi]
    else:
        cle = famille
        sql = UNITES_SQL[famille]
    filtre = FILTRES_PERIMETRE.get((cle, perimetre_type), "")
    return sql.replace("{filtre}", filtre)


REQUETE_SEGMENTS = """
    WITH unites AS ({unites})
    SELECT d.dimension, COALESCE(d.segment, '(non renseigné)') AS segment,
           SUM(IF(u.jour BETWEEN @d1 AND @d2, u.n, 0)) AS n_f, SUM(IF(u.jour BETWEEN @d1 AND @d2, u.e, 0)) AS e_f,
           SUM(IF(u.jour BETWEEN @b1 AND @b2, u.n, 0)) AS n_b, SUM(IF(u.jour BETWEEN @b1 AND @b2, u.e, 0)) AS e_b
    FROM unites AS u, UNNEST(u.dims) AS d
    WHERE u.jour BETWEEN @b1 AND @d2
    GROUP BY 1, 2
"""

ORDRE_SEVERITE = "CASE severite WHEN 'CRITIQUE' THEN 1 WHEN 'MAJEURE' THEN 2 ELSE 3 END"


class ErreurDonnees(Exception):
    """Erreur présentable à l'utilisateur (identifiant invalide, introuvable...)."""


def propre(v):
    """Convertit les valeurs BigQuery en types JSON simples."""
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: propre(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [propre(x) for x in v]
    return v


def detail_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return propre(v)


def normaliser_commande(identifiant):
    identifiant = (identifiant or "").strip().upper()
    m = RE_LIGNE.match(identifiant)
    if m:
        return m.group(1)
    if not RE_COMMANDE.match(identifiant):
        raise ErreurDonnees(f"'{identifiant}' n'est pas un numéro de commande valide (format attendu : CMD00012345).")
    return identifiant


def normaliser_lot(identifiant):
    identifiant = (identifiant or "").strip().upper()
    if not RE_LOT.match(identifiant):
        raise ErreurDonnees(f"'{identifiant}' n'est pas un identifiant de lot valide (format attendu : LOT001234).")
    return identifiant


def normaliser_derive(identifiant):
    identifiant = (identifiant or "").strip().upper()
    if not RE_DERIVE.match(identifiant):
        raise ErreurDonnees(f"'{identifiant}' n'est pas un identifiant de dérive valide (format attendu : DER-1A2B3C4D).")
    return identifiant


def normaliser_action(identifiant):
    identifiant = (identifiant or "").strip().upper()
    if not RE_ACTION.match(identifiant):
        raise ErreurDonnees(f"'{identifiant}' n'est pas un identifiant d'action valide (format attendu : ACT-1A2B3C4D).")
    return identifiant


def normaliser_regle(identifiant):
    identifiant = (identifiant or "").strip().upper()
    if not RE_REGLE.match(identifiant):
        raise ErreurDonnees(f"'{identifiant}' n'est pas un identifiant de règle valide (format attendu : DQ_COH_005).")
    return identifiant


class EntrepotBigQuery:
    def __init__(self):
        from google.cloud import bigquery
        self.bq = bigquery
        self.client = bigquery.Client(project=C.PROJET or None, location=C.BQ_LOCATION)
        self._verrou_controles = threading.Lock()
        self._dernier_lancement = 0.0

    # ------------------------------------------------------------------
    def _requete(self, sql, params=None, octets_max=None):
        bq = self.bq
        conf = bq.QueryJobConfig(
            query_parameters=[bq.ArrayQueryParameter(n, t[6:-1], v) if t.startswith("ARRAY<")
                              else bq.ScalarQueryParameter(n, t, v) for n, t, v in (params or [])],
            maximum_bytes_billed=octets_max or C.OCTETS_MAX_LECTURE,
            labels={"application": "copilote-entrepot"},
        )
        lignes = self.client.query(sql, job_config=conf).result(timeout=240)
        return [{k: propre(v) for k, v in dict(r).items()} for r in lignes]

    # ------------------------------------------------------------------
    def synthese(self):
        regles = self._requete(f"""
            SELECT regle_id, libelle, dimension, severite, table_cible, nb_anomalies,
                   nb_lignes_controlees, taux_conformite_pct, controle_le, description
            FROM sc_dq.v_synthese
            ORDER BY {ORDRE_SEVERITE}, nb_anomalies DESC
        """)
        lignes = self._requete("""
            SELECT
              (SELECT row_count FROM sc_raw.__TABLES__ WHERE table_id = 'lignes_commande') AS total,
              (SELECT COUNT(DISTINCT REGEXP_EXTRACT(cle_ligne, r'^(CMD\\d{8}-\\d{5})$'))
                 FROM sc_dq.v_anomalies_courantes WHERE table_cible = 'lignes_commande') AS en_anomalie
        """)[0]
        return construire_synthese(regles, lignes["total"], lignes["en_anomalie"])

    def anomalies(self, regle_id, limite=50):
        regle_id = normaliser_regle(regle_id)
        limite = max(1, min(int(limite or 50), 500))
        regle = self._requete("""
            SELECT regle_id, libelle, description, dimension, severite, table_cible, nb_anomalies, controle_le
            FROM sc_dq.v_synthese WHERE regle_id = @r
        """, [("r", "STRING", regle_id)])
        if not regle:
            raise ErreurDonnees(f"La règle {regle_id} n'existe pas ou n'est pas active.")
        lignes = self._requete("""
            SELECT cle_ligne, detail
            FROM sc_dq.v_anomalies_courantes
            WHERE regle_id = @r
            ORDER BY cle_ligne
            LIMIT @n
        """, [("r", "STRING", regle_id), ("n", "INT64", limite)])
        for l in lignes:
            l["detail"] = detail_json(l["detail"])
        return {"regle": regle[0], "anomalies": lignes, "limite": limite}

    def lancer_controles(self, regle_id=None):
        if regle_id:
            regle_id = normaliser_regle(regle_id)
        attente = C.DELAI_MIN_ENTRE_CONTROLES_S - (time.time() - self._dernier_lancement)
        if attente > 0:
            raise ErreurDonnees(f"Les contrôles ont été lancés il y a moins de {C.DELAI_MIN_ENTRE_CONTROLES_S // 60} minutes. "
                                f"Réessayez dans {int(attente // 60) + 1} minute(s) ; les résultats actuels sont à jour.")
        if not self._verrou_controles.acquire(blocking=False):
            raise ErreurDonnees("Une exécution des contrôles est déjà en cours.")
        try:
            debut = time.time()
            resultats = self._requete("CALL sc_dq.executer_controles(@r)", [("r", "STRING", regle_id)],
                                      octets_max=C.OCTETS_MAX_CONTROLES)
            self._dernier_lancement = time.time()
            return {"regles_executees": resultats, "duree_s": round(time.time() - debut, 1)}
        finally:
            self._verrou_controles.release()

    # ------------------------------------------------------------------
    # KPI (couche 4)
    # ------------------------------------------------------------------
    def _cache(self, cle, fonction, duree_s=600):
        """Petites données de référence (bornes, définitions, libellés) : relues toutes les 10 minutes."""
        if not hasattr(self, "_memo"):
            self._memo = {}
        v = self._memo.get(cle)
        if v and time.time() - v[0] < duree_s:
            return v[1]
        r = fonction()
        self._memo[cle] = (time.time(), r)
        return r

    def kpi_bornes(self):
        def f():
            r = self._requete("SELECT MIN(date_kpi) AS debut, MAX(date_kpi) AS fin FROM sc_kpi.kpi_jour")[0]
            if not r["debut"]:
                raise ErreurDonnees("Les KPI n'ont pas encore été calculés : exécutez CALL sc_kpi.calculer_kpi().")
            return r
        return self._cache("bornes", f)

    def kpi_definitions(self):
        return self._cache("definitions", lambda: self._requete("""
            SELECT kpi_code, version, libelle, description, formule, unite, sens, niveau, date_reference
            FROM sc_kpi.definitions_kpi
            WHERE valide_jusqu IS NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY kpi_code ORDER BY version DESC) = 1
            ORDER BY kpi_code
        """))

    def libelles_perimetres(self):
        def f():
            lignes = self._requete("""
                SELECT donneur_ordre_id AS id, nom FROM sc_raw.dim_donneur_ordre
                UNION ALL SELECT transporteur_id, nom FROM sc_raw.dim_transporteur
            """)
            d = {l["id"]: l["nom"] for l in lignes}
            d.update({"TOUS": "Site", "MATIN": "Equipe du matin", "APRES_MIDI": "Equipe d'après-midi", "NUIT": "Equipe de nuit"})
            return d
        return self._cache("libelles", f)

    def objectifs(self):
        def f():
            lignes = self._requete("SELECT donneur_ordre_id, objectif_otif_pct, objectif_precision_pct FROM sc_raw.dim_donneur_ordre")
            return {"OTIF": {l["donneur_ordre_id"]: l["objectif_otif_pct"] for l in lignes},
                    "ERREUR_PREPARATION": {l["donneur_ordre_id"]: round(100 - l["objectif_precision_pct"], 3) for l in lignes}}
        return self._cache("objectifs", f)

    def kpi_sommes(self, kpis, debut, fin, troncature, axe, debut_courant):
        """Sommes des numérateurs et dénominateurs par période et périmètre, de debut à fin.
        dans_periode distingue les jours de la période demandée (>= debut_courant) de ceux
        de la période précédente. troncature et axe viennent d'une liste blanche (kpi.py)."""
        return self._requete(f"""
            SELECT DATE_TRUNC(date_kpi, {troncature}) AS periode, kpi_code, perimetre_valeur,
                   date_kpi >= @debut_courant AS dans_periode,
                   SUM(numerateur) AS num, SUM(denominateur) AS den
            FROM sc_kpi.kpi_jour
            WHERE date_kpi BETWEEN @debut AND @fin AND perimetre_type = @axe AND kpi_code IN UNNEST(@kpis)
            GROUP BY periode, kpi_code, perimetre_valeur, dans_periode
            ORDER BY periode
        """, [("debut", "DATE", debut), ("fin", "DATE", fin), ("axe", "STRING", axe),
              ("kpis", "ARRAY<STRING>", kpis), ("debut_courant", "DATE", debut_courant)])

    def lignes_en_echec(self, kpi, debut, fin, axe, valeur, limite):
        if kpi == "OTIF":
            filtre = {"DONNEUR": "AND donneur_ordre_id = @v", "TRANSPORTEUR": "AND transporteur_id = @v"}.get(axe, "")
            p = [("d", "DATE", debut), ("f", "DATE", fin), ("v", "STRING", valeur), ("n", "INT64", limite)]
            cause = """CASE WHEN rupture THEN 'RUPTURE' WHEN date_livraison IS NULL THEN 'NON_LIVREE'
                            WHEN NOT a_l_heure AND NOT complete THEN 'RETARD_ET_INCOMPLET'
                            WHEN NOT a_l_heure THEN 'RETARD' ELSE 'INCOMPLET' END"""
            resume = self._requete(f"""
                SELECT {cause} AS cause, COUNT(*) AS nb
                FROM sc_kpi.lignes_service
                WHERE statut_mesure = 'MESURE' AND NOT otif AND date_promise BETWEEN @d AND @f {filtre}
                GROUP BY cause ORDER BY nb DESC
            """, p)
            lignes = self._requete(f"""
                SELECT ligne_id, commande_id, donneur_ordre_id, transporteur_id, article_id, date_promise,
                       date_livraison, IF(date_livraison IS NULL, NULL, retard_jours) AS retard_jours,
                       qte_commandee, qte_livree, motif_ecart, {cause} AS cause
                FROM sc_kpi.lignes_service
                WHERE statut_mesure = 'MESURE' AND NOT otif AND date_promise BETWEEN @d AND @f {filtre}
                ORDER BY date_promise DESC, ligne_id
                LIMIT @n
            """, p)
            return {"kpi": kpi, "resume": resume, "lignes": lignes}
        if kpi == "ERREUR_PREPARATION":
            filtre = "AND o.equipe = @v" if axe == "EQUIPE" else ""
            p = [("d", "DATE", debut), ("f", "DATE", fin), ("v", "STRING", valeur), ("n", "INT64", limite)]
            base = f"""
                FROM sc_raw.preparations AS p
                LEFT JOIN sc_raw.dim_operateur AS o ON o.operateur_id = p.operateur_id
                WHERE p.resultat_controle IN ('ECART_QUANTITE', 'ERREUR_ARTICLE', 'ERREUR_LOT')
                  AND IF(EXTRACT(HOUR FROM DATETIME(p.debut, 'Europe/Paris')) >= 21,
                         DATE_ADD(DATE(p.debut, 'Europe/Paris'), INTERVAL 1 DAY), DATE(p.debut, 'Europe/Paris'))
                      BETWEEN @d AND @f {filtre}"""
            resume = self._requete(f"""
                SELECT o.equipe, o.type_contrat,
                       IF(DATE_DIFF(DATE(p.debut, 'Europe/Paris'), o.date_arrivee, DAY) < 30, 'moins de 30 jours', '30 jours et plus') AS anciennete,
                       COUNT(*) AS nb
                {base}
                GROUP BY 1, 2, 3 ORDER BY nb DESC
            """, p)
            lignes = self._requete(f"""
                SELECT p.tache_id, p.commande_id, p.lot_id, p.operateur_id, o.equipe, o.type_contrat,
                       DATE_DIFF(DATE(p.debut, 'Europe/Paris'), o.date_arrivee, DAY) AS anciennete_jours,
                       p.debut, p.resultat_controle
                {base}
                ORDER BY p.debut DESC
                LIMIT @n
            """, p)
            return {"kpi": kpi, "resume": resume, "lignes": lignes}
        raise ErreurDonnees("Le détail ligne à ligne est disponible pour OTIF et ERREUR_PREPARATION.")

    # ------------------------------------------------------------------
    # Dérives et plan d'actions (couche 6)
    # ------------------------------------------------------------------
    def _ecrire(self, sql, params):
        """Requête de modification (INSERT, UPDATE). Renvoie le nombre de lignes modifiées."""
        bq = self.bq
        conf = bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter(n, t, v) for n, t, v in params],
            maximum_bytes_billed=C.OCTETS_MAX_LECTURE, labels={"application": "copilote-entrepot"})
        job = self.client.query(sql, job_config=conf)
        job.result(timeout=120)
        return job.num_dml_affected_rows or 0

    def lean_derives(self):
        lignes = self._requete("""
            SELECT d.derive_id, d.kpi_code, d.perimetre_type, d.perimetre_valeur, d.famille, d.gravite,
                   d.periode_debut, d.periode_fin, d.valeur_observee, d.valeur_reference, d.limite_controle,
                   d.z_max, d.nb_semaines, d.regle, d.unite, d.sens, d.statut, d.analyse_causes, d.signaux,
                   d.detectee_le, COALESCE(a.nb_actions, 0) AS nb_actions
            FROM sc_lean.derives AS d
            LEFT JOIN (SELECT derive_id, COUNT(*) AS nb_actions FROM sc_lean.actions GROUP BY 1) AS a USING (derive_id)
            ORDER BY d.periode_debut DESC
        """)
        for l in lignes:
            l["signaux"] = detail_json(l["signaux"]) or []
        return lignes

    def lean_derive(self, derive_id):
        derive_id = normaliser_derive(derive_id)
        d = next((x for x in self.lean_derives() if x["derive_id"] == derive_id), None)
        if not d:
            raise ErreurDonnees(f"La dérive {derive_id} n'existe pas (lancer CALL sc_lean.detecter_derives() si besoin).")
        return d

    def lean_carte(self, kpi, perimetre_type, perimetre_valeur, debut, fin):
        return self._requete("""
            SELECT semaine, valeur, centre, lcs, lci, z_defav, alerte, denominateur
            FROM sc_lean.cartes_controle
            WHERE kpi_code = @k AND perimetre_type = @t AND perimetre_valeur = @v AND semaine BETWEEN @d AND @f
            ORDER BY semaine
        """, [("k", "STRING", kpi), ("t", "STRING", perimetre_type), ("v", "STRING", perimetre_valeur),
              ("d", "DATE", debut), ("f", "DATE", fin)])

    def lean_segments(self, famille, kpi, perimetre_type, perimetre_valeur, d1, d2, b1, b2):
        sql = REQUETE_SEGMENTS.replace("{unites}", requete_unites(famille, kpi, perimetre_type))
        p = [("d1", "DATE", d1), ("d2", "DATE", d2), ("b1", "DATE", b1), ("b2", "DATE", b2),
             ("v", "STRING", perimetre_valeur)]
        segments = self._requete(sql, p)
        totaux = None
        if famille == "CLIENT":      # dénominateur des réclamations : lignes livrées
            totaux = self._requete("""
                SELECT COUNTIF(date_livraison BETWEEN @d1 AND @d2) AS n_f, COUNTIF(date_livraison BETWEEN @b1 AND @b2) AS n_b
                FROM sc_kpi.lignes_service
                WHERE statut_mesure = 'MESURE' AND date_livraison BETWEEN @b1 AND @d2
            """, p[:4])[0]
        return segments, totaux

    def lean_actions(self):
        return self._requete("""
            SELECT a.action_id, a.derive_id, a.cause_identifiee, a.action, a.responsable, a.echeance, a.statut,
                   a.date_effet, a.commentaire, a.cree_par, a.cree_le, a.cloturee_le, a.maj_le
            FROM sc_lean.actions AS a
            ORDER BY a.cree_le DESC
        """)

    def lean_creer_action(self, a):
        n = self._ecrire("""
            INSERT INTO sc_lean.actions (action_id, derive_id, cause_identifiee, action, responsable, echeance, statut,
                                         date_effet, commentaire, cree_par, cree_le, maj_le)
            VALUES (@id, @derive, @cause, @action, @resp, @echeance, 'A_FAIRE', @effet, NULL, @par,
                    CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        """, [("id", "STRING", a["action_id"]), ("derive", "STRING", a["derive_id"]), ("cause", "STRING", a["cause_identifiee"]),
              ("action", "STRING", a["action"]), ("resp", "STRING", a["responsable"]), ("echeance", "DATE", a["echeance"]),
              ("effet", "DATE", a["date_effet"]), ("par", "STRING", a["cree_par"])])
        if n != 1:
            raise ErreurDonnees("L'action n'a pas pu être enregistrée.")
        return next(x for x in self.lean_actions() if x["action_id"] == a["action_id"])

    def lean_maj_action(self, action_id, champs):
        action_id = normaliser_action(action_id)
        n = self._ecrire("""
            UPDATE sc_lean.actions SET
              statut = COALESCE(@statut, statut),
              commentaire = COALESCE(@commentaire, commentaire),
              responsable = COALESCE(@resp, responsable),
              echeance = COALESCE(@echeance, echeance),
              date_effet = COALESCE(@effet, date_effet),
              cloturee_le = CASE WHEN COALESCE(@statut, statut) IN ('FAITE', 'EFFICACE', 'NON_EFFICACE')
                                 THEN COALESCE(cloturee_le, CURRENT_TIMESTAMP()) ELSE NULL END,
              maj_le = CURRENT_TIMESTAMP()
            WHERE action_id = @id
        """, [("id", "STRING", action_id), ("statut", "STRING", champs.get("statut")),
              ("commentaire", "STRING", champs.get("commentaire")), ("resp", "STRING", champs.get("responsable")),
              ("echeance", "DATE", champs.get("echeance")), ("effet", "DATE", champs.get("date_effet"))])
        if n != 1:
            raise ErreurDonnees(f"L'action {action_id} est introuvable.")
        return next(x for x in self.lean_actions() if x["action_id"] == action_id)

    def lean_maj_derive(self, derive_id, champs):
        derive_id = normaliser_derive(derive_id)
        n = self._ecrire("""
            UPDATE sc_lean.derives SET
              statut = COALESCE(@statut, statut),
              analyse_causes = COALESCE(@analyse, analyse_causes),
              maj_le = CURRENT_TIMESTAMP()
            WHERE derive_id = @id
        """, [("id", "STRING", derive_id), ("statut", "STRING", champs.get("statut")),
              ("analyse", "STRING", champs.get("analyse_causes"))])
        if n != 1:
            raise ErreurDonnees(f"La dérive {derive_id} est introuvable.")
        return self.lean_derive(derive_id)

    def tracer_commande(self, identifiant):
        cmd = normaliser_commande(identifiant)
        p = [("c", "STRING", cmd)]
        entetes = self._requete("""
            SELECT c.commande_id, c.donneur_ordre_id, d.nom AS donneur_ordre, c.destinataire_id,
                   x.nom AS destinataire, x.type_destinataire, c.transporteur_id, t.nom AS transporteur,
                   c.priorite, c.canal, c.date_creation, c.date_livraison_demandee, c.statut_commande, c.creneau_id
            FROM sc_raw.commandes AS c
            LEFT JOIN sc_raw.dim_donneur_ordre AS d USING (donneur_ordre_id)
            LEFT JOIN sc_raw.dim_destinataire AS x USING (destinataire_id)
            LEFT JOIN sc_raw.dim_transporteur AS t USING (transporteur_id)
            WHERE c.commande_id = @c
        """, p)
        if not entetes:
            raise ErreurDonnees(f"La commande {cmd} est introuvable.")
        lignes = self._requete("""
            SELECT l.ligne_id, l.num_ligne, l.article_id, a.libelle, l.qte_commandee, l.qte_preparee,
                   l.qte_expediee, l.qte_livree, l.date_promise, l.date_expedition_reelle,
                   l.date_livraison_reelle, l.statut_ligne, l.motif_ecart
            FROM sc_raw.lignes_commande AS l
            LEFT JOIN sc_raw.dim_article AS a USING (article_id)
            WHERE l.commande_id = @c
            ORDER BY l.num_ligne
        """, p)
        preparations = self._requete("""
            SELECT p.tache_id, p.ligne_id, p.lot_id, p.operateur_id, o.equipe, o.type_contrat,
                   p.qte_prelevee, p.debut, p.fin, p.resultat_controle
            FROM sc_raw.preparations AS p
            LEFT JOIN sc_raw.dim_operateur AS o USING (operateur_id)
            WHERE p.commande_id = @c
            ORDER BY p.debut
        """, p)
        statuts = self._requete("""
            SELECT statut, horodatage, source_systeme
            FROM sc_raw.historique_statuts
            WHERE type_entite = 'COMMANDE' AND entite_id = @c
            ORDER BY horodatage
        """, p)
        anomalies = self._requete("""
            SELECT a.regle_id, a.libelle, a.severite, a.cle_ligne, a.detail
            FROM sc_dq.v_anomalies_courantes AS a
            WHERE a.cle_ligne = @c OR STARTS_WITH(a.cle_ligne, CONCAT(@c, '-'))
               OR a.cle_ligne IN (SELECT tache_id FROM sc_raw.preparations WHERE commande_id = @c)
        """, p)
        for a in anomalies:
            a["detail"] = detail_json(a["detail"])
        return {"commande": entetes[0], "nb_entetes": len(entetes), "lignes": lignes,
                "preparations": preparations, "historique": statuts, "anomalies": anomalies}

    def tracer_lot(self, identifiant):
        lot = normaliser_lot(identifiant)
        p = [("l", "STRING", lot)]
        info = self._requete("""
            SELECT l.lot_id, l.num_lot, l.article_id, a.libelle, a.donneur_ordre_id,
                   CONCAT(CAST(a.temp_min_c AS STRING), '-', CAST(a.temp_max_c AS STRING), ' °C') AS conservation,
                   l.date_fabrication, l.date_peremption, l.statut_qualite, l.rappele, l.date_rappel
            FROM sc_raw.dim_lot AS l
            LEFT JOIN sc_raw.dim_article AS a USING (article_id)
            WHERE l.lot_id = @l
        """, p)
        if not info:
            raise ErreurDonnees(f"Le lot {lot} est introuvable.")
        stock = self._requete("""
            SELECT s.date_photo, s.emplacement_id, e.zone_id, s.statut_qualite, s.qte_physique, s.qte_reservee
            FROM sc_raw.stock_photo AS s
            LEFT JOIN sc_raw.dim_emplacement AS e USING (emplacement_id)
            WHERE s.lot_id = @l
              AND s.date_photo = (SELECT MAX(date_photo) FROM sc_raw.stock_photo WHERE lot_id = @l)
        """, p)
        mouvements = self._requete("""
            SELECT type_mouvement, code_mvt_sap, COUNT(*) AS nb, SUM(quantite) AS quantite,
                   MIN(horodatage) AS premier, MAX(horodatage) AS dernier
            FROM sc_raw.mouvements_stock
            WHERE lot_id = @l
            GROUP BY type_mouvement, code_mvt_sap
            ORDER BY premier
        """, p)
        diffusion = self._requete("""
            SELECT COUNT(DISTINCT p.commande_id) AS nb_commandes,
                   COUNT(DISTINCT c.destinataire_id) AS nb_destinataires,
                   SUM(p.qte_prelevee) AS boites_prelevees,
                   MIN(p.debut) AS premier_prelevement, MAX(p.debut) AS dernier_prelevement
            FROM sc_raw.preparations AS p
            LEFT JOIN (SELECT DISTINCT commande_id, destinataire_id FROM sc_raw.commandes) AS c USING (commande_id)
            WHERE p.lot_id = @l
        """, p)[0]
        anomalies = self._requete("""
            SELECT a.regle_id, a.libelle, a.severite, a.cle_ligne, a.detail
            FROM sc_dq.v_anomalies_courantes AS a
            WHERE a.cle_ligne = @l OR STARTS_WITH(a.cle_ligne, CONCAT(@l, '|'))
               OR a.cle_ligne IN (SELECT tache_id FROM sc_raw.preparations WHERE lot_id = @l)
            LIMIT 200
        """, p)
        for a in anomalies:
            a["detail"] = detail_json(a["detail"])
        return {"lot": info[0], "stock_actuel": stock, "mouvements": mouvements,
                "diffusion": diffusion, "anomalies": anomalies}


def construire_synthese(regles, total_lignes, lignes_en_anomalie):
    """Partagé entre BigQuery et le mode démo : indicateurs globaux transparents."""
    actives = [r for r in regles if r.get("nb_anomalies") is not None]
    controle_le = max((r["controle_le"] for r in actives if r.get("controle_le")), default=None)
    critiques = sum(r["nb_anomalies"] for r in actives if r["severite"] == "CRITIQUE")
    total_lignes = total_lignes or 0
    fiables = None
    if total_lignes:
        fiables = round(100 * (1 - (lignes_en_anomalie or 0) / total_lignes), 2)
    return {
        "regles": regles,
        "indicateurs": {
            "nb_regles": len(regles),
            "nb_regles_executees": len(actives),
            "nb_regles_en_anomalie": sum(1 for r in actives if r["nb_anomalies"] > 0),
            "nb_anomalies_total": sum(r["nb_anomalies"] for r in actives),
            "nb_anomalies_critiques": critiques,
            "lignes_commande_total": total_lignes,
            "lignes_commande_en_anomalie": lignes_en_anomalie,
            "lignes_commande_fiables_pct": fiables,
            "dernier_controle": controle_le,
        },
    }
