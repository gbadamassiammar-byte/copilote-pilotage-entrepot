-- =====================================================================
-- 03_pilotage.sql : tables de fiabilisation, KPI, Lean et évaluation
--
-- sc_dq, sc_kpi et sc_lean utilisent CREATE TABLE IF NOT EXISTS :
-- ces tables accumulent un historique (exécutions, dérives, actions)
-- qu'on ne veut pas effacer en relançant le script.
-- sc_eval utilise CREATE OR REPLACE : la vérité terrain est régénérée
-- en même temps que les données simulées.
-- =====================================================================

SET @@location = 'EU';  -- force l'exécution en EU (la console est en US par défaut)

-- ---------------------------------------------------------------------
-- Fiabilisation (sc_dq)
-- Les règles sont des DONNEES : ajouter un contrôle = insérer une ligne.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sc_dq.dq_regles (
  regle_id      STRING NOT NULL OPTIONS(description="Ex : DQ_UNI_001"),
  libelle       STRING,
  description   STRING OPTIONS(description="Explication métier : pourquoi cette anomalie fausse les indicateurs"),
  dimension     STRING OPTIONS(description="UNICITE, COMPLETUDE, COHERENCE, EXACTITUDE, CONFORMITE, FRAICHEUR"),
  table_cible   STRING,
  severite      STRING OPTIONS(description="CRITIQUE, MAJEURE, MINEURE"),
  requete_sql   STRING OPTIONS(description="Requête qui renvoie une ligne par anomalie, avec les colonnes cle_ligne et detail"),
  active        BOOL,
  cree_le       TIMESTAMP,
  maj_le        TIMESTAMP
)
OPTIONS(description="Catalogue des règles de fiabilisation");

CREATE TABLE IF NOT EXISTS sc_dq.dq_runs (
  run_id                STRING NOT NULL,
  regle_id              STRING,
  debut                 TIMESTAMP,
  fin                   TIMESTAMP,
  nb_lignes_controlees  INT64,
  nb_anomalies          INT64,
  statut_execution      STRING OPTIONS(description="SUCCES ou ECHEC"),
  message_erreur        STRING
)
PARTITION BY DATE(debut)
OPTIONS(description="Historique des exécutions : permet de suivre le taux de fiabilité dans le temps");

CREATE TABLE IF NOT EXISTS sc_dq.dq_anomalies (
  anomalie_id   STRING NOT NULL,
  run_id        STRING,
  regle_id      STRING,
  table_cible   STRING,
  cle_ligne     STRING OPTIONS(description="Identifiant de la ligne en anomalie : lien direct vers la donnée source"),
  detail        JSON   OPTIONS(description="Valeurs constatées, au format JSON"),
  detectee_le   TIMESTAMP
)
PARTITION BY DATE(detectee_le)
CLUSTER BY regle_id
OPTIONS(description="Anomalies détectées, une ligne par anomalie");

-- ---------------------------------------------------------------------
-- KPI (sc_kpi)
-- Une définition versionnée par KPI : l'agent cite toujours la version
-- utilisée, ce qui rend le chiffre discutable et reproductible.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sc_kpi.definitions_kpi (
  kpi_code         STRING NOT NULL OPTIONS(description="Ex : OTIF_LIGNE_PROMISE"),
  version          INT64  NOT NULL,
  libelle          STRING,
  description      STRING,
  formule          STRING OPTIONS(description="Formule lisible : numérateur / dénominateur"),
  requete_sql      STRING OPTIONS(description="Requête de calcul au grain ligne, pour la traçabilité"),
  niveau           STRING OPTIONS(description="LIGNE, COMMANDE, TACHE, JOUR, EQUIPE"),
  date_reference   STRING OPTIONS(description="Date de référence utilisée, ex : date_promise"),
  tolerance_jours  INT64,
  unite            STRING OPTIONS(description="%, lignes/h, heures..."),
  sens             STRING OPTIONS(description="HAUSSE_FAVORABLE ou BAISSE_FAVORABLE"),
  valide_depuis    DATE,
  valide_jusqu     DATE
)
OPTIONS(description="Définitions versionnées des KPI");

-- ---------------------------------------------------------------------
-- Amélioration continue (sc_lean)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sc_lean.derives (
  derive_id          STRING NOT NULL,
  kpi_code           STRING,
  perimetre_type     STRING OPTIONS(description="SITE, EQUIPE, CRENEAU, ZONE, DONNEUR_ORDRE, TRANSPORTEUR"),
  perimetre_valeur   STRING,
  periode_debut      DATE,
  periode_fin        DATE,
  valeur_observee    NUMERIC,
  valeur_reference   NUMERIC,
  limite_controle    NUMERIC,
  methode            STRING OPTIONS(description="CARTE_CONTROLE, ARIMA_DETECT_ANOMALIES..."),
  analyse_causes     STRING OPTIONS(description="Première analyse de causes proposée par l agent"),
  statut             STRING OPTIONS(description="NOUVELLE, EN_ANALYSE, TRAITEE, FAUSSE_ALERTE"),
  detectee_le        TIMESTAMP
)
PARTITION BY DATE(detectee_le)
OPTIONS(description="Dérives de performance détectées");

CREATE TABLE IF NOT EXISTS sc_lean.actions (
  action_id         STRING NOT NULL,
  derive_id         STRING,
  cause_identifiee  STRING,
  action            STRING,
  responsable       STRING,
  echeance          DATE,
  statut            STRING OPTIONS(description="A_FAIRE, EN_COURS, FAITE, EFFICACE, NON_EFFICACE"),
  cree_le           TIMESTAMP,
  cloturee_le       TIMESTAMP
)
OPTIONS(description="Plan d actions rattaché aux dérives, à la manière d une routine OMS");

-- ---------------------------------------------------------------------
-- Evaluation (sc_eval) : ce que le générateur a injecté volontairement.
-- Permet de mesurer le rappel et la précision des outils de l'agent.
-- ---------------------------------------------------------------------

CREATE OR REPLACE TABLE sc_eval.scenarios (
  scenario_id      STRING NOT NULL,
  libelle          STRING,
  description      STRING,
  debut            DATE,
  fin              DATE,
  perimetre_type   STRING,
  perimetre_valeur STRING,
  cause_reelle     STRING OPTIONS(description="Cause racine simulée, que l agent doit retrouver")
)
OPTIONS(description="Scénarios de dérive opérationnelle injectés");

CREATE OR REPLACE TABLE sc_eval.anomalies_injectees (
  injection_id   STRING NOT NULL,
  scenario_id    STRING OPTIONS(description="Vide pour une anomalie de données isolée"),
  type_anomalie  STRING OPTIONS(description="Ex : DOUBLON_TECHNIQUE, DATE_MANQUANTE, ECART_STOCK, SORTIE_LOT_NON_LIBRE"),
  table_cible    STRING,
  cle_ligne      STRING,
  detail         JSON,
  injectee_le    TIMESTAMP
)
CLUSTER BY type_anomalie
OPTIONS(description="Vérité terrain des anomalies de données injectées");
