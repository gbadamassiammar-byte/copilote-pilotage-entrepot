-- =====================================================================
-- 06_dq_vues.sql : vues de lecture des résultats de fiabilisation
--
-- sc_dq.v_synthese            : une ligne par règle, résultat de sa dernière
--                               exécution réussie (ce que lira l'agent)
-- sc_dq.v_anomalies_courantes : le détail des anomalies de ces exécutions
-- sc_eval.v_evaluation_dq     : comparaison avec la vérité terrain
--                               (rappel, précision). Jamais exposée à l'agent.
--
-- Une vue ne stocke rien : elle est recalculée à chaque lecture, donc
-- toujours à jour après un nouvel appel de la procédure.
-- =====================================================================

SET @@location = 'EU';

CREATE OR REPLACE VIEW sc_dq.v_synthese
OPTIONS (description = "Résultat de la dernière exécution réussie de chaque règle active. taux_conformite_pct = 100 x (1 - anomalies / lignes de la table contrôlée) : indicatif pour les règles dont la clé n'est pas une ligne (lot-jour, épisode de température).")
AS
WITH dernier AS (
  SELECT *
  FROM sc_dq.dq_runs
  WHERE statut_execution = 'SUCCES'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY regle_id ORDER BY debut DESC) = 1
)
SELECT g.regle_id, g.libelle, g.dimension, g.severite, g.table_cible,
       d.nb_anomalies, d.nb_lignes_controlees,
       ROUND(100 * (1 - SAFE_DIVIDE(d.nb_anomalies, d.nb_lignes_controlees)), 3) AS taux_conformite_pct,
       d.debut AS controle_le, d.run_id, g.description
FROM sc_dq.dq_regles AS g
LEFT JOIN dernier AS d USING (regle_id)
WHERE g.active;

CREATE OR REPLACE VIEW sc_dq.v_anomalies_courantes
OPTIONS (description = "Anomalies détectées lors de la dernière exécution réussie de chaque règle, avec le libellé et la gravité de la règle.")
AS
WITH dernier AS (
  SELECT regle_id, run_id
  FROM sc_dq.dq_runs
  WHERE statut_execution = 'SUCCES'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY regle_id ORDER BY debut DESC) = 1
)
SELECT a.regle_id, g.libelle, g.dimension, g.severite, a.table_cible, a.cle_ligne, a.detail, a.detectee_le
FROM sc_dq.dq_anomalies AS a
JOIN dernier AS d ON d.regle_id = a.regle_id AND d.run_id = a.run_id
JOIN sc_dq.dq_regles AS g ON g.regle_id = a.regle_id;

CREATE OR REPLACE VIEW sc_eval.v_evaluation_dq
OPTIONS (description = "Rappel et précision de chaque règle face aux anomalies injectées par le générateur. rappel = part des anomalies injectées retrouvées ; precision = part des alertes qui sont de vraies anomalies.")
AS
WITH correspondance AS (
  SELECT * FROM UNNEST([
    STRUCT('DQ_UNI_001' AS regle_id, 'DOUBLON_TECHNIQUE' AS type_anomalie, 'commandes' AS table_cible),
    ('DQ_UNI_002', 'DOUBLON_TECHNIQUE', 'lignes_commande'),
    ('DQ_UNI_003', 'DOUBLON_TECHNIQUE', 'mouvements_stock'),
    ('DQ_UNI_004', 'DOUBLON_METIER', 'commandes'),
    ('DQ_COM_001', 'DATE_LIVRAISON_MANQUANTE', 'lignes_commande'),
    ('DQ_COM_002', 'DATE_EXPEDITION_MANQUANTE', 'lignes_commande'),
    ('DQ_COM_003', 'RELEVES_TEMPERATURE_MANQUANTS', 'releves_temperature'),
    ('DQ_COH_001', 'STATUT_ENTETE_INCOHERENT', 'commandes'),
    ('DQ_COH_002', 'STATUT_NON_MIS_A_JOUR', 'lignes_commande'),
    ('DQ_COH_003', 'DATES_INVERSEES', 'lignes_commande'),
    ('DQ_COH_004', 'SEQUENCE_STATUTS_IMPOSSIBLE', 'historique_statuts'),
    ('DQ_COH_005', 'ECART_STOCK_MOUVEMENT_PERDU', 'mouvements_stock'),
    ('DQ_EXA_001', 'QTE_EXPEDIEE_SUPERIEURE', 'lignes_commande'),
    ('DQ_EXA_002', 'ARTICLE_INCONNU', 'lignes_commande'),
    ('DQ_CNF_001', 'NON_RESPECT_FEFO', 'preparations'),
    ('DQ_CNF_002', 'EXPEDITION_LOT_RAPPELE', 'preparations'),
    ('DQ_CNF_003', 'REMISE_STOCK_SANS_DECISION_RP', 'retours'),
    ('DQ_CNF_004', 'RETOUR_HORS_DELAI_REMIS_EN_STOCK', 'retours'),
    ('DQ_CNF_005', 'ZONE_TEMPERATURE_INCOMPATIBLE', 'stock_photo'),
    ('DQ_CNF_006', 'EXCURSION_TEMPERATURE', 'releves_temperature')
  ])
),
injectees AS (
  SELECT DISTINCT c.regle_id, i.cle_ligne
  FROM correspondance AS c
  JOIN sc_eval.anomalies_injectees AS i
    ON i.type_anomalie = c.type_anomalie AND i.table_cible = c.table_cible
),
detectees AS (
  SELECT DISTINCT regle_id, cle_ligne
  FROM sc_dq.v_anomalies_courantes
),
paires AS (
  SELECT COALESCE(i.regle_id, d.regle_id) AS regle_id,
         i.cle_ligne AS cle_injectee, d.cle_ligne AS cle_detectee
  FROM injectees AS i
  FULL OUTER JOIN detectees AS d ON d.regle_id = i.regle_id AND d.cle_ligne = i.cle_ligne
),
scores AS (
  SELECT regle_id,
         COUNTIF(cle_injectee IS NOT NULL) AS nb_injectees,
         COUNTIF(cle_detectee IS NOT NULL) AS nb_detectees,
         COUNTIF(cle_injectee IS NOT NULL AND cle_detectee IS NOT NULL) AS vrais_positifs
  FROM paires
  GROUP BY regle_id
)
SELECT g.regle_id, g.libelle, c.type_anomalie,
       COALESCE(s.nb_injectees, 0) AS nb_injectees,
       COALESCE(s.nb_detectees, 0) AS nb_detectees,
       COALESCE(s.vrais_positifs, 0) AS vrais_positifs,
       ROUND(100 * SAFE_DIVIDE(s.vrais_positifs, s.nb_injectees), 1) AS rappel_pct,
       ROUND(100 * SAFE_DIVIDE(s.vrais_positifs, s.nb_detectees), 1) AS precision_pct
FROM sc_dq.dq_regles AS g
LEFT JOIN correspondance AS c USING (regle_id)
LEFT JOIN scores AS s USING (regle_id)
WHERE g.active
ORDER BY g.regle_id;
