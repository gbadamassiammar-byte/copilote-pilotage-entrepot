-- =====================================================================
-- 04_dq_regles.sql : catalogue des règles de fiabilisation
--
-- Principe : une règle = une requête SQL qui renvoie UNE LIGNE PAR ANOMALIE,
-- avec exactement deux colonnes :
--   * cle_ligne : l'identifiant de la donnée en anomalie (ligne, commande,
--     lot|jour...), pour remonter directement à la source ;
--   * detail    : un objet JSON avec les valeurs constatées.
-- Les règles sont des DONNEES : ajouter un contrôle = ajouter une ligne,
-- sans toucher à la procédure qui les exécute.
--
-- MERGE : relancer le script met à jour les règles existantes et ajoute
-- les nouvelles, sans écraser le champ "active" ni la date de création.
-- =====================================================================

SET @@location = 'EU';

MERGE sc_dq.dq_regles AS t
USING (
  SELECT * FROM UNNEST([

  -- -------------------------------------------------------------------
  -- UNICITE
  -- -------------------------------------------------------------------
  STRUCT(
    'DQ_UNI_001' AS regle_id,
    'Commande en double (même numéro)' AS libelle,
    'Le même numéro de commande apparaît plusieurs fois, typiquement après une double intégration de l interface. Gonfle les volumes et fausse tous les KPI calculés par commande.' AS description,
    'UNICITE' AS dimension, 'commandes' AS table_cible, 'MAJEURE' AS severite,
    """
SELECT commande_id AS cle_ligne,
       TO_JSON(STRUCT(COUNT(*) AS nb_occurrences)) AS detail
FROM sc_raw.commandes
GROUP BY commande_id
HAVING COUNT(*) > 1
    """ AS requete_sql),

  STRUCT(
    'DQ_UNI_002', 'Ligne de commande en double (même identifiant)',
    'La même ligne de commande est présente plusieurs fois. Elle serait comptée deux fois dans l OTIF et le taux de service.',
    'UNICITE', 'lignes_commande', 'MAJEURE',
    """
SELECT ligne_id AS cle_ligne,
       TO_JSON(STRUCT(COUNT(*) AS nb_occurrences)) AS detail
FROM sc_raw.lignes_commande
GROUP BY ligne_id
HAVING COUNT(*) > 1
    """),

  STRUCT(
    'DQ_UNI_003', 'Mouvement de stock en double',
    'Le même document de mouvement est enregistré plusieurs fois : le stock théorique calculé à partir des mouvements devient faux.',
    'UNICITE', 'mouvements_stock', 'MAJEURE',
    """
SELECT mouvement_id AS cle_ligne,
       TO_JSON(STRUCT(COUNT(*) AS nb_occurrences)) AS detail
FROM sc_raw.mouvements_stock
GROUP BY mouvement_id
HAVING COUNT(*) > 1
    """),

  STRUCT(
    'DQ_UNI_004', 'Commande ressaisie sous un autre numéro (doublon métier)',
    'Deux commandes du même donneur d ordre, pour le même destinataire, avec exactement les mêmes articles et quantités, créées à moins de 30 minutes d intervalle. Risque de double expédition. La commande signalée est la plus récente.',
    'UNICITE', 'commandes', 'MAJEURE',
    """
WITH c AS (
  SELECT DISTINCT commande_id, donneur_ordre_id, destinataire_id, date_creation
  FROM sc_raw.commandes
),
l AS (
  SELECT commande_id,
         STRING_AGG(CONCAT(article_id, ':', CAST(qte_commandee AS STRING)), ',' ORDER BY article_id, qte_commandee) AS signature
  FROM (SELECT DISTINCT ligne_id, commande_id, article_id, qte_commandee FROM sc_raw.lignes_commande)
  GROUP BY commande_id
),
s AS (SELECT c.*, l.signature FROM c JOIN l USING (commande_id))
SELECT b.commande_id AS cle_ligne,
       TO_JSON(STRUCT(a.commande_id AS commande_origine,
                      TIMESTAMP_DIFF(b.date_creation, a.date_creation, MINUTE) AS ecart_minutes)) AS detail
FROM s AS a
JOIN s AS b
  ON  b.donneur_ordre_id = a.donneur_ordre_id
  AND b.destinataire_id = a.destinataire_id
  AND b.signature = a.signature
  AND b.commande_id != a.commande_id
  AND b.date_creation > a.date_creation
  AND b.date_creation <= TIMESTAMP_ADD(a.date_creation, INTERVAL 30 MINUTE)
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (PARTITION BY b.commande_id ORDER BY a.date_creation) = 1
    """),

  -- -------------------------------------------------------------------
  -- COMPLETUDE
  -- -------------------------------------------------------------------
  STRUCT(
    'DQ_COM_001', 'Ligne livrée sans date de livraison',
    'La ligne est au statut LIVREE mais la preuve de livraison n a pas de date. La ligne sort du calcul de l OTIF, qui devient flatteur.',
    'COMPLETUDE', 'lignes_commande', 'MAJEURE',
    """
SELECT ligne_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(commande_id) AS commande_id)) AS detail
FROM sc_raw.lignes_commande
WHERE statut_ligne = 'LIVREE' AND date_livraison_reelle IS NULL
GROUP BY ligne_id
    """),

  STRUCT(
    'DQ_COM_002', 'Ligne expédiée sans date d expédition',
    'La ligne est expédiée ou livrée mais sa date de sortie physique est vide : impossible de mesurer le délai de préparation ni de rattacher la ligne à un créneau.',
    'COMPLETUDE', 'lignes_commande', 'MINEURE',
    """
SELECT ligne_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(commande_id) AS commande_id, ANY_VALUE(statut_ligne) AS statut_ligne)) AS detail
FROM sc_raw.lignes_commande
WHERE statut_ligne IN ('EXPEDIEE', 'LIVREE') AND date_expedition_reelle IS NULL
GROUP BY ligne_id
    """),

  STRUCT(
    'DQ_COM_003', 'Interruption des relevés de température',
    'Plus de 20 minutes sans relevé pour une sonde qui enregistre toutes les 15 minutes. Une période non enregistrée ne peut pas être justifiée lors d un audit BPD.',
    'COMPLETUDE', 'releves_temperature', 'MAJEURE',
    """
SELECT CONCAT(capteur_id, '|', FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S%Ez', TIMESTAMP_ADD(horodatage, INTERVAL 15 MINUTE), 'Europe/Paris')) AS cle_ligne,
       TO_JSON(STRUCT(capteur_id, zone_id,
                      TIMESTAMP_DIFF(suivant, horodatage, MINUTE) - 15 AS duree_manquante_min)) AS detail
FROM (
  SELECT capteur_id, zone_id, horodatage,
         LEAD(horodatage) OVER (PARTITION BY capteur_id ORDER BY horodatage) AS suivant
  FROM sc_raw.releves_temperature
)
WHERE TIMESTAMP_DIFF(suivant, horodatage, MINUTE) > 20
    """),

  -- -------------------------------------------------------------------
  -- COHERENCE
  -- -------------------------------------------------------------------
  STRUCT(
    'DQ_COH_001', 'Commande annulée dont des lignes sont expédiées ou livrées',
    'L en-tête dit ANNULEE alors que des lignes sont parties chez le destinataire. Soit la commande est exclue à tort des KPI, soit de la marchandise est sortie sans commande valide.',
    'COHERENCE', 'commandes', 'MAJEURE',
    """
SELECT c.commande_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(c.statut_commande) AS statut_entete,
                      COUNT(DISTINCT l.ligne_id) AS nb_lignes_expediees)) AS detail
FROM sc_raw.commandes AS c
JOIN sc_raw.lignes_commande AS l ON l.commande_id = c.commande_id
WHERE c.statut_commande = 'ANNULEE' AND l.statut_ligne IN ('EXPEDIEE', 'LIVREE')
GROUP BY c.commande_id
    """),

  STRUCT(
    'DQ_COH_002', 'Statut de ligne non mis à jour après livraison',
    'La ligne est encore au statut EXPEDIEE alors qu une date de livraison existe. La ligne disparaît des KPI de service.',
    'COHERENCE', 'lignes_commande', 'MINEURE',
    """
SELECT ligne_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(commande_id) AS commande_id,
                      ANY_VALUE(date_livraison_reelle) AS date_livraison_reelle)) AS detail
FROM sc_raw.lignes_commande
WHERE statut_ligne = 'EXPEDIEE' AND date_livraison_reelle IS NOT NULL
GROUP BY ligne_id
    """),

  STRUCT(
    'DQ_COH_003', 'Livraison datée avant l expédition',
    'La date de livraison est antérieure à la date d expédition : au moins une des deux dates est fausse, et le délai de livraison calculé est négatif.',
    'COHERENCE', 'lignes_commande', 'MAJEURE',
    """
SELECT ligne_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(date_expedition_reelle) AS date_expedition_reelle,
                      ANY_VALUE(date_livraison_reelle) AS date_livraison_reelle)) AS detail
FROM sc_raw.lignes_commande
WHERE date_livraison_reelle < date_expedition_reelle
GROUP BY ligne_id
    """),

  STRUCT(
    'DQ_COH_004', 'Séquence de statuts impossible',
    'Dans l historique, un statut apparaît après un statut plus avancé (ex : LIVREE horodaté avant EXPEDIEE). Les temps de cycle calculés sur cette commande sont faux.',
    'COHERENCE', 'historique_statuts', 'MINEURE',
    """
WITH e AS (
  SELECT entite_id, statut, horodatage,
         CASE statut WHEN 'CREEE' THEN 1 WHEN 'CONFIRMEE' THEN 2 WHEN 'EN_PREPARATION' THEN 3
                     WHEN 'PREPAREE' THEN 4 WHEN 'EXPEDIEE' THEN 5 WHEN 'LIVREE' THEN 6 END AS rang
  FROM sc_raw.historique_statuts
  WHERE type_entite = 'COMMANDE' AND statut != 'ANNULEE'
),
o AS (
  SELECT entite_id, statut, rang,
         LAG(rang)   OVER (PARTITION BY entite_id ORDER BY horodatage, rang) AS rang_precedent,
         LAG(statut) OVER (PARTITION BY entite_id ORDER BY horodatage, rang) AS statut_precedent
  FROM e
)
SELECT entite_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(statut_precedent) AS statut_horodate_avant,
                      ANY_VALUE(statut) AS statut_horodate_apres)) AS detail
FROM o
WHERE rang < rang_precedent
GROUP BY entite_id
    """),

  STRUCT(
    'DQ_COH_005', 'Ecart de stock non expliqué par les mouvements',
    'Pour un lot et un jour : stock de la veille + mouvements du jour différent du stock du jour. Signe d un mouvement non enregistré (perte, vol, erreur de saisie). Les mouvements en double sont neutralisés avant le calcul, et le contrôle s arrête à la dernière photo de stock disponible.',
    'COHERENCE', 'mouvements_stock', 'CRITIQUE',
    """
WITH photo AS (
  SELECT lot_id, date_photo, SUM(qte_physique) AS q
  FROM sc_raw.stock_photo
  GROUP BY lot_id, date_photo
),
bornes AS (SELECT MIN(date_photo) AS d0, MAX(date_photo) AS d1 FROM photo),
mvt AS (
  SELECT lot_id, DATE(horodatage, 'Europe/Paris') AS d, SUM(quantite) AS q
  FROM (
    SELECT mouvement_id, lot_id, horodatage, quantite
    FROM sc_raw.mouvements_stock
    WHERE TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY mouvement_id ORDER BY _charge_le) = 1
  )
  GROUP BY lot_id, d
),
lots AS (SELECT lot_id FROM photo UNION DISTINCT SELECT lot_id FROM mvt),
grille AS (
  SELECT lots.lot_id, d
  FROM lots
  CROSS JOIN bornes
  CROSS JOIN UNNEST(GENERATE_DATE_ARRAY(DATE_ADD(bornes.d0, INTERVAL 1 DAY), bornes.d1)) AS d
)
SELECT CONCAT(g.lot_id, '|', CAST(g.d AS STRING)) AS cle_ligne,
       TO_JSON(STRUCT(COALESCE(pv.q, 0) AS stock_veille,
                      COALESCE(m.q, 0)  AS mouvements_jour,
                      COALESCE(p.q, 0)  AS stock_jour,
                      COALESCE(p.q, 0) - COALESCE(pv.q, 0) - COALESCE(m.q, 0) AS ecart)) AS detail
FROM grille AS g
LEFT JOIN photo AS p  ON p.lot_id = g.lot_id  AND p.date_photo = g.d
LEFT JOIN photo AS pv ON pv.lot_id = g.lot_id AND pv.date_photo = DATE_SUB(g.d, INTERVAL 1 DAY)
LEFT JOIN mvt   AS m  ON m.lot_id = g.lot_id  AND m.d = g.d
WHERE COALESCE(p.q, 0) - COALESCE(pv.q, 0) != COALESCE(m.q, 0)
    """),

  -- -------------------------------------------------------------------
  -- EXACTITUDE
  -- -------------------------------------------------------------------
  STRUCT(
    'DQ_EXA_001', 'Quantité expédiée supérieure à la quantité préparée',
    'On ne peut pas expédier plus que ce qui a été prélevé : l une des deux quantités est fausse, et le taux de service est surestimé.',
    'EXACTITUDE', 'lignes_commande', 'MAJEURE',
    """
SELECT ligne_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(qte_preparee) AS qte_preparee,
                      ANY_VALUE(qte_expediee) AS qte_expediee)) AS detail
FROM sc_raw.lignes_commande
WHERE qte_expediee > qte_preparee
GROUP BY ligne_id
    """),

  STRUCT(
    'DQ_EXA_002', 'Ligne de commande sur un article inconnu',
    'L article de la ligne n existe pas dans le référentiel articles (référence orpheline) : la ligne ne peut être rattachée ni à un donneur d ordre, ni à une zone de stockage.',
    'EXACTITUDE', 'lignes_commande', 'MAJEURE',
    """
SELECT l.ligne_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(l.article_id) AS article_id)) AS detail
FROM sc_raw.lignes_commande AS l
LEFT JOIN sc_raw.dim_article AS a ON a.article_id = l.article_id
WHERE a.article_id IS NULL
GROUP BY l.ligne_id
    """),

  -- -------------------------------------------------------------------
  -- CONFORMITE (bonnes pratiques de distribution, BPD / GDP)
  -- -------------------------------------------------------------------
  STRUCT(
    'DQ_CNF_001', 'Prélèvement ne respectant pas le FEFO',
    'Un lot a été prélevé alors qu un autre lot du même article, libre, disponible toute la journée et avec une durée de vie résiduelle suffisante, périmait plus tôt. Contrôle approché à partir des photos de stock de la veille et du jour.',
    'CONFORMITE', 'preparations', 'MAJEURE',
    """
WITH dispo AS (
  SELECT s.date_photo, s.article_id, s.lot_id
  FROM sc_raw.stock_photo AS s
  JOIN sc_raw.dim_emplacement AS e ON e.emplacement_id = s.emplacement_id
  WHERE s.statut_qualite = 'LIBRE' AND e.zone_id != 'Z06'
  GROUP BY s.date_photo, s.article_id, s.lot_id
  HAVING SUM(s.qte_physique - s.qte_reservee) > 0
),
dispo_journee AS (
  SELECT a.date_photo, a.article_id, a.lot_id, l.date_peremption
  FROM dispo AS a
  JOIN dispo AS b ON b.lot_id = a.lot_id AND b.date_photo = DATE_SUB(a.date_photo, INTERVAL 1 DAY)
  JOIN sc_raw.dim_lot AS l ON l.lot_id = a.lot_id
),
t AS (
  SELECT p.tache_id, p.lot_id, l.article_id, l.date_peremption, DATE(p.debut, 'Europe/Paris') AS j
  FROM sc_raw.preparations AS p
  JOIN sc_raw.dim_lot AS l ON l.lot_id = p.lot_id
)
SELECT t.tache_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(t.lot_id) AS lot_preleve,
                      ANY_VALUE(t.date_peremption) AS peremption_lot_preleve,
                      ARRAY_AGG(d.lot_id ORDER BY d.date_peremption LIMIT 1)[OFFSET(0)] AS lot_attendu,
                      MIN(d.date_peremption) AS peremption_lot_attendu)) AS detail
FROM t
JOIN dispo_journee AS d
  ON  d.date_photo = t.j
  AND d.article_id = t.article_id
  AND d.lot_id != t.lot_id
  AND d.date_peremption < t.date_peremption
  AND d.date_peremption > DATE_ADD(t.j, INTERVAL 60 DAY)
GROUP BY t.tache_id
    """),

  STRUCT(
    'DQ_CNF_002', 'Prélèvement d un lot rappelé',
    'Un lot visé par un rappel a été prélevé à partir du jour du rappel. Chaque boîte concernée doit être retrouvée chez le destinataire : écart critique au regard des BPD.',
    'CONFORMITE', 'preparations', 'CRITIQUE',
    """
SELECT p.tache_id AS cle_ligne,
       TO_JSON(STRUCT(p.lot_id, l.date_rappel, p.debut AS preleve_le, p.commande_id, p.qte_prelevee)) AS detail
FROM sc_raw.preparations AS p
JOIN sc_raw.dim_lot AS l ON l.lot_id = p.lot_id
WHERE l.rappele AND DATE(p.debut, 'Europe/Paris') >= l.date_rappel
    """),

  STRUCT(
    'DQ_CNF_003', 'Retour remis en stock sans décision de la Personne Responsable',
    'Un mouvement a remis le retour en stock vendable alors que la décision de la Personne Responsable est absente ou en attente. Les BPD exigent une décision enregistrée avant toute remise en stock.',
    'CONFORMITE', 'retours', 'CRITIQUE',
    """
WITH remises AS (
  SELECT ref_document, MIN(horodatage) AS remis_le
  FROM sc_raw.mouvements_stock
  WHERE type_mouvement = 'CHANGEMENT_STATUT' AND code_mvt_sap = '321' AND quantite > 0
  GROUP BY ref_document
)
SELECT r.retour_id AS cle_ligne,
       TO_JSON(STRUCT(r.lot_id, r.decision_rp, m.remis_le)) AS detail
FROM sc_raw.retours AS r
JOIN remises AS m ON m.ref_document = r.retour_id
WHERE r.decision_rp IS NULL OR r.decision_rp = 'EN_ATTENTE'
    """),

  STRUCT(
    'DQ_CNF_004', 'Retour hors délai remis en stock',
    'Retour d un destinataire sans autorisation de distribution en gros, reçu plus de 5 jours après l expédition d origine, et pourtant remis en stock vendable. Les BPD n autorisent la remise en stock que dans ce délai.',
    'CONFORMITE', 'retours', 'CRITIQUE',
    """
WITH remises AS (
  SELECT ref_document, MIN(horodatage) AS remis_le
  FROM sc_raw.mouvements_stock
  WHERE type_mouvement = 'CHANGEMENT_STATUT' AND code_mvt_sap = '321' AND quantite > 0
  GROUP BY ref_document
)
SELECT r.retour_id AS cle_ligne,
       TO_JSON(STRUCT(r.lot_id, d.type_destinataire,
                      DATE_DIFF(DATE(r.date_retour, 'Europe/Paris'), DATE(r.date_expedition_origine, 'Europe/Paris'), DAY) AS jours_depuis_expedition,
                      m.remis_le)) AS detail
FROM sc_raw.retours AS r
JOIN sc_raw.dim_destinataire AS d ON d.destinataire_id = r.destinataire_id
JOIN remises AS m ON m.ref_document = r.retour_id
WHERE NOT d.autorisation_gros
  AND DATE_DIFF(DATE(r.date_retour, 'Europe/Paris'), DATE(r.date_expedition_origine, 'Europe/Paris'), DAY) > 5
    """),

  STRUCT(
    'DQ_CNF_005', 'Lot stocké dans une zone de température incompatible',
    'La plage de température qualifiée de la zone ne tient pas dans la plage de conservation de l article (ex : article 2-8 °C en réserve ambiante 15-25 °C).',
    'CONFORMITE', 'stock_photo', 'CRITIQUE',
    """
SELECT s.lot_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(s.article_id) AS article_id,
                      ANY_VALUE(z.zone_id) AS zone_id,
                      ANY_VALUE(CONCAT(CAST(a.temp_min_c AS STRING), '-', CAST(a.temp_max_c AS STRING))) AS plage_article,
                      ANY_VALUE(CONCAT(CAST(z.temp_min_c AS STRING), '-', CAST(z.temp_max_c AS STRING))) AS plage_zone,
                      MIN(s.date_photo) AS premiere_photo)) AS detail
FROM sc_raw.stock_photo AS s
JOIN sc_raw.dim_emplacement AS e ON e.emplacement_id = s.emplacement_id
JOIN sc_raw.dim_zone AS z ON z.zone_id = e.zone_id
JOIN sc_raw.dim_article AS a ON a.article_id = s.article_id
WHERE z.temp_min_c < a.temp_min_c OR z.temp_max_c > a.temp_max_c
GROUP BY s.lot_id
    """),

  STRUCT(
    'DQ_CNF_006', 'Excursion de température',
    'Une sonde relève une température hors de la plage qualifiée de sa zone. Chaque épisode doit faire l objet d une investigation et d une décision qualité sur les lots présents.',
    'CONFORMITE', 'releves_temperature', 'CRITIQUE',
    """
WITH r AS (
  SELECT t.capteur_id, t.zone_id, t.horodatage, t.temperature_c,
         (t.temperature_c < z.temp_min_c OR t.temperature_c > z.temp_max_c) AS hors_plage
  FROM sc_raw.releves_temperature AS t
  JOIN sc_raw.dim_zone AS z ON z.zone_id = t.zone_id
),
debuts AS (
  SELECT *,
         IF(hors_plage AND NOT COALESCE(LAG(hors_plage) OVER (PARTITION BY capteur_id ORDER BY horodatage), FALSE), 1, 0) AS debut_episode
  FROM r
),
episodes AS (
  SELECT *, SUM(debut_episode) OVER (PARTITION BY capteur_id ORDER BY horodatage) AS num_episode
  FROM debuts
)
SELECT CONCAT(capteur_id, '|', FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S%Ez', MIN(horodatage), 'Europe/Paris')) AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(zone_id) AS zone_id,
                      MIN(horodatage) AS debut,
                      TIMESTAMP_DIFF(MAX(horodatage), MIN(horodatage), MINUTE) + 15 AS duree_min,
                      MAX(temperature_c) AS temperature_max,
                      MIN(temperature_c) AS temperature_min)) AS detail
FROM episodes
WHERE hors_plage
GROUP BY capteur_id, num_episode
    """),

  STRUCT(
    'DQ_CNF_007', 'Lot périmé encore en stock libre',
    'Un lot dont la date de péremption est dépassée figure en stock libre, donc allouable à une commande. Il aurait dû être bloqué.',
    'CONFORMITE', 'stock_photo', 'CRITIQUE',
    """
SELECT s.lot_id AS cle_ligne,
       TO_JSON(STRUCT(ANY_VALUE(l.date_peremption) AS date_peremption,
                      MIN(s.date_photo) AS premiere_photo_concernee,
                      MAX(s.qte_physique) AS qte_max)) AS detail
FROM sc_raw.stock_photo AS s
JOIN sc_raw.dim_lot AS l ON l.lot_id = s.lot_id
WHERE s.statut_qualite = 'LIBRE' AND s.qte_physique > 0 AND l.date_peremption <= s.date_photo
GROUP BY s.lot_id
    """)

  ])
) AS s
ON t.regle_id = s.regle_id
WHEN MATCHED THEN UPDATE SET
  libelle = s.libelle, description = s.description, dimension = s.dimension,
  table_cible = s.table_cible, severite = s.severite, requete_sql = s.requete_sql,
  maj_le = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
  (regle_id, libelle, description, dimension, table_cible, severite, requete_sql, active, cree_le, maj_le)
VALUES
  (s.regle_id, s.libelle, s.description, s.dimension, s.table_cible, s.severite, s.requete_sql, TRUE,
   CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP());
