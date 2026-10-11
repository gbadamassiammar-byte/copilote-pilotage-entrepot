-- =====================================================================
-- 02_raw_flux.sql : tables de faits (flux) de la couche brute
--
-- Partitionnement : MENSUEL, pas journalier.
-- Avec environ 12 mois et quelques centaines de Mo au total, des partitions
-- journalières seraient minuscules, et BigQuery facture de toute façon
-- 10 Mo minimum par table lue. Le partitionnement mensuel garde l'habitude
-- de bonne pratique (filtrer par période) sans fragmenter la table.
-- Le clustering trie les données par les colonnes les plus filtrées
-- (article, lot, opérateur...), ce qui accélère les requêtes de l'agent.
--
-- date_commande et site_id sont recopiés sur les lignes : redondance
-- volontaire pour pouvoir partitionner et filtrer sans jointure.
-- =====================================================================

SET @@location = 'EU';  -- force l'exécution en EU (la console est en US par défaut)

-- ---------------------------------------------------------------------
-- Flux sortant
-- ---------------------------------------------------------------------

CREATE OR REPLACE TABLE sc_raw.commandes (
  commande_id              STRING NOT NULL OPTIONS(description="Numéro de commande. SAP indicatif : VBELN (VBAK)"),
  donneur_ordre_id         STRING,
  destinataire_id          STRING,
  site_id                  STRING,
  transporteur_id          STRING,
  creneau_id               STRING OPTIONS(description="Créneau d enlèvement auquel la commande est rattachée"),
  canal                    STRING OPTIONS(description="EDI, PORTAIL ou EMAIL"),
  priorite                 STRING OPTIONS(description="STANDARD ou URGENT"),
  date_creation            TIMESTAMP OPTIONS(description="Création de la commande. SAP indicatif : ERDAT + ERZET"),
  date_livraison_demandee  DATE      OPTIONS(description="Date souhaitée par le destinataire. SAP indicatif : VDATU"),
  statut_commande          STRING    OPTIONS(description="CREEE, CONFIRMEE, EN_PREPARATION, PREPAREE, EXPEDIEE, LIVREE, ANNULEE"),
  source_systeme           STRING,
  _charge_le               TIMESTAMP
)
PARTITION BY TIMESTAMP_TRUNC(date_creation, MONTH)
CLUSTER BY donneur_ordre_id, destinataire_id
OPTIONS(description="En-têtes de commandes clients");

CREATE OR REPLACE TABLE sc_raw.lignes_commande (
  ligne_id                STRING NOT NULL,
  commande_id             STRING,
  num_ligne               INT64  OPTIONS(description="Numéro de poste. SAP indicatif : POSNR"),
  article_id              STRING,
  site_id                 STRING OPTIONS(description="Recopié de l en-tête"),
  date_commande           DATE   OPTIONS(description="Recopiée de l en-tête, sert au partitionnement"),
  qte_commandee           INT64,
  qte_preparee            INT64,
  qte_expediee            INT64,
  qte_livree              INT64,
  date_promise            DATE      OPTIONS(description="Date de livraison confirmée par l entrepôt : référence principale de l OTIF"),
  date_expedition_reelle  TIMESTAMP OPTIONS(description="Sortie physique (sortie de marchandise)"),
  date_livraison_reelle   TIMESTAMP OPTIONS(description="Preuve de livraison transporteur"),
  statut_ligne            STRING    OPTIONS(description="Mêmes valeurs que statut_commande"),
  motif_ecart             STRING    OPTIONS(description="RUPTURE, LOT_BLOQUE, ERREUR_PREPA, RETARD_TRANSPORT, CASSE..."),
  _charge_le              TIMESTAMP
)
PARTITION BY DATE_TRUNC(date_commande, MONTH)
CLUSTER BY article_id, commande_id
OPTIONS(description="Lignes de commande : grain de calcul de l OTIF et du taux de service");

CREATE OR REPLACE TABLE sc_raw.preparations (
  tache_id          STRING NOT NULL OPTIONS(description="Tâche de prélèvement. SAP indicatif : ordre de transfert (WM) ou tâche magasin (EWM)"),
  ligne_id          STRING,
  commande_id       STRING OPTIONS(description="Recopié pour faciliter les analyses"),
  vague_id          STRING OPTIONS(description="Vague de préparation"),
  operateur_id      STRING,
  emplacement_id    STRING,
  lot_id            STRING OPTIONS(description="Lot effectivement prélevé : permet le contrôle FEFO et la traçabilité lot vers destinataire"),
  qte_a_prelever    INT64,
  qte_prelevee      INT64,
  debut             TIMESTAMP,
  fin               TIMESTAMP,
  resultat_controle STRING OPTIONS(description="OK, ECART_QUANTITE, ERREUR_ARTICLE, ERREUR_LOT, NON_CONTROLE"),
  controleur_id     STRING,
  _charge_le        TIMESTAMP
)
PARTITION BY TIMESTAMP_TRUNC(debut, MONTH)
CLUSTER BY operateur_id, lot_id
OPTIONS(description="Tâches de prélèvement : base du taux d erreur interne, de la productivité et du contrôle FEFO");

CREATE OR REPLACE TABLE sc_raw.historique_statuts (
  evenement_id    STRING NOT NULL,
  type_entite     STRING OPTIONS(description="COMMANDE ou LIGNE"),
  entite_id       STRING,
  statut          STRING,
  horodatage      TIMESTAMP,
  operateur_id    STRING,
  source_systeme  STRING,
  _charge_le      TIMESTAMP
)
PARTITION BY TIMESTAMP_TRUNC(horodatage, MONTH)
CLUSTER BY type_entite, entite_id
OPTIONS(description="Journal des changements de statut : détecte les séquences impossibles et mesure les temps de cycle");

-- ---------------------------------------------------------------------
-- Stock
-- ---------------------------------------------------------------------

CREATE OR REPLACE TABLE sc_raw.mouvements_stock (
  mouvement_id             STRING NOT NULL OPTIONS(description="SAP indicatif : MBLNR + ZEILE (document article)"),
  site_id                  STRING,
  emplacement_id           STRING,
  article_id               STRING,
  lot_id                   STRING,
  type_mouvement           STRING OPTIONS(description="REPRISE_STOCK, RECEPTION, SORTIE_LIVRAISON, RETOUR_CLIENT, AJUSTEMENT_INVENTAIRE, CHANGEMENT_STATUT, DESTRUCTION, RETOUR_LABO"),
  code_mvt_sap             STRING OPTIONS(description="Code indicatif SAP BWART : 561 reprise de stock, 101 réception, 601 sortie livraison, 655 retour client en contrôle qualité, 321 CQ vers libre, 350 CQ vers bloqué, 344 libre vers bloqué, 343 bloqué vers libre, 701/702 écarts d inventaire, 553/555 destruction depuis CQ/bloqué, 122 retour fournisseur"),
  statut_qualite_origine   STRING OPTIONS(description="Statut du stock avant mouvement"),
  statut_qualite_cible     STRING OPTIONS(description="Statut du stock après mouvement"),
  quantite                 INT64  OPTIONS(description="Quantité signée appliquée au bac (emplacement, lot, statut). Statut du bac = statut_qualite_cible si quantite > 0, statut_qualite_origine sinon. Un changement de statut s écrit en deux lignes (-q puis +q)"),
  horodatage               TIMESTAMP,
  ref_document             STRING OPTIONS(description="Référence de la pièce d origine : réception, ligne de commande, retour, inventaire"),
  operateur_id             STRING,
  _charge_le               TIMESTAMP
)
PARTITION BY TIMESTAMP_TRUNC(horodatage, MONTH)
CLUSTER BY article_id, lot_id
OPTIONS(description="Mouvements de stock : avec stock_photo, permet de détecter les écarts non tracés. Simplification : la sortie 601 est comptabilisée à la fin du prélèvement");

CREATE OR REPLACE TABLE sc_raw.stock_photo (
  date_photo       DATE NOT NULL,
  site_id          STRING,
  emplacement_id   STRING,
  article_id       STRING,
  lot_id           STRING,
  statut_qualite   STRING OPTIONS(description="LIBRE, CONTROLE_QUALITE ou BLOQUE"),
  qte_physique     INT64,
  qte_reservee     INT64 OPTIONS(description="Quantité réservée pour des commandes en cours"),
  _charge_le       TIMESTAMP
)
PARTITION BY DATE_TRUNC(date_photo, MONTH)
CLUSTER BY article_id, lot_id
OPTIONS(description="Photo quotidienne du stock par emplacement et lot");

CREATE OR REPLACE TABLE sc_raw.receptions (
  reception_id            STRING NOT NULL OPTIONS(description="SAP indicatif : livraison entrante"),
  site_id                 STRING,
  donneur_ordre_id        STRING,
  article_id              STRING,
  lot_id                  STRING,
  qte_attendue            INT64,
  qte_recue               INT64,
  date_prevue             DATE,
  date_reelle             TIMESTAMP,
  statut_qualite_initial  STRING OPTIONS(description="Statut à l entrée, en général CONTROLE_QUALITE avant libération"),
  statut_reception        STRING OPTIONS(description="PREVUE, RECUE, CLOTUREE, ANNULEE"),
  _charge_le              TIMESTAMP
)
PARTITION BY DATE_TRUNC(date_prevue, MONTH)
CLUSTER BY article_id
OPTIONS(description="Réceptions attendues et réalisées : entrée du calcul de risque de rupture");

-- ---------------------------------------------------------------------
-- Transport et ressources
-- ---------------------------------------------------------------------

CREATE OR REPLACE TABLE sc_raw.creneaux (
  creneau_id        STRING NOT NULL,
  site_id           STRING,
  quai              STRING,
  transporteur_id   STRING,
  type_flux         STRING OPTIONS(description="ENLEVEMENT (sortant) ou LIVRAISON_ENTRANTE"),
  debut_prevu       TIMESTAMP,
  fin_prevue        TIMESTAMP,
  arrivee_reelle    TIMESTAMP,
  depart_reel       TIMESTAMP,
  nb_colis_prevus   INT64,
  nb_colis_charges  INT64,
  _charge_le        TIMESTAMP
)
PARTITION BY TIMESTAMP_TRUNC(debut_prevu, MONTH)
CLUSTER BY transporteur_id
OPTIONS(description="Rendez-vous quais : base du KPI de respect des créneaux");

CREATE OR REPLACE TABLE sc_raw.temps_travail (
  pointage_id    STRING NOT NULL,
  date_travail   DATE,
  site_id        STRING,
  equipe         STRING,
  operateur_id   STRING,
  activite       STRING OPTIONS(description="Mêmes valeurs que standards_activite.activite, plus INDIRECT (réunions, formation)"),
  heures         NUMERIC,
  _charge_le     TIMESTAMP
)
PARTITION BY DATE_TRUNC(date_travail, MONTH)
CLUSTER BY equipe, activite
OPTIONS(description="Heures travaillées par opérateur et activité : dénominateur de la productivité");

-- ---------------------------------------------------------------------
-- Qualité pharma
-- ---------------------------------------------------------------------

CREATE OR REPLACE TABLE sc_raw.retours (
  retour_id                 STRING NOT NULL,
  commande_id               STRING,
  ligne_id                  STRING,
  destinataire_id           STRING,
  article_id                STRING,
  lot_id                    STRING,
  qte_retournee             INT64,
  date_expedition_origine   TIMESTAMP,
  date_retour               TIMESTAMP,
  motif                     STRING OPTIONS(description="ERREUR_COMMANDE, ERREUR_PREPA, PRODUIT_ENDOMMAGE, RAPPEL, AUTRE"),
  statut_qualite_entree     STRING OPTIONS(description="Statut à l entrée : stock séparé du vendable jusqu à décision"),
  decision_rp               STRING OPTIONS(description="Décision de la Personne Responsable : EN_ATTENTE, REMISE_EN_STOCK, DESTRUCTION, RETOUR_LABO"),
  date_decision_rp          TIMESTAMP,
  _charge_le                TIMESTAMP
)
PARTITION BY TIMESTAMP_TRUNC(date_retour, MONTH)
CLUSTER BY lot_id
OPTIONS(description="Retours clients : séparation, décision RP, délai de retour (règles BPD)");

CREATE OR REPLACE TABLE sc_raw.reclamations (
  reclamation_id    STRING NOT NULL,
  commande_id       STRING,
  ligne_id          STRING,
  destinataire_id   STRING,
  date_reclamation  TIMESTAMP,
  type_reclamation  STRING OPTIONS(description="MANQUANT, EXCEDENT, ERREUR_ARTICLE, ERREUR_LOT, CASSE, TEMPERATURE, RETARD"),
  qte_concernee     INT64,
  statut            STRING OPTIONS(description="OUVERTE, JUSTIFIEE, NON_JUSTIFIEE"),
  cause_imputee     STRING OPTIONS(description="ENTREPOT, TRANSPORT, DESTINATAIRE, DONNEUR_ORDRE"),
  _charge_le        TIMESTAMP
)
PARTITION BY TIMESTAMP_TRUNC(date_reclamation, MONTH)
OPTIONS(description="Réclamations : erreurs détectées par le client, après expédition (taux d erreur externe)");

CREATE OR REPLACE TABLE sc_raw.releves_temperature (
  capteur_id      STRING NOT NULL,
  zone_id         STRING,
  horodatage      TIMESTAMP,
  temperature_c   NUMERIC,
  _charge_le      TIMESTAMP
)
PARTITION BY TIMESTAMP_TRUNC(horodatage, MONTH)
CLUSTER BY zone_id
OPTIONS(description="Relevés des sondes de température par zone. Alimentée plus tard via Pub/Sub");
