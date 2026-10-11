-- =====================================================================
-- 08_lean.sql : détection des dérives de performance (amélioration continue)
--
-- Utilisation : exécuter ce fichier une fois, puis
--   CALL sc_lean.detecter_derives();
-- (à relancer après CALL sc_kpi.calculer_kpi())
--
-- Méthode : cartes de contrôle hebdomadaires (Shewhart), une par KPI et par périmètre.
--
-- 1. Taux (OTIF, ruptures, erreurs, créneaux...) : carte p' de Laney.
--    Une carte p classique suppose que la seule variation est le hasard binomial.
--    Avec 17 000 lignes par semaine, ses limites deviennent si étroites que tout
--    déclenche une alerte. La carte p' mesure la variation réelle d'une semaine à
--    l'autre (étendue mobile des scores z) et élargit les limites d'autant.
-- 2. Ratios continus (performance picking) : carte des valeurs individuelles (XmR),
--    sigma = étendue mobile moyenne / 1,128.
-- 3. Référence glissante : les 12 semaines précédentes. Deux passes : la seconde
--    recalcule la référence sans les semaines hors contrôle de la première, sinon
--    une forte dérive gonfle la référence et masque les semaines suivantes.
-- 4. Règles (côté défavorable uniquement) :
--      R1 : un point au-delà de 3 sigma ;
--      R2 : 2 points sur 3 consécutifs au-delà de 2 sigma.
--    Filtres de pertinence : écart minimal en valeur métier (ex. 0,5 pt d'OTIF),
--    volume minimal, au moins 3 échecs de plus que l'attendu, 6 semaines d'historique.
-- 5. Semaines consécutives en alerte = un épisode. Les épisodes simultanés d'une
--    même famille (une rupture fait aussi baisser l'OTIF du site, du donneur, des
--    transporteurs) sont regroupés en UN incident : une dérive principale (la plus
--    forte) et des signaux liés. C'est ce que voit le responsable.
-- =====================================================================

SET @@location = 'EU';

-- Colonnes ajoutées aux tables créées par 03_pilotage.sql (sans perte des données existantes)
ALTER TABLE sc_lean.derives
  ADD COLUMN IF NOT EXISTS famille STRING OPTIONS(description="SERVICE, PREPARATION, TRANSPORT, CLIENT"),
  ADD COLUMN IF NOT EXISTS gravite STRING OPTIONS(description="CRITIQUE, MAJEURE, MINEURE"),
  ADD COLUMN IF NOT EXISTS z_max FLOAT64 OPTIONS(description="Ecart maximal à la référence, en nombre de sigma"),
  ADD COLUMN IF NOT EXISTS nb_semaines INT64,
  ADD COLUMN IF NOT EXISTS regle STRING OPTIONS(description="R1 (point hors 3 sigma) ou R2 (2 sur 3 au-delà de 2 sigma)"),
  ADD COLUMN IF NOT EXISTS unite STRING,
  ADD COLUMN IF NOT EXISTS sens STRING,
  ADD COLUMN IF NOT EXISTS signaux JSON OPTIONS(description="Autres KPI ou périmètres en dérive sur la même période (même incident)"),
  ADD COLUMN IF NOT EXISTS maj_le TIMESTAMP;

ALTER TABLE sc_lean.actions
  ADD COLUMN IF NOT EXISTS date_effet DATE OPTIONS(description="Date de mise en oeuvre, à partir de laquelle on mesure l'efficacité"),
  ADD COLUMN IF NOT EXISTS commentaire STRING,
  ADD COLUMN IF NOT EXISTS cree_par STRING OPTIONS(description="interface ou copilote"),
  ADD COLUMN IF NOT EXISTS maj_le TIMESTAMP;

CREATE OR REPLACE PROCEDURE sc_lean.detecter_derives()
OPTIONS (description = "Cartes de contrôle hebdomadaires sur sc_kpi.kpi_jour, puis détection et regroupement des dérives dans sc_lean.derives (les statuts et analyses déjà saisis sont conservés).")
BEGIN

  -- -------------------------------------------------------------------
  -- 1. Cartes de contrôle : une ligne par semaine, KPI et périmètre
  -- -------------------------------------------------------------------
  CREATE OR REPLACE TABLE sc_lean.cartes_controle
  CLUSTER BY kpi_code, perimetre_type, perimetre_valeur
  OPTIONS (description = "Cartes de contrôle hebdomadaires : valeur, ligne centrale (référence des 12 semaines précédentes), limites à 3 sigma, alerte. Valeurs dans l'unité du KPI (%, ‰, lignes/h).")
  AS
  -- DEBUT cartes_controle
  WITH params AS (
    -- carte : P = taux (p' de Laney), I = valeurs individuelles ; seuil_min dans l'unité affichée
    SELECT 'OTIF' AS kpi_code, 'P' AS carte, 'HAUSSE_FAVORABLE' AS sens, 100.0 AS facteur, '%' AS unite, 0.5 AS seuil_min, 200.0 AS den_min
    UNION ALL SELECT 'TAUX_RUPTURE', 'P', 'BAISSE_FAVORABLE', 100.0, '%', 0.3, 200.0
    UNION ALL SELECT 'LIGNES_NON_MESURABLES', 'P', 'BAISSE_FAVORABLE', 100.0, '%', 0.3, 200.0
    UNION ALL SELECT 'ERREUR_PREPARATION', 'P', 'BAISSE_FAVORABLE', 100.0, '%', 0.1, 500.0
    UNION ALL SELECT 'RESPECT_CRENEAUX', 'P', 'HAUSSE_FAVORABLE', 100.0, '%', 5.0, 5.0
    UNION ALL SELECT 'RECLAMATIONS_ENTREPOT', 'P', 'BAISSE_FAVORABLE', 1000.0, '‰', 0.15, 2000.0
    UNION ALL SELECT 'PERFORMANCE_PICKING', 'I', 'HAUSSE_FAVORABLE', 100.0, '%', 3.0, 20.0
  ),
  semaines AS (
    SELECT DATE_TRUNC(k.date_kpi, WEEK(MONDAY)) AS semaine, k.kpi_code, k.perimetre_type, k.perimetre_valeur,
           SUM(k.numerateur) AS num, SUM(k.denominateur) AS den
    FROM sc_kpi.kpi_jour AS k
    JOIN params AS p ON p.kpi_code = k.kpi_code
    -- une rupture ne dépend pas du transporteur : cette ventilation ne ferait que dupliquer le signal
    WHERE NOT (k.kpi_code = 'TAUX_RUPTURE' AND k.perimetre_type = 'TRANSPORTEUR')
    GROUP BY 1, 2, 3, 4
  ),
  -- Passe 1 : référence glissante sur les 12 semaines précédentes
  p1 AS (
    SELECT s.*, p.carte, p.sens, p.facteur, p.unite, p.seuil_min, p.den_min,
      s.num / s.den AS v,
      SUM(s.num) OVER w / SUM(s.den) OVER w AS p_ref,
      AVG(s.num / s.den) OVER w AS m_ref
    FROM semaines AS s JOIN params AS p ON p.kpi_code = s.kpi_code
    WINDOW w AS (PARTITION BY s.kpi_code, s.perimetre_type, s.perimetre_valeur ORDER BY s.semaine ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING)
  ),
  p1b AS (
    SELECT *,
      IF(carte = 'P', SAFE_DIVIDE(v - p_ref, SQRT(p_ref * (1 - p_ref) / den)), NULL) AS zb,
      ABS(v - LAG(v) OVER s) AS mr_v
    FROM p1 WINDOW s AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine)
  ),
  p1c AS (
    SELECT *, ABS(zb - LAG(zb) OVER s) AS mr_z
    FROM p1b WINDOW s AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine)
  ),
  p1d AS (
    SELECT *,
      IF(carte = 'P', SAFE_DIVIDE(zb, GREATEST(1.0, COALESCE(AVG(mr_z) OVER w / 1.128, 1.0))),
                      SAFE_DIVIDE(v - m_ref, AVG(mr_v) OVER w / 1.128)) AS z1
    FROM p1c WINDOW w AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING)
  ),
  -- Passe 2 : même calcul en écartant de la référence les semaines hors contrôle de la passe 1
  p2 AS (
    SELECT *, COALESCE(ABS(z1) <= 3, TRUE) AS stable FROM p1d
  ),
  p2b AS (
    SELECT *, stable AND COALESCE(LAG(stable) OVER s, FALSE) AS paire_stable
    FROM p2 WINDOW s AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine)
  ),
  p2c AS (
    SELECT *,
      SUM(IF(stable, num, 0)) OVER w / NULLIF(SUM(IF(stable, den, 0)) OVER w, 0) AS p_ref2,
      AVG(IF(stable, v, NULL)) OVER w AS m_ref2,
      AVG(IF(paire_stable, mr_v, NULL)) OVER w / 1.128 AS sigma_i,
      COUNTIF(stable) OVER w AS nb_hist
    FROM p2b WINDOW w AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING)
  ),
  p2d AS (
    SELECT *, IF(carte = 'P', SAFE_DIVIDE(v - p_ref2, SQRT(p_ref2 * (1 - p_ref2) / den)), NULL) AS zb2
    FROM p2c
  ),
  p2e AS (
    SELECT *, IF(paire_stable, ABS(zb2 - LAG(zb2) OVER s), NULL) AS mr_z2
    FROM p2d WINDOW s AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine)
  ),
  p2f AS (
    SELECT *, GREATEST(1.0, COALESCE(AVG(mr_z2) OVER w / 1.128, 1.0)) AS sigma_z
    FROM p2e WINDOW w AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING)
  ),
  c1 AS (
    SELECT *,
      IF(carte = 'P', p_ref2, m_ref2) AS centre,
      IF(carte = 'P', SQRT(p_ref2 * (1 - p_ref2) / den) * sigma_z, sigma_i) AS sigma_point
    FROM p2f
  ),
  c2 AS (
    SELECT *,
      SAFE_DIVIDE(v - centre, sigma_point) * IF(sens = 'HAUSSE_FAVORABLE', -1, 1) AS z_defav,
      (v - centre) * IF(sens = 'HAUSSE_FAVORABLE', -1, 1) * facteur AS ecart_defav,
      IF(carte = 'P', (v - centre) * IF(sens = 'HAUSSE_FAVORABLE', -1, 1) * den, NULL) AS exces_echecs
    FROM c1
  ),
  c3 AS (
    SELECT *,
      LAG(z_defav, 1) OVER s AS z_m1, LAG(z_defav, 2) OVER s AS z_m2,
      LEAD(z_defav, 1) OVER s AS z_p1, LEAD(z_defav, 2) OVER s AS z_p2
    FROM c2 WINDOW s AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine)
  ),
  c4 AS (
    SELECT *,
      COALESCE(z_defav > 3, FALSE) AS regle_1,
      COALESCE(z_defav > 2 AND (z_m1 > 2 OR z_m2 > 2 OR z_p1 > 2 OR z_p2 > 2), FALSE) AS regle_2
    FROM c3
  )
  SELECT
    semaine, kpi_code, perimetre_type, perimetre_valeur, carte, sens, unite,
    ROUND(v * facteur, 4) AS valeur,
    ROUND(centre * facteur, 4) AS centre,
    -- GREATEST / LEAST renvoient NULL si un argument est NULL (semaines sans historique)
    IF(sigma_point IS NULL, NULL, ROUND(LEAST(IF(unite = '%', 100, 1e9), (centre + 3 * sigma_point) * facteur), 4)) AS lcs,
    IF(sigma_point IS NULL, NULL, ROUND(GREATEST(0, (centre - 3 * sigma_point) * facteur), 4)) AS lci,
    ROUND(z_defav, 2) AS z_defav,
    ROUND(ecart_defav, 4) AS ecart_defav,
    ROUND(exces_echecs, 1) AS exces_echecs,
    num AS numerateur, den AS denominateur, nb_hist,
    regle_1, regle_2,
    COALESCE(nb_hist >= 6 AND den >= den_min AND ecart_defav >= seuil_min
             AND COALESCE(exces_echecs >= 3, TRUE) AND (regle_1 OR regle_2), FALSE) AS alerte
  FROM c4
  -- FIN cartes_controle
  ;

  -- -------------------------------------------------------------------
  -- 2. Episodes, regroupement en incidents, mise à jour de sc_lean.derives
  -- -------------------------------------------------------------------
  CREATE OR REPLACE TEMP TABLE incidents AS
  -- DEBUT incidents
  WITH fin_donnees AS (
    SELECT MAX(date_kpi) AS d FROM sc_kpi.kpi_jour
  ),
  marques AS (
    SELECT *, alerte AND NOT COALESCE(LAG(alerte) OVER s, FALSE) AS debut_episode
    FROM sc_lean.cartes_controle
    WINDOW s AS (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine)
  ),
  numerotes AS (
    SELECT *, SUM(IF(debut_episode, 1, 0)) OVER (PARTITION BY kpi_code, perimetre_type, perimetre_valeur ORDER BY semaine) AS episode
    FROM marques
  ),
  episodes AS (
    SELECT
      kpi_code, perimetre_type, perimetre_valeur, episode,
      ANY_VALUE(unite) AS unite, ANY_VALUE(sens) AS sens,
      CASE
        WHEN kpi_code IN ('OTIF', 'TAUX_RUPTURE', 'LIGNES_NON_MESURABLES') THEN 'SERVICE'
        WHEN kpi_code IN ('ERREUR_PREPARATION', 'PERFORMANCE_PICKING') THEN 'PREPARATION'
        WHEN kpi_code = 'RESPECT_CRENEAUX' THEN 'TRANSPORT'
        ELSE 'CLIENT'
      END AS famille,
      MIN(semaine) AS debut,
      LEAST(DATE_ADD(MAX(semaine), INTERVAL 6 DAY), ANY_VALUE(f.d)) AS fin,
      COUNT(*) AS nb_semaines,
      MAX(z_defav) AS z_max,
      LOGICAL_OR(regle_1) AS avec_r1,
      -- valeur observée sur l'épisode = somme/somme (ou moyenne des semaines pour une carte I)
      IF(ANY_VALUE(carte) = 'P', SUM(numerateur) / SUM(denominateur) * IF(ANY_VALUE(unite) = '‰', 1000, 100), AVG(valeur)) AS valeur_observee,
      ARRAY_AGG(centre ORDER BY semaine LIMIT 1)[OFFSET(0)] AS valeur_reference,
      ARRAY_AGG(IF(sens = 'HAUSSE_FAVORABLE', lci, lcs) ORDER BY semaine LIMIT 1)[OFFSET(0)] AS limite_controle
    FROM numerotes CROSS JOIN fin_donnees AS f
    WHERE alerte
    GROUP BY kpi_code, perimetre_type, perimetre_valeur, episode
  ),
  ordonnes AS (
    SELECT *,
      MAX(fin) OVER (PARTITION BY famille ORDER BY debut, kpi_code, perimetre_type, perimetre_valeur
                     ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS fin_precedente
    FROM episodes
  ),
  groupes AS (
    SELECT *,
      SUM(IF(fin_precedente IS NULL OR debut > fin_precedente, 1, 0))
        OVER (PARTITION BY famille ORDER BY debut, kpi_code, perimetre_type, perimetre_valeur) AS incident
    FROM ordonnes
  ),
  classes AS (
    SELECT *,
      ROW_NUMBER() OVER (PARTITION BY famille, incident
                         ORDER BY z_max DESC, IF(perimetre_type = 'GLOBAL', 1, 0), kpi_code) AS rang,
      MIN(debut) OVER (PARTITION BY famille, incident) AS debut_incident,
      MAX(fin) OVER (PARTITION BY famille, incident) AS fin_incident
    FROM groupes
  ),
  -- signaux liés : les autres épisodes du même incident (agrégés puis joints,
  -- BigQuery n'accepte pas ici une sous-requête corrélée)
  signaux AS (
    SELECT famille, incident,
      TO_JSON(ARRAY_AGG(STRUCT(kpi_code, perimetre_type, perimetre_valeur, debut, fin,
                               ROUND(valeur_observee, 3) AS valeur_observee,
                               ROUND(valeur_reference, 3) AS valeur_reference,
                               ROUND(z_max, 1) AS z_max, unite)
                        ORDER BY z_max DESC)) AS signaux
    FROM classes
    WHERE rang > 1
    GROUP BY famille, incident
  )
  SELECT
    CONCAT('DER-', UPPER(SUBSTR(TO_HEX(MD5(CONCAT(p.kpi_code, '|', p.perimetre_type, '|', p.perimetre_valeur, '|',
                                                  CAST(p.debut AS STRING)))), 1, 8))) AS derive_id,
    p.kpi_code, p.perimetre_type, p.perimetre_valeur, p.famille, p.unite, p.sens,
    p.debut_incident AS periode_debut, p.fin_incident AS periode_fin,
    p.valeur_observee, p.valeur_reference, p.limite_controle,
    p.z_max, p.nb_semaines, IF(p.avec_r1, 'R1', 'R2') AS regle,
    CASE WHEN p.z_max >= 8 OR (p.z_max >= 4 AND p.nb_semaines >= 3) THEN 'CRITIQUE'
         WHEN p.z_max >= 4 OR p.nb_semaines >= 2 THEN 'MAJEURE'
         ELSE 'MINEURE' END AS gravite,
    sg.signaux
  FROM classes AS p
  LEFT JOIN signaux AS sg ON sg.famille = p.famille AND sg.incident = p.incident
  WHERE p.rang = 1
  -- FIN incidents
  ;

  MERGE sc_lean.derives AS t
  USING incidents AS s
  ON t.derive_id = s.derive_id
  WHEN MATCHED THEN UPDATE SET
    kpi_code = s.kpi_code, perimetre_type = s.perimetre_type, perimetre_valeur = s.perimetre_valeur,
    periode_debut = s.periode_debut, periode_fin = s.periode_fin,
    valeur_observee = CAST(ROUND(s.valeur_observee, 4) AS NUMERIC),
    valeur_reference = CAST(ROUND(s.valeur_reference, 4) AS NUMERIC),
    limite_controle = CAST(ROUND(s.limite_controle, 4) AS NUMERIC),
    famille = s.famille, gravite = s.gravite, z_max = s.z_max, nb_semaines = s.nb_semaines, regle = s.regle,
    unite = s.unite, sens = s.sens, signaux = s.signaux, maj_le = CURRENT_TIMESTAMP()
  WHEN NOT MATCHED BY TARGET THEN INSERT
    (derive_id, kpi_code, perimetre_type, perimetre_valeur, periode_debut, periode_fin, valeur_observee, valeur_reference,
     limite_controle, methode, analyse_causes, statut, detectee_le, famille, gravite, z_max, nb_semaines, regle, unite, sens,
     signaux, maj_le)
  VALUES
    (s.derive_id, s.kpi_code, s.perimetre_type, s.perimetre_valeur, s.periode_debut, s.periode_fin,
     CAST(ROUND(s.valeur_observee, 4) AS NUMERIC), CAST(ROUND(s.valeur_reference, 4) AS NUMERIC),
     CAST(ROUND(s.limite_controle, 4) AS NUMERIC), 'CARTE_CONTROLE_LANEY', NULL, 'NOUVELLE', CURRENT_TIMESTAMP(),
     s.famille, s.gravite, s.z_max, s.nb_semaines, s.regle, s.unite, s.sens, s.signaux, CURRENT_TIMESTAMP())
  -- une alerte jamais traitée qui n'est plus détectée (données corrigées) disparaît ;
  -- une dérive déjà analysée ou traitée est conservée
  WHEN NOT MATCHED BY SOURCE AND t.statut = 'NOUVELLE' THEN DELETE;

  SELECT famille, gravite, COUNT(*) AS nb_derives
  FROM sc_lean.derives
  GROUP BY famille, gravite
  ORDER BY famille, gravite;
END;

-- ---------------------------------------------------------------------
-- Evaluation : les scénarios injectés par le générateur sont-ils retrouvés ?
-- (vue réservée à l'évaluation : l'agent n'a pas accès à sc_eval)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW sc_eval.v_evaluation_derives AS
WITH attendu AS (
  SELECT scenario_id, libelle, debut, fin,
    CASE scenario_id
      WHEN 'S1' THEN 'ERREUR_PREPARATION|EQUIPE|NUIT'
      WHEN 'S2' THEN 'RESPECT_CRENEAUX|TRANSPORTEUR|T01'
      WHEN 'S3' THEN 'TAUX_RUPTURE|DONNEUR|'
    END AS cible,
    CASE scenario_id
      WHEN 'S4' THEN 'Hors périmètre : non-conformité détectée par le contrôle DQ_CNF_002'
      WHEN 'S5' THEN 'Hors périmètre : non-conformité détectée par les contrôles DQ_CNF_003 et DQ_CNF_004'
    END AS remarque
  FROM sc_eval.scenarios
),
signaux AS (
  -- dérive principale + signaux liés, chacun avec sa propre période
  SELECT d.derive_id, CONCAT(d.kpi_code, '|', d.perimetre_type, '|', d.perimetre_valeur) AS cle,
         d.periode_debut AS debut, d.periode_fin AS fin, d.gravite
  FROM sc_lean.derives AS d
  UNION ALL
  SELECT d.derive_id,
         CONCAT(JSON_VALUE(s, '$.kpi_code'), '|', JSON_VALUE(s, '$.perimetre_type'), '|', JSON_VALUE(s, '$.perimetre_valeur')),
         DATE(JSON_VALUE(s, '$.debut')), DATE(JSON_VALUE(s, '$.fin')), d.gravite
  FROM sc_lean.derives AS d, UNNEST(JSON_QUERY_ARRAY(d.signaux)) AS s
)
SELECT
  a.scenario_id, a.libelle, a.debut, a.fin,
  COALESCE(a.remarque,
           IF(COUNT(s.derive_id) > 0, 'Détecté', 'Non détecté')) AS resultat,
  STRING_AGG(DISTINCT s.derive_id) AS derives,
  MIN(s.debut) AS premiere_semaine_detectee,
  DATE_DIFF(MIN(s.debut), a.debut, DAY) AS delai_detection_jours
FROM attendu AS a
LEFT JOIN signaux AS s
  ON a.cible IS NOT NULL AND STARTS_WITH(s.cle, a.cible) AND s.debut <= a.fin AND s.fin >= a.debut
GROUP BY a.scenario_id, a.libelle, a.debut, a.fin, a.remarque
ORDER BY a.scenario_id;
