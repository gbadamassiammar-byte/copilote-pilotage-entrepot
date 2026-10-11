-- =====================================================================
-- 00_datasets.sql : création des datasets (schémas) BigQuery
-- A exécuter dans la console BigQuery, projet sélectionné.
-- Tous les datasets sont en location EU : BigQuery refuse les requêtes
-- qui joignent des datasets de locations différentes.
-- La console exécute par défaut en région US : la ligne SET ci-dessous
-- force l'exécution du script en EU. Elle doit rester la première
-- instruction du script.
-- =====================================================================

SET @@location = 'EU';

CREATE SCHEMA IF NOT EXISTS sc_raw
OPTIONS (location = 'EU',
         description = 'Données brutes simulées de type extraction SAP / WMS, avec anomalies volontaires');

CREATE SCHEMA IF NOT EXISTS sc_dq
OPTIONS (location = 'EU',
         description = 'Fiabilisation : catalogue de règles, exécutions, anomalies détectées');

CREATE SCHEMA IF NOT EXISTS sc_core
OPTIONS (location = 'EU',
         description = 'Données nettoyées et dédoublonnées, base des KPI et du ML');

CREATE SCHEMA IF NOT EXISTS sc_kpi
OPTIONS (location = 'EU',
         description = 'Définitions versionnées des KPI et tables KPI agrégées');

CREATE SCHEMA IF NOT EXISTS sc_lean
OPTIONS (location = 'EU',
         description = 'Amélioration continue : dérives détectées et plans d actions');

CREATE SCHEMA IF NOT EXISTS sc_ml
OPTIONS (location = 'EU',
         description = 'Features et prévisions de charge / rupture');

CREATE SCHEMA IF NOT EXISTS sc_eval
OPTIONS (location = 'EU',
         description = 'Vérité terrain des anomalies et scénarios injectés. Jamais exposé à l agent');
