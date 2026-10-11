# Copilote de pilotage d'entrepôt

Projet de démonstration : agent conversationnel (Gemini, tool calling) qui aide un responsable
d'exploitation d'un site logistique pharmaceutique (3PL) à fiabiliser ses données, piloter ses KPI
façon OMS, détecter les dérives (Lean) et prévoir la charge. Données simulées "de type" extraction
SAP / WMS ; les correspondances SAP sont indicatives.

## Hypothèses du site simulé

- 1 site pharma, 3 donneurs d'ordre (laboratoires fictifs), destinataires : officines, grossistes, hôpitaux
- Organisation en 3x8 (matin, après-midi, nuit), opérateurs CDI / CDD / intérim
- 12 mois d'historique, gestion par lot (péremption, statut qualité, rappel)
- Zones avec plage de température qualifiée ; chaque article a sa propre plage

## Couches BigQuery (location EU)

| Dataset   | Rôle |
|-----------|------|
| sc_raw    | Données brutes simulées, avec anomalies volontaires |
| sc_dq     | Règles de fiabilisation, exécutions, anomalies détectées |
| sc_core   | Vues nettoyées (couche 2) |
| sc_kpi    | Définitions des KPI et tables KPI agrégées |
| sc_lean   | Dérives détectées et plans d'actions |
| sc_ml     | Features et prévisions (couche 4) |
| sc_eval   | Vérité terrain des anomalies et scénarios injectés (cachée à l'agent) |

## Etape 1 : tables BigQuery

Exécuter dans la console BigQuery, dans l'ordre : `sql/00_datasets.sql`, `sql/01_raw_referentiels.sql`,
`sql/02_raw_flux.sql`, `sql/03_pilotage.sql`. Les instructions CREATE ne sont pas facturées.

## Etape 2 : données simulées

Depuis Cloud Shell (gratuit, déjà authentifié) :

```bash
cd generateur
pip install -r requirements.txt
python3 generer.py --sortie donnees                 # environ 2 minutes, 80 Mo compressés
python3 charger_bigquery.py --projet $GOOGLE_CLOUD_PROJECT --dossier donnees
```

Le générateur est une simulation à événements discrets : commandes, préparation FEFO par lot,
stock réconciliable (photo J = photo J-1 + mouvements J), réceptions et contrôle qualité,
retours avec décision RP, réclamations, créneaux transporteurs, relevés de température.
Il injecte des anomalies de données et 5 scénarios de dérive, tous tracés dans `sc_eval`.
Les paramètres sont dans `generateur/config.py`. Même graine = mêmes données.

| Scénario | Ce qui se passe | Signal attendu |
|---|---|---|
| S1 | Intérimaires non formés en équipe de nuit (mars 2026) | Taux d'erreur de préparation de nuit x3 à x5 |
| S2 | Transporteur en retard sur le créneau de 16h (mai-juin 2026) | Respect du créneau et OTIF en baisse sur ce transporteur |
| S3 | Blocage qualité d'un article à forte rotation (janvier 2026) | Ruptures quasi totales sur l'article pendant 2 semaines |
| S4 | Lot rappelé bloqué avec 2 jours de retard (juin 2026) | Expéditions d'un lot rappelé, retours de rappel |
| S5 | Retours hors délai remis en stock (août-sept. 2026) | Non-conformités BPD sur les retours |

Le chargement utilise des jobs de chargement BigQuery, gratuits. Volume : environ 4 millions de
lignes, sous le palier gratuit de 10 Gio de stockage.

## Feuille de route

1. Modèle de données et DDL  (fait)
2. Générateur Python de données simulées (anomalies + scénarios de dérive)  <- en cours
3. Outil 1 de l'agent : fiabilisation des données
4. KPI OMS et traçabilité
5. Agent Gemini + application Cloud Run
6. Détection de dérives et prévision (BigQuery ML)
7. Restitution Looker Studio et Power BI
