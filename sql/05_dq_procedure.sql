-- =====================================================================
-- 05_dq_procedure.sql : procédure qui exécute les règles de fiabilisation
--
-- Utilisation (console BigQuery) :
--   CALL sc_dq.executer_controles(NULL);          -- toutes les règles actives
--   CALL sc_dq.executer_controles('DQ_COH_005');  -- une seule règle
--
-- Pourquoi une procédure stockée plutôt qu'un script Python :
-- * tout le calcul reste dans BigQuery, aucune donnée ne transite par un PC ;
-- * elle peut être planifiée telle quelle (requête programmée, gratuite) ;
-- * l'agent n'aura qu'une instruction à lancer : CALL sc_dq.executer_controles(...).
--
-- Chaque exécution reçoit un identifiant (run_id). Les anomalies trouvées vont
-- dans sc_dq.dq_anomalies, et une ligne de bilan par règle va dans sc_dq.dq_runs.
-- Si une règle plante, l'erreur est enregistrée et les autres règles continuent.
--
-- Coût : une exécution complète lit environ quelques centaines de Mo,
-- sur le palier gratuit de 1 Tio de requêtes par mois.
-- =====================================================================

SET @@location = 'EU';

CREATE OR REPLACE PROCEDURE sc_dq.executer_controles(p_regle_id STRING)
OPTIONS (description = "Exécute les règles actives de sc_dq.dq_regles (ou une seule si p_regle_id est renseigné) et enregistre les anomalies et le bilan de l'exécution.")
BEGIN
  DECLARE v_run_id STRING DEFAULT GENERATE_UUID();
  DECLARE v_debut TIMESTAMP;
  DECLARE v_nb INT64;
  DECLARE v_total INT64;

  FOR r IN (
    SELECT regle_id, table_cible, requete_sql
    FROM sc_dq.dq_regles
    WHERE active AND (p_regle_id IS NULL OR regle_id = p_regle_id)
    ORDER BY regle_id
  )
  DO
    SET v_debut = CURRENT_TIMESTAMP();
    BEGIN
      -- 1. La requête de la règle est insérée dans un INSERT ... SELECT et exécutée.
      EXECUTE IMMEDIATE FORMAT("""
        INSERT INTO sc_dq.dq_anomalies (anomalie_id, run_id, regle_id, table_cible, cle_ligne, detail, detectee_le)
        SELECT GENERATE_UUID(), @run_id, @regle_id, @table_cible, CAST(cle_ligne AS STRING), detail, CURRENT_TIMESTAMP()
        FROM (%s)
      """, r.requete_sql)
      USING v_run_id AS run_id, r.regle_id AS regle_id, r.table_cible AS table_cible;

      -- 2. Bilan : nombre d'anomalies trouvées, et taille de la table contrôlée
      --    (lue dans les métadonnées, requête gratuite).
      SET v_nb = (
        SELECT COUNT(*) FROM sc_dq.dq_anomalies
        WHERE run_id = v_run_id AND regle_id = r.regle_id AND detectee_le >= v_debut
      );
      SET v_total = (SELECT row_count FROM sc_raw.__TABLES__ WHERE table_id = r.table_cible);

      INSERT INTO sc_dq.dq_runs
        (run_id, regle_id, debut, fin, nb_lignes_controlees, nb_anomalies, statut_execution, message_erreur)
      VALUES
        (v_run_id, r.regle_id, v_debut, CURRENT_TIMESTAMP(), v_total, v_nb, 'SUCCES', NULL);

    EXCEPTION WHEN ERROR THEN
      INSERT INTO sc_dq.dq_runs
        (run_id, regle_id, debut, fin, nb_lignes_controlees, nb_anomalies, statut_execution, message_erreur)
      VALUES
        (v_run_id, r.regle_id, v_debut, CURRENT_TIMESTAMP(), NULL, NULL, 'ECHEC', @@error.message);
    END;
  END FOR;

  -- 3. Résultat affiché à la fin de l'exécution
  SELECT r.regle_id, g.libelle, g.dimension, g.severite, r.nb_anomalies,
         r.statut_execution, r.message_erreur,
         TIMESTAMP_DIFF(r.fin, r.debut, SECOND) AS duree_s
  FROM sc_dq.dq_runs AS r
  JOIN sc_dq.dq_regles AS g USING (regle_id)
  WHERE r.run_id = v_run_id
  ORDER BY CASE g.severite WHEN 'CRITIQUE' THEN 1 WHEN 'MAJEURE' THEN 2 ELSE 3 END, r.nb_anomalies DESC;
END;
