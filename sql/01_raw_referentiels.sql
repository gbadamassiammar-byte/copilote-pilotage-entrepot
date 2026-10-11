-- =====================================================================
-- 01_raw_referentiels.sql : référentiels (dimensions) de la couche brute
--
-- Choix de conception :
-- * Identifiants en STRING, comme dans un ERP (ex : 'ART-000123').
-- * Aucune contrainte PRIMARY KEY : dans BigQuery elles ne sont pas
--   vérifiées (NOT ENFORCED) et l'optimiseur s'en sert pour réécrire les
--   requêtes. Déclarer une clé sur une table qui contient des doublons
--   volontaires peut donc produire des résultats faux. Les clés seront
--   déclarées plus tard, dans sc_core, sur des données dédoublonnées.
-- * Peu de colonnes obligatoires (NOT NULL) : la couche brute doit pouvoir
--   accueillir des valeurs manquantes, c'est ce que l'outil de fiabilisation
--   doit détecter.
-- * Les descriptions de colonnes servent de documentation ET de contexte
--   pour l'agent, qui pourra les lire via INFORMATION_SCHEMA.
-- * Correspondances SAP : indicatives, elles varient selon la version
--   (ECC ou S/4HANA, WM ou EWM).
-- * CREATE OR REPLACE : la couche brute est régénérable par le script
--   Python, on peut donc la recréer sans risque. Attention, cela efface
--   les données existantes de la table.
-- =====================================================================

SET @@location = 'EU';  -- force l'exécution en EU (la console est en US par défaut)

CREATE OR REPLACE TABLE sc_raw.dim_site (
  site_id               STRING NOT NULL OPTIONS(description="Identifiant du site. SAP indicatif : WERKS (division)"),
  code_site             STRING OPTIONS(description="Code court du site"),
  nom                   STRING OPTIONS(description="Nom du site"),
  ville                 STRING,
  region                STRING,
  nb_quais              INT64  OPTIONS(description="Nombre de quais de chargement"),
  capacite_lignes_jour  INT64  OPTIONS(description="Capacité nominale de préparation, en lignes par jour"),
  actif                 BOOL,
  _charge_le            TIMESTAMP OPTIONS(description="Horodatage technique de chargement")
)
OPTIONS(description="Sites logistiques");

CREATE OR REPLACE TABLE sc_raw.dim_zone (
  zone_id       STRING NOT NULL OPTIONS(description="Identifiant de la zone. SAP indicatif : LGORT / type de magasin"),
  site_id       STRING,
  code_zone     STRING,
  libelle       STRING,
  type_zone     STRING OPTIONS(description="RECEPTION, STOCKAGE, PICKING, FROID, QUARANTAINE, SECURISEE, EXPEDITION"),
  temp_min_c    NUMERIC OPTIONS(description="Borne basse de la plage de température qualifiée de la zone, en °C"),
  temp_max_c    NUMERIC OPTIONS(description="Borne haute de la plage de température qualifiée de la zone, en °C"),
  actif         BOOL,
  _charge_le    TIMESTAMP
)
OPTIONS(description="Zones de l entrepôt avec leur plage de température qualifiée");

CREATE OR REPLACE TABLE sc_raw.dim_emplacement (
  emplacement_id     STRING NOT NULL OPTIONS(description="Identifiant de l emplacement. SAP indicatif : LGPLA"),
  zone_id            STRING,
  code_emplacement   STRING OPTIONS(description="Adresse lisible, ex : A-12-03"),
  allee              STRING,
  niveau             INT64,
  type_emplacement   STRING OPTIONS(description="PICKING ou RESERVE"),
  actif              BOOL,
  _charge_le         TIMESTAMP
)
OPTIONS(description="Emplacements de stockage");

CREATE OR REPLACE TABLE sc_raw.dim_donneur_ordre (
  donneur_ordre_id        STRING NOT NULL OPTIONS(description="Client du 3PL (laboratoire fictif)"),
  nom                     STRING,
  objectif_otif_pct       NUMERIC OPTIONS(description="Objectif contractuel d OTIF, en %"),
  objectif_precision_pct  NUMERIC OPTIONS(description="Objectif contractuel de précision de préparation, en %"),
  heure_limite_commande   TIME    OPTIONS(description="Heure limite de réception des commandes pour expédition le jour même"),
  date_debut_contrat      DATE,
  _charge_le              TIMESTAMP
)
OPTIONS(description="Donneurs d ordre : clients contractuels du 3PL");

CREATE OR REPLACE TABLE sc_raw.dim_destinataire (
  destinataire_id    STRING NOT NULL OPTIONS(description="Lieu de livraison. SAP indicatif : KUNNR (partenaire réceptionnaire)"),
  nom                STRING,
  type_destinataire  STRING OPTIONS(description="OFFICINE, GROSSISTE, HOPITAL, CLINIQUE"),
  autorisation_gros  BOOL   OPTIONS(description="Vrai si le destinataire détient une autorisation de distribution en gros (impacte la règle de délai des retours)"),
  ville              STRING,
  code_postal        STRING,
  region             STRING,
  _charge_le         TIMESTAMP
)
OPTIONS(description="Destinataires des livraisons");

CREATE OR REPLACE TABLE sc_raw.dim_article (
  article_id        STRING NOT NULL OPTIONS(description="Identifiant article. SAP indicatif : MATNR"),
  sku               STRING OPTIONS(description="Code article lisible"),
  libelle           STRING,
  donneur_ordre_id  STRING OPTIONS(description="Propriétaire du stock"),
  famille           STRING,
  classe_abc        STRING OPTIONS(description="A, B ou C selon la rotation"),
  temp_min_c        NUMERIC OPTIONS(description="Température minimale de conservation de l article, en °C"),
  temp_max_c        NUMERIC OPTIONS(description="Température maximale de conservation de l article, en °C"),
  gestion_lot       BOOL   OPTIONS(description="Article géré par lot. SAP indicatif : XCHPF"),
  duree_vie_mois    INT64  OPTIONS(description="Durée de vie totale du produit, en mois"),
  unite_vente       STRING OPTIONS(description="Unité de vente, ex : BOITE"),
  colisage          INT64  OPTIONS(description="Nombre d unités par colis"),
  poids_kg          NUMERIC,
  volume_dm3        NUMERIC,
  statut_article    STRING OPTIONS(description="ACTIF ou DEREFERENCE"),
  date_creation     DATE,
  _charge_le        TIMESTAMP
)
OPTIONS(description="Articles (référentiel produit)");

CREATE OR REPLACE TABLE sc_raw.dim_lot (
  lot_id            STRING NOT NULL OPTIONS(description="Identifiant technique du lot"),
  article_id        STRING,
  num_lot           STRING OPTIONS(description="Numéro de lot fabricant. SAP indicatif : CHARG"),
  date_fabrication  DATE,
  date_peremption   DATE   OPTIONS(description="Date de péremption. SAP indicatif : VFDAT"),
  statut_qualite    STRING OPTIONS(description="LIBRE, CONTROLE_QUALITE ou BLOQUE (types de stock SAP : libre, contrôle qualité, bloqué)"),
  rappele           BOOL   OPTIONS(description="Lot visé par un rappel"),
  date_rappel       DATE,
  date_maj          TIMESTAMP,
  _charge_le        TIMESTAMP
)
OPTIONS(description="Lots : péremption, statut qualité, rappel");

CREATE OR REPLACE TABLE sc_raw.dim_operateur (
  operateur_id   STRING NOT NULL,
  site_id        STRING,
  equipe         STRING OPTIONS(description="MATIN, APRES_MIDI ou NUIT (organisation 3x8)"),
  type_contrat   STRING OPTIONS(description="CDI, CDD ou INTERIM"),
  date_arrivee   DATE,
  date_depart    DATE,
  actif          BOOL,
  _charge_le     TIMESTAMP
)
OPTIONS(description="Opérateurs d entrepôt (pseudonymisés)");

CREATE OR REPLACE TABLE sc_raw.dim_transporteur (
  transporteur_id  STRING NOT NULL,
  nom              STRING,
  mode             STRING OPTIONS(description="MESSAGERIE, EXPRESS ou FRIGORIFIQUE"),
  actif            BOOL,
  _charge_le       TIMESTAMP
)
OPTIONS(description="Transporteurs");

CREATE OR REPLACE TABLE sc_raw.dim_calendrier (
  date_jour       DATE NOT NULL,
  annee           INT64,
  mois            INT64,
  semaine_iso     INT64,
  jour_semaine    INT64  OPTIONS(description="1 = lundi ... 7 = dimanche"),
  jour_ouvre      BOOL,
  ferie           BOOL,
  libelle_ferie   STRING
)
OPTIONS(description="Calendrier : jours ouvrés et fériés, feature clé de la prévision de charge");

CREATE OR REPLACE TABLE sc_raw.standards_activite (
  standard_id              STRING NOT NULL,
  site_id                  STRING,
  activite                 STRING OPTIONS(description="RECEPTION, RANGEMENT, PICKING, CONTROLE, CHARGEMENT"),
  type_zone                STRING OPTIONS(description="Le standard peut varier selon la zone (ex : picking en froid plus lent)"),
  unite_oeuvre             STRING OPTIONS(description="LIGNE, COLIS ou PALETTE"),
  cadence_std_par_heure    NUMERIC OPTIONS(description="Cadence standard attendue, en unités d oeuvre par heure"),
  valide_depuis            DATE,
  valide_jusqu             DATE,
  _charge_le               TIMESTAMP
)
OPTIONS(description="Standards de productivité par activité : base du taux de performance OMS et du dimensionnement des équipes");
