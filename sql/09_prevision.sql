-- =====================================================================
-- 09_prevision.sql : prévision de la charge de préparation (BigQuery ML)
--
-- Utilisation : exécuter ce fichier une fois, puis
--   CALL sc_ml.prevoir_charge();
-- (à relancer après CALL sc_kpi.calculer_kpi(), par exemple chaque semaine)
--
-- Ce que l'on prévoit : le nombre de lignes à préparer par jour et par équipe
-- (matin, après-midi, nuit, et le site entier), sur les 4 semaines qui suivent
-- la fin des données. Puis on le traduit en heures de picking (à la productivité
-- réelle récente) et on compare aux heures de picking habituellement consacrées
-- ce jour de semaine. Les opérateurs sont polyvalents : le lundi, l'équipe de nuit
-- passe du rangement au picking sans renfort. Seul le surplus d'heures au-delà de
-- l'habitude est converti en renfort (équivalents temps plein) : c'est ce qui sert
-- à planifier l'intérim.
--
-- Modèle : ARIMA_PLUS de BigQuery ML, une série par équipe. Il décompose chaque
-- série en tendance + saisonnalité hebdomadaire (le lundi est le jour le plus
-- chargé : rattrapage du week-end) + effet des jours fériés français, puis
-- choisit automatiquement le meilleur modèle ARIMA pour le reste.
-- Les jours de fermeture (week-ends, fériés) sont mis à 0 d'après sc_raw.dim_calendrier.
--
-- Contrôle de la qualité (backtest) : un premier modèle est entraîné sans les
-- 4 dernières semaines, qui sont ensuite prévues et comparées au réel, ainsi
-- qu'à une méthode naïve (moyenne des 4 mêmes jours de semaine précédents).
-- Un modèle n'a d'intérêt que s'il bat cette méthode simple.
--
-- Coût : l'historique fait 4 séries de 365 jours (quelques Ko). L'entraînement
-- reste dans le palier gratuit de BigQuery ML (10 Go par mois).
-- =====================================================================

SET @@location = 'EU';

CREATE OR REPLACE PROCEDURE sc_ml.prevoir_charge()
OPTIONS (description = "Historique de charge, backtest (4 dernières semaines), modèle ARIMA_PLUS final et prévision à 28 jours traduite en heures et en préparateurs.")
BEGIN
  DECLARE fin_donnees DATE DEFAULT (SELECT MAX(date_kpi) FROM sc_kpi.kpi_jour);
  DECLARE debut_donnees DATE DEFAULT (SELECT MIN(date_kpi) FROM sc_kpi.kpi_jour);
  DECLARE fin_apprentissage_test DATE DEFAULT DATE_SUB(fin_donnees, INTERVAL 28 DAY);

  -- -------------------------------------------------------------------
  -- 1. Historique : une ligne par jour calendaire et par équipe (0 si fermé)
  -- -------------------------------------------------------------------
  CREATE OR REPLACE TABLE sc_ml.historique_charge
  OPTIONS (description = "Lignes préparées, heures de picking, personnes présentes et heures de présence par jour et par équipe. Jours fermés à 0.")
  AS
  -- DEBUT historique
  WITH equipes AS (
    SELECT equipe FROM UNNEST(['TOUS', 'MATIN', 'APRES_MIDI', 'NUIT']) AS equipe
  ),
  jours AS (
    SELECT DISTINCT date_jour AS jour, jour_ouvre
    FROM sc_raw.dim_calendrier
    WHERE date_jour BETWEEN debut_donnees AND fin_donnees
  ),
  volumes AS (
    SELECT date_kpi AS jour, perimetre_valeur AS equipe, numerateur AS lignes, denominateur AS heures
    FROM sc_kpi.kpi_jour
    WHERE kpi_code = 'PRODUCTIVITE_PICKING' AND perimetre_type IN ('GLOBAL', 'EQUIPE')
  ),
  -- les opérateurs sont polyvalents (picking, rangement, contrôle...) : on garde leur présence totale
  presents AS (
    SELECT date_travail AS jour, COALESCE(equipe, 'TOUS') AS equipe,
           COUNT(DISTINCT operateur_id) AS personnes, SUM(CAST(heures AS FLOAT64)) AS heures_presence
    FROM sc_raw.temps_travail
    GROUP BY GROUPING SETS ((date_travail, equipe), (date_travail))
  )
  SELECT
    j.jour, e.equipe, j.jour_ouvre,
    COALESCE(v.lignes, 0) AS lignes,
    COALESCE(v.heures, 0) AS heures_picking,
    COALESCE(p.personnes, 0) AS personnes,
    COALESCE(p.heures_presence, 0) AS heures_presence
  FROM jours AS j
  CROSS JOIN equipes AS e
  LEFT JOIN volumes AS v ON v.jour = j.jour AND v.equipe = e.equipe
  LEFT JOIN presents AS p ON p.jour = j.jour AND p.equipe = e.equipe
  -- FIN historique
  ;

  -- -------------------------------------------------------------------
  -- 2. Backtest : modèle entraîné sans les 4 dernières semaines
  -- -------------------------------------------------------------------
  CREATE OR REPLACE MODEL sc_ml.modele_charge_test
  OPTIONS (
    model_type = 'ARIMA_PLUS',
    time_series_timestamp_col = 'jour',
    time_series_data_col = 'lignes',
    time_series_id_col = 'equipe',
    data_frequency = 'DAILY',
    holiday_region = 'FR',
    horizon = 28,
    auto_arima = TRUE,
    decompose_time_series = TRUE
  ) AS
  SELECT jour, equipe, lignes
  FROM sc_ml.historique_charge
  -- même date de coupure que fin_apprentissage_test, écrite en SQL pur pour l'instruction CREATE MODEL
  WHERE jour <= DATE_SUB((SELECT MAX(jour) FROM sc_ml.historique_charge), INTERVAL 28 DAY);

  -- ML.FORECAST référence un modèle : BigQuery valide cette référence à la CREATION de la
  -- procédure, or le modèle n'existe pas encore. EXECUTE IMMEDIATE la met dans une chaîne,
  -- évaluée seulement à l'exécution, une fois le modèle construit. @coupure = date de coupure.
  EXECUTE IMMEDIATE """
  CREATE OR REPLACE TABLE sc_ml.backtest_charge
  OPTIONS (description = "4 dernières semaines : réel, prévision du modèle entraîné sans elles (intervalle à 90 %) et méthode naïve.")
  AS
  -- DEBUT backtest
  WITH prevu AS (
    SELECT equipe, DATE(forecast_timestamp) AS jour, forecast_value,
           prediction_interval_lower_bound AS borne_basse, prediction_interval_upper_bound AS borne_haute
    FROM ML.FORECAST(MODEL sc_ml.modele_charge_test, STRUCT(28 AS horizon, 0.9 AS confidence_level))
  ),
  -- méthode naïve : moyenne des 4 derniers jours ouvrés de même jour de semaine connus à la date de coupure
  naive AS (
    SELECT jour, equipe, AVG(lignes_passees) AS naive
    FROM (
      SELECT t.jour, t.equipe, p.lignes AS lignes_passees
      FROM sc_ml.historique_charge AS t
      JOIN sc_ml.historique_charge AS p
        ON p.equipe = t.equipe AND p.jour_ouvre AND p.jour <= @coupure
       AND EXTRACT(DAYOFWEEK FROM p.jour) = EXTRACT(DAYOFWEEK FROM t.jour)
      WHERE t.jour > @coupure
      QUALIFY ROW_NUMBER() OVER (PARTITION BY t.jour, t.equipe ORDER BY p.jour DESC) <= 4
    )
    GROUP BY jour, equipe
  )
  SELECT
    h.jour, h.equipe, h.jour_ouvre,
    h.lignes AS reel,
    IF(h.jour_ouvre, GREATEST(0, f.forecast_value), 0) AS prevu,
    IF(h.jour_ouvre, GREATEST(0, f.borne_basse), 0) AS borne_basse,
    IF(h.jour_ouvre, GREATEST(0, f.borne_haute), 0) AS borne_haute,
    IF(h.jour_ouvre, n.naive, 0) AS naive
  FROM sc_ml.historique_charge AS h
  LEFT JOIN prevu AS f ON f.jour = h.jour AND f.equipe = h.equipe
  LEFT JOIN naive AS n ON n.jour = h.jour AND n.equipe = h.equipe
  WHERE h.jour > @coupure
  -- FIN backtest
  """ USING fin_apprentissage_test AS coupure;

  -- -------------------------------------------------------------------
  -- 3. Modèle final sur tout l'historique, prévision à 28 jours
  -- -------------------------------------------------------------------
  CREATE OR REPLACE MODEL sc_ml.modele_charge
  OPTIONS (
    model_type = 'ARIMA_PLUS',
    time_series_timestamp_col = 'jour',
    time_series_data_col = 'lignes',
    time_series_id_col = 'equipe',
    data_frequency = 'DAILY',
    holiday_region = 'FR',
    horizon = 28,
    auto_arima = TRUE,
    decompose_time_series = TRUE
  ) AS
  SELECT jour, equipe, lignes
  FROM sc_ml.historique_charge;

  EXECUTE IMMEDIATE """
  CREATE OR REPLACE TABLE sc_ml.prevision_charge
  OPTIONS (description = "Prévision à 28 jours par équipe : lignes (intervalle à 90 %), heures de picking nécessaires comparées aux heures habituelles, renfort en équivalents temps plein.")
  AS
  -- DEBUT prevision
  WITH prevu AS (
    SELECT equipe, DATE(forecast_timestamp) AS jour, forecast_value,
           prediction_interval_lower_bound AS borne_basse, prediction_interval_upper_bound AS borne_haute
    FROM ML.FORECAST(MODEL sc_ml.modele_charge, STRUCT(28 AS horizon, 0.9 AS confidence_level))
  ),
  recent AS (            -- 8 dernières semaines ouvrées
    SELECT equipe,
           SAFE_DIVIDE(SUM(lignes), SUM(heures_picking)) AS productivite,          -- lignes par heure de picking
           SAFE_DIVIDE(SUM(heures_presence), SUM(personnes)) AS heures_par_poste    -- heures de présence d'une personne sur un poste
    FROM sc_ml.historique_charge
    WHERE jour_ouvre AND jour > DATE_SUB(@fin, INTERVAL 56 DAY)
    GROUP BY equipe
  ),
  habituel AS (          -- heures de picking habituellement consacrées, par équipe et jour de semaine
    SELECT equipe, EXTRACT(DAYOFWEEK FROM jour) AS jour_semaine,
           AVG(heures_picking) AS heures_habituelles, AVG(personnes) AS personnes_habituelles
    FROM sc_ml.historique_charge
    WHERE jour_ouvre AND jour > DATE_SUB(@fin, INTERVAL 56 DAY)
    GROUP BY 1, 2
  ),
  calcul AS (
    SELECT
      f.jour, f.equipe, c.jour_ouvre, c.libelle_ferie,
      IF(c.jour_ouvre, GREATEST(0, f.forecast_value), 0) AS lignes_prevues,
      IF(c.jour_ouvre, GREATEST(0, f.borne_basse), 0) AS borne_basse,
      IF(c.jour_ouvre, GREATEST(0, f.borne_haute), 0) AS borne_haute,
      r.productivite, r.heures_par_poste,
      IF(c.jour_ouvre, hb.heures_habituelles, 0) AS heures_habituelles,
      IF(c.jour_ouvre, hb.personnes_habituelles, 0) AS personnes_habituelles
    FROM prevu AS f
    JOIN (SELECT DISTINCT date_jour, jour_ouvre, libelle_ferie FROM sc_raw.dim_calendrier) AS c ON c.date_jour = f.jour
    LEFT JOIN recent AS r ON r.equipe = f.equipe
    LEFT JOIN habituel AS hb ON hb.equipe = f.equipe AND hb.jour_semaine = EXTRACT(DAYOFWEEK FROM f.jour)
  ),
  heures AS (
    SELECT *,
      SAFE_DIVIDE(lignes_prevues, productivite) AS h_necessaires,
      SAFE_DIVIDE(borne_haute, productivite) AS h_haute
    FROM calcul
  )
  SELECT
    jour, equipe, jour_ouvre, libelle_ferie, lignes_prevues, borne_basse, borne_haute,
    productivite, heures_par_poste, personnes_habituelles,
    ROUND(heures_habituelles, 1) AS heures_habituelles,
    ROUND(h_necessaires, 1) AS heures_necessaires,
    ROUND(h_haute, 1) AS heures_haute,
    ROUND(h_necessaires - heures_habituelles, 1) AS ecart_heures,
    -- écart en équivalents temps plein (postes) : heures manquantes / heures d'un poste
    ROUND(SAFE_DIVIDE(h_necessaires - heures_habituelles, heures_par_poste), 1) AS ecart_etp,
    -- renfort entier : arrondi, un écart de moins d'un demi-poste est absorbé par la polyvalence de l'équipe
    GREATEST(0, CAST(ROUND(SAFE_DIVIDE(h_necessaires - heures_habituelles, heures_par_poste)) AS INT64)) AS renfort_etp,
    GREATEST(0, CAST(ROUND(SAFE_DIVIDE(h_haute - heures_habituelles, heures_par_poste)) AS INT64)) AS renfort_securite_etp
  FROM heures
  -- FIN prevision
  """ USING fin_donnees AS fin;

  -- caractéristiques du modèle retenu pour chaque série (pour l'expliquer)
  EXECUTE IMMEDIATE """
  CREATE OR REPLACE TABLE sc_ml.modele_charge_info AS
  SELECT equipe, non_seasonal_p, non_seasonal_d, non_seasonal_q, has_drift, has_holiday_effect,
         has_spikes_and_dips, has_step_changes, seasonal_periods, AIC
  FROM ML.ARIMA_EVALUATE(MODEL sc_ml.modele_charge)
  """;

  -- Qualité (WAPE) mesurée sur le backtest : table, pour éviter une vue dépendant d'une table
  -- qui n'existe qu'après le premier passage de la procédure.
  CREATE OR REPLACE TABLE sc_ml.qualite_prevision AS
  -- DEBUT qualite
  SELECT
    equipe,
    COUNTIF(jour_ouvre) AS jours_ouvres,
    CAST(SUM(reel) AS INT64) AS lignes_reelles,
    ROUND(100 * SAFE_DIVIDE(SUM(IF(jour_ouvre, ABS(prevu - reel), 0)), SUM(reel)), 1) AS wape_modele_pct,
    ROUND(100 * SAFE_DIVIDE(SUM(IF(jour_ouvre, ABS(naive - reel), 0)), SUM(reel)), 1) AS wape_naive_pct,
    ROUND(100 * SAFE_DIVIDE(SUM(prevu) - SUM(reel), SUM(reel)), 1) AS biais_pct,
    ROUND(100 * SAFE_DIVIDE(COUNTIF(jour_ouvre AND reel BETWEEN borne_basse AND borne_haute), COUNTIF(jour_ouvre)), 1) AS couverture_intervalle_pct
  FROM sc_ml.backtest_charge
  GROUP BY equipe
  -- FIN qualite
  ;

  SELECT * FROM sc_ml.qualite_prevision ORDER BY equipe;
END;
