-- =====================================================================
-- 07_kpi.sql : calcul des indicateurs de performance (KPI)
--
-- Utilisation : exécuter ce fichier une fois, puis
--   CALL sc_kpi.calculer_kpi();
-- (à relancer après chaque rechargement des données ou exécution des contrôles)
--
-- Trois principes :
-- 1. TRAÇABILITE. sc_kpi.lignes_service garde UNE LIGNE PAR LIGNE DE COMMANDE avec
--    tous les indicateurs élémentaires (à l'heure ? complète ? mesurable ? pourquoi ?).
--    Tout KPI de service se recalcule à partir d'elle et chaque chiffre se décompose
--    jusqu'à la ligne de donnée.
-- 2. AGREGATION CORRECTE. sc_kpi.kpi_jour stocke NUMERATEUR et DENOMINATEUR par jour
--    et par périmètre, jamais un pourcentage. Un taux mensuel = somme des numérateurs
--    / somme des dénominateurs, et non la moyenne des taux quotidiens (qui donnerait
--    le même poids à un jour de 50 lignes et à un jour de 5 000).
-- 3. BRUT CONTRE FIABILISE. L'OTIF "brut" reproduit le calcul naïf d'un tableau de
--    bord branché directement sur les données ; l'OTIF fiabilisé applique les règles
--    de traitement des anomalies détectées par sc_dq. L'écart entre les deux mesure
--    le coût de la non-qualité des données.
--
-- Règles de fiabilisation appliquées aux lignes de commande (version 1) :
--   doublon technique (DQ_UNI_001/002)   -> dédoublonné, compté une fois
--   doublon métier (DQ_UNI_004)          -> EXCLU (commande ressaisie, jamais servie)
--   annulation client (sans motif)       -> EXCLU
--   promesse postérieure aux données     -> EXCLU (pas encore due)
--   livrée sans date (DQ_COM_001)        -> NON_MESURABLE (ni réussite ni échec, compté à part)
--   livraison avant expédition (DQ_COH_003) -> NON_MESURABLE
--   statut non mis à jour (DQ_COH_002)   -> MESURE avec la date de livraison existante
--   rupture ou lot bloqué                -> MESURE, en échec (le client n'a pas été servi)
-- =====================================================================

SET @@location = 'EU';

CREATE OR REPLACE PROCEDURE sc_kpi.calculer_kpi()
OPTIONS (description = "Recalcule sc_kpi.lignes_service et sc_kpi.kpi_jour à partir de sc_raw et des anomalies de sc_dq, et met à jour les définitions des KPI.")
BEGIN

  -- -------------------------------------------------------------------
  -- 1. Une ligne par ligne de commande, avec ses indicateurs élémentaires
  -- -------------------------------------------------------------------
  CREATE OR REPLACE TABLE sc_kpi.lignes_service
  PARTITION BY DATE_TRUNC(date_promise, MONTH)
  CLUSTER BY donneur_ordre_id, transporteur_id
  OPTIONS (description = "Une ligne par ligne de commande dédoublonnée : statut de mesure, raison d'exclusion, à l'heure, complète, OTIF. Base de traçabilité des KPI de service.")
  AS
  -- DEBUT lignes_service
  WITH fin AS (
    SELECT MAX(DATE(date_creation, 'Europe/Paris')) AS d FROM sc_raw.commandes
  ),
  lignes AS (
    SELECT *, COUNT(*) OVER (PARTITION BY ligne_id) AS nb_occurrences
    FROM sc_raw.lignes_commande
    WHERE TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ligne_id ORDER BY _charge_le) = 1
  ),
  cmd AS (
    SELECT * FROM sc_raw.commandes
    WHERE TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY commande_id ORDER BY _charge_le) = 1
  ),
  doublons_metier AS (
    SELECT DISTINCT cle_ligne AS commande_id
    FROM sc_dq.v_anomalies_courantes
    WHERE regle_id = 'DQ_UNI_004'
  ),
  base AS (
    SELECT
      l.ligne_id, l.commande_id, c.donneur_ordre_id, c.destinataire_id, c.transporteur_id, l.article_id,
      l.date_commande, l.date_promise, c.date_livraison_demandee,
      DATE(l.date_livraison_reelle, 'Europe/Paris') AS date_livraison,
      l.qte_commandee, COALESCE(l.qte_livree, 0) AS qte_livree,
      l.statut_ligne, l.motif_ecart, l.nb_occurrences,
      l.date_livraison_reelle IS NULL AS sans_date_livraison,
      COALESCE(l.date_livraison_reelle < l.date_expedition_reelle, FALSE) AS dates_incoherentes,
      dm.commande_id IS NOT NULL AS est_doublon_metier,
      fin.d AS fin_donnees
    FROM lignes AS l
    LEFT JOIN cmd AS c ON c.commande_id = l.commande_id
    LEFT JOIN doublons_metier AS dm ON dm.commande_id = l.commande_id
    CROSS JOIN fin
  ),
  qualifiee AS (
    SELECT *,
      CASE
        WHEN est_doublon_metier THEN 'DOUBLON_METIER'
        WHEN statut_ligne = 'ANNULEE' AND motif_ecart IS NULL THEN 'ANNULATION_CLIENT'
        WHEN date_promise > fin_donnees THEN 'PAS_ENCORE_DUE'
        WHEN statut_ligne = 'LIVREE' AND sans_date_livraison THEN 'DATE_LIVRAISON_MANQUANTE'
        WHEN dates_incoherentes THEN 'DATES_INCOHERENTES'
      END AS raison_exclusion
    FROM base
  )
  SELECT
    ligne_id, commande_id, donneur_ordre_id, destinataire_id, transporteur_id, article_id,
    date_commande, date_promise, date_livraison_demandee, date_livraison,
    qte_commandee, qte_livree, statut_ligne, motif_ecart, nb_occurrences, raison_exclusion,
    CASE
      WHEN raison_exclusion IN ('DOUBLON_METIER', 'ANNULATION_CLIENT', 'PAS_ENCORE_DUE') THEN 'EXCLU'
      WHEN raison_exclusion IS NOT NULL THEN 'NON_MESURABLE'
      ELSE 'MESURE'
    END AS statut_mesure,
    COALESCE(date_livraison <= date_promise, FALSE) AS a_l_heure,
    qte_livree >= qte_commandee AS complete,
    COALESCE(date_livraison <= date_promise, FALSE) AND qte_livree >= qte_commandee AS otif,
    DATE_DIFF(COALESCE(date_livraison, fin_donnees), date_promise, DAY) AS retard_jours,
    motif_ecart IN ('RUPTURE', 'LOT_BLOQUE') AS rupture,
    -- vue "tableau de bord naïf" : lignes au statut LIVREE avec une date, doublons compris
    statut_ligne = 'LIVREE' AND NOT sans_date_livraison AS eligible_brut
  FROM qualifiee
  -- FIN lignes_service
  ;

  -- -------------------------------------------------------------------
  -- 2. Numérateurs et dénominateurs par jour et par périmètre
  -- -------------------------------------------------------------------
  CREATE OR REPLACE TABLE sc_kpi.kpi_jour
  PARTITION BY DATE_TRUNC(date_kpi, MONTH)
  CLUSTER BY kpi_code, perimetre_type
  OPTIONS (description = "Numérateur et dénominateur de chaque KPI par jour et par périmètre (GLOBAL, DONNEUR, TRANSPORTEUR, EQUIPE). Valeur d'une période = SUM(numerateur) / SUM(denominateur).")
  AS
  -- DEBUT kpi_jour
  WITH fin AS (
    SELECT MAX(DATE(date_creation, 'Europe/Paris')) AS d FROM sc_raw.commandes
  ),
  ls AS (SELECT * FROM sc_kpi.lignes_service),
  service AS (
    SELECT
      date_promise AS date_kpi,
      CASE WHEN GROUPING(donneur_ordre_id) = 0 THEN 'DONNEUR'
           WHEN GROUPING(transporteur_id) = 0 THEN 'TRANSPORTEUR' ELSE 'GLOBAL' END AS perimetre_type,
      COALESCE(donneur_ordre_id, transporteur_id, 'TOUS') AS perimetre_valeur,
      COUNTIF(statut_mesure = 'MESURE' AND otif) AS otif_num,
      COUNTIF(statut_mesure = 'MESURE') AS otif_den,
      SUM(IF(eligible_brut AND date_livraison <= date_promise AND qte_livree >= qte_commandee, nb_occurrences, 0)) AS brut_num,
      SUM(IF(eligible_brut, nb_occurrences, 0)) AS brut_den,
      SUM(IF(statut_mesure = 'MESURE', qte_livree, 0)) AS service_num,
      SUM(IF(statut_mesure = 'MESURE', qte_commandee, 0)) AS service_den,
      COUNTIF(statut_mesure = 'MESURE' AND rupture) AS rupture_num,
      COUNTIF(statut_mesure = 'NON_MESURABLE') AS nm_num,
      COUNTIF(statut_mesure IN ('MESURE', 'NON_MESURABLE')) AS nm_den
    FROM ls
    WHERE date_promise IS NOT NULL
    GROUP BY GROUPING SETS ((date_promise), (date_promise, donneur_ordre_id), (date_promise, transporteur_id))
  ),
  taches AS (
    SELECT
      p.tache_id, p.resultat_controle, o.equipe,
      -- jour de production : l'équipe de nuit (21h-5h) travaille pour le lendemain
      IF(EXTRACT(HOUR FROM DATETIME(p.debut, 'Europe/Paris')) >= 21,
         DATE_ADD(DATE(p.debut, 'Europe/Paris'), INTERVAL 1 DAY), DATE(p.debut, 'Europe/Paris')) AS jour_prod,
      1 / COALESCE(sz.cadence_std_par_heure, sp.cadence_std_par_heure) AS heures_standard
    FROM sc_raw.preparations AS p
    LEFT JOIN sc_raw.dim_operateur AS o ON o.operateur_id = p.operateur_id
    LEFT JOIN sc_raw.dim_emplacement AS e ON e.emplacement_id = p.emplacement_id
    LEFT JOIN sc_raw.dim_zone AS z ON z.zone_id = e.zone_id
    LEFT JOIN sc_raw.standards_activite AS sz ON sz.activite = 'PICKING' AND sz.type_zone = z.type_zone
    LEFT JOIN sc_raw.standards_activite AS sp ON sp.activite = 'PICKING' AND sp.type_zone = 'PICKING'
  ),
  prepa AS (
    SELECT
      jour_prod AS date_kpi,
      IF(GROUPING(equipe) = 0, 'EQUIPE', 'GLOBAL') AS perimetre_type,
      COALESCE(equipe, 'TOUS') AS perimetre_valeur,
      COUNTIF(resultat_controle IN ('ECART_QUANTITE', 'ERREUR_ARTICLE', 'ERREUR_LOT')) AS erreur_num,
      COUNTIF(resultat_controle != 'NON_CONTROLE') AS erreur_den,
      COUNT(*) AS nb_taches,
      SUM(heures_standard) AS heures_standard
    FROM taches
    GROUP BY GROUPING SETS ((jour_prod), (jour_prod, equipe))
  ),
  heures AS (
    SELECT
      date_travail AS date_kpi,
      IF(GROUPING(equipe) = 0, 'EQUIPE', 'GLOBAL') AS perimetre_type,
      COALESCE(equipe, 'TOUS') AS perimetre_valeur,
      SUM(heures) AS heures_picking
    FROM sc_raw.temps_travail
    WHERE activite = 'PICKING'
    GROUP BY GROUPING SETS ((date_travail), (date_travail, equipe))
  ),
  enlevements AS (
    SELECT DATE(debut_prevu, 'Europe/Paris') AS jour, transporteur_id,
           arrivee_reelle IS NOT NULL AS realise,
           COALESCE(arrivee_reelle <= TIMESTAMP_ADD(debut_prevu, INTERVAL 15 MINUTE), FALSE) AS a_l_heure
    FROM sc_raw.creneaux
    WHERE type_flux = 'ENLEVEMENT'
  ),
  creneaux AS (
    SELECT
      jour AS date_kpi,
      IF(GROUPING(transporteur_id) = 0, 'TRANSPORTEUR', 'GLOBAL') AS perimetre_type,
      COALESCE(transporteur_id, 'TOUS') AS perimetre_valeur,
      COUNTIF(realise AND a_l_heure) AS creneau_num,
      COUNTIF(realise) AS creneau_den
    FROM enlevements
    GROUP BY GROUPING SETS ((jour), (jour, transporteur_id))
  ),
  reclam AS (
    SELECT DATE(date_reclamation, 'Europe/Paris') AS date_kpi, COUNT(*) AS nb
    FROM (SELECT DISTINCT reclamation_id, date_reclamation, cause_imputee, statut FROM sc_raw.reclamations)
    WHERE cause_imputee = 'ENTREPOT' AND statut != 'NON_JUSTIFIEE'
    GROUP BY 1
  ),
  livrees AS (
    SELECT date_livraison AS date_kpi, COUNT(*) AS nb
    FROM ls
    WHERE statut_mesure = 'MESURE' AND date_livraison IS NOT NULL
    GROUP BY 1
  )
  , tout AS (
  SELECT date_kpi, 'OTIF' AS kpi_code, perimetre_type, perimetre_valeur,
         CAST(otif_num AS FLOAT64) AS numerateur, CAST(otif_den AS FLOAT64) AS denominateur FROM service
  UNION ALL SELECT date_kpi, 'OTIF_BRUT', perimetre_type, perimetre_valeur, brut_num, brut_den FROM service
  UNION ALL SELECT date_kpi, 'TAUX_SERVICE', perimetre_type, perimetre_valeur, service_num, service_den FROM service
  UNION ALL SELECT date_kpi, 'TAUX_RUPTURE', perimetre_type, perimetre_valeur, rupture_num, otif_den FROM service
  UNION ALL SELECT date_kpi, 'LIGNES_NON_MESURABLES', perimetre_type, perimetre_valeur, nm_num, nm_den FROM service
  UNION ALL SELECT date_kpi, 'ERREUR_PREPARATION', perimetre_type, perimetre_valeur, erreur_num, erreur_den FROM prepa
  UNION ALL
  SELECT p.date_kpi, 'PRODUCTIVITE_PICKING', p.perimetre_type, p.perimetre_valeur, p.nb_taches, h.heures_picking
  FROM prepa AS p
  JOIN heures AS h USING (date_kpi, perimetre_type, perimetre_valeur)
  UNION ALL
  SELECT p.date_kpi, 'PERFORMANCE_PICKING', p.perimetre_type, p.perimetre_valeur, p.heures_standard, h.heures_picking
  FROM prepa AS p
  JOIN heures AS h USING (date_kpi, perimetre_type, perimetre_valeur)
  UNION ALL SELECT date_kpi, 'RESPECT_CRENEAUX', perimetre_type, perimetre_valeur, creneau_num, creneau_den FROM creneaux
  UNION ALL
  SELECT l.date_kpi, 'RECLAMATIONS_ENTREPOT', 'GLOBAL', 'TOUS', COALESCE(r.nb, 0), l.nb
  FROM livrees AS l
  LEFT JOIN reclam AS r USING (date_kpi)
  )
  -- on ne garde que les jours couverts par les données, avec un dénominateur non nul
  SELECT tout.* FROM tout CROSS JOIN fin
  WHERE tout.date_kpi <= fin.d AND tout.denominateur > 0
  -- FIN kpi_jour
  ;

  -- -------------------------------------------------------------------
  -- 3. Définitions versionnées (l'agent cite toujours la version utilisée)
  -- -------------------------------------------------------------------
  MERGE sc_kpi.definitions_kpi AS t
  USING (
    SELECT * FROM UNNEST([
      STRUCT('OTIF' AS kpi_code, 1 AS version, 'OTIF ligne (fiabilisé)' AS libelle,
        'Part des lignes de commande livrées au plus tard à la date promise ET en totalité. Calculé sur les lignes dédoublonnées et mesurables ; les ruptures comptent comme des échecs ; doublons métier, annulations client et lignes pas encore dues sont exclus ; lignes sans date de livraison ou aux dates incohérentes comptées à part (LIGNES_NON_MESURABLES).' AS description,
        'lignes à l’heure et complètes / lignes mesurables' AS formule, 'LIGNE' AS niveau, 'date_promise' AS date_reference,
        0 AS tolerance_jours, '%' AS unite, 'HAUSSE_FAVORABLE' AS sens),
      ('OTIF_BRUT', 1, 'OTIF ligne (brut, non fiabilisé)',
        'Calcul naïf d’un tableau de bord branché directement sur les données : lignes au statut LIVREE ayant une date de livraison, doublons compris, ruptures ignorées. Sert uniquement à mesurer l’effet de la non-qualité des données.',
        'lignes LIVREE à l’heure et complètes / lignes LIVREE datées (doublons compris)', 'LIGNE', 'date_promise', 0, '%', 'HAUSSE_FAVORABLE'),
      ('TAUX_SERVICE', 1, 'Taux de service quantité',
        'Part des quantités commandées effectivement livrées, sur les lignes mesurables.',
        'quantités livrées / quantités commandées', 'LIGNE', 'date_promise', 0, '%', 'HAUSSE_FAVORABLE'),
      ('TAUX_RUPTURE', 1, 'Taux de rupture',
        'Part des lignes mesurables non servies ou partiellement servies pour rupture de stock ou lot bloqué.',
        'lignes en rupture / lignes mesurables', 'LIGNE', 'date_promise', 0, '%', 'BAISSE_FAVORABLE'),
      ('LIGNES_NON_MESURABLES', 1, 'Lignes non mesurables',
        'Part des lignes dont la donnée ne permet pas de dire si l’OTIF est atteint (livrée sans date, dates incohérentes). Indicateur de fiabilité des données.',
        'lignes non mesurables / (lignes mesurables + non mesurables)', 'LIGNE', 'date_promise', 0, '%', 'BAISSE_FAVORABLE'),
      ('ERREUR_PREPARATION', 1, 'Taux d’erreur de préparation (interne)',
        'Part des tâches de prélèvement contrôlées pour lesquelles le contrôle a détecté un écart de quantité, une erreur d’article ou une erreur de lot. Erreurs détectées avant expédition.',
        'tâches en erreur / tâches contrôlées', 'TACHE', 'jour de production', 0, '%', 'BAISSE_FAVORABLE'),
      ('PRODUCTIVITE_PICKING', 1, 'Productivité de préparation',
        'Nombre de lignes prélevées par heure de picking pointée.',
        'tâches de prélèvement / heures de picking', 'JOUR', 'jour de production', 0, 'lignes/h', 'HAUSSE_FAVORABLE'),
      ('PERFORMANCE_PICKING', 1, 'Taux de performance (OMS)',
        'Heures standard produites (somme sur les tâches de 1 / cadence standard de la zone) rapportées aux heures de picking réelles. 100 % = cadence standard tenue.',
        'heures standard / heures réelles de picking', 'JOUR', 'jour de production', 0, '%', 'HAUSSE_FAVORABLE'),
      ('RESPECT_CRENEAUX', 1, 'Respect des créneaux d’enlèvement',
        'Part des enlèvements transporteurs dont l’arrivée au quai a lieu au plus tard 15 minutes après le début du créneau.',
        'enlèvements à l’heure (+15 min) / enlèvements réalisés', 'CRENEAU', 'début du créneau', 0, '%', 'HAUSSE_FAVORABLE'),
      ('RECLAMATIONS_ENTREPOT', 1, 'Réclamations imputées à l’entrepôt',
        'Réclamations clients justifiées ou en cours dont la cause est imputée à l’entrepôt, pour 1 000 lignes livrées.',
        'réclamations entrepôt / lignes livrées x 1000', 'JOUR', 'date de réclamation', 0, '‰', 'BAISSE_FAVORABLE')
    ])
  ) AS s
  ON t.kpi_code = s.kpi_code AND t.version = s.version
  WHEN MATCHED THEN UPDATE SET libelle = s.libelle, description = s.description, formule = s.formule,
    niveau = s.niveau, date_reference = s.date_reference, tolerance_jours = s.tolerance_jours,
    unite = s.unite, sens = s.sens
  WHEN NOT MATCHED THEN INSERT
    (kpi_code, version, libelle, description, formule, requete_sql, niveau, date_reference, tolerance_jours, unite, sens, valide_depuis, valide_jusqu)
  VALUES
    (s.kpi_code, s.version, s.libelle, s.description, s.formule, 'CALL sc_kpi.calculer_kpi()', s.niveau, s.date_reference,
     s.tolerance_jours, s.unite, s.sens, DATE '2025-10-01', NULL);

  SELECT kpi_code, perimetre_type, COUNT(*) AS nb_lignes,
         ROUND(SAFE_DIVIDE(SUM(numerateur), SUM(denominateur)), 4) AS valeur_globale
  FROM sc_kpi.kpi_jour
  WHERE perimetre_type = 'GLOBAL'
  GROUP BY kpi_code, perimetre_type
  ORDER BY kpi_code;
END;
