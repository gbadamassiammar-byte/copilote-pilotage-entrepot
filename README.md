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

## Etape 3 : outil de fiabilisation

Exécuter dans la console BigQuery : `sql/04_dq_regles.sql` (catalogue de 21 règles),
`sql/05_dq_procedure.sql` (procédure d'exécution), `sql/06_dq_vues.sql` (vues de lecture), puis :

```sql
CALL sc_dq.executer_controles(NULL);           -- exécute toutes les règles
SELECT * FROM sc_dq.v_synthese;                -- bilan par règle
SELECT * FROM sc_eval.v_evaluation_dq;         -- rappel et précision face à la vérité terrain
```

Chaque règle est une requête SQL stockée dans `sc_dq.dq_regles` (une ligne par anomalie,
colonnes `cle_ligne` et `detail`). Ajouter un contrôle = ajouter une ligne, sans modifier la procédure.

| Dimension | Règles |
|---|---|
| Unicité | doublons techniques (commandes, lignes, mouvements), doublon métier (commande ressaisie) |
| Complétude | date de livraison ou d'expédition manquante, interruption des relevés de température |
| Cohérence | statuts en-tête/lignes, statut non mis à jour, dates inversées, séquence de statuts, écart de stock photo/mouvements |
| Exactitude | quantité expédiée > préparée, article inconnu |
| Conformité BPD | FEFO, lot rappelé prélevé, retours (décision RP, délai), zone de température, excursion, lot périmé en stock libre |

Mesure sur les données simulées : 19 règles sur 20 évaluables ont un rappel et une précision
supérieurs à 99 %. Le contrôle FEFO, approché à partir des photos quotidiennes de stock,
retrouve 91 % des prélèvements non conformes avec 99,6 % de précision.

## Etape 4 : agent copilote et interface web

Dossier `app/` : API REST Flask, agent Gemini (Vertex AI) avec 12 outils sur BigQuery, historique
Firestore, interface React (pages Copilote, Fiabilité, KPI, Dérives et actions), thème clair ou sombre. Installation et fonctionnement : `app/README.md`.

## Etape 5 : KPI OMS avec traçabilité

Exécuter `sql/07_kpi.sql` (crée la procédure), puis `CALL sc_kpi.calculer_kpi();` (environ une minute).

| Table | Contenu |
|---|---|
| `sc_kpi.lignes_service` | Une ligne de commande dédoublonnée par ligne, avec son statut de mesure (MESURE, NON_MESURABLE, EXCLU), la raison d'exclusion et les indicateurs à l'heure / complète / OTIF / rupture. C'est le lien entre un KPI et la donnée. |
| `sc_kpi.kpi_jour` | Numérateur et dénominateur de chaque KPI par jour et par périmètre (site, donneur d'ordre, transporteur, équipe). |
| `sc_kpi.definitions_kpi` | Définition versionnée de chaque KPI (formule, unité, sens favorable, date de référence). |

Principes :
- Une valeur sur une période = somme des numérateurs / somme des dénominateurs, jamais une moyenne de taux journaliers.
- Deux OTIF : fiabilisé (doublons métier retirés, lignes non mesurables comptées à part, ruptures comptées en échec)
  et brut (ce qu'afficherait un tableau branché directement sur les données). L'écart mesure le coût de la non-qualité.
- Jour de production : une tâche de préparation commencée à 21 h ou plus compte pour le lendemain (équipe de nuit).

| KPI | Valeur sur 12 mois |
|---|---|
| OTIF fiabilisé / brut | 95,96 % / 97,16 % (janvier : 94,11 % / 97,26 %) |
| Taux de service quantité | 98,55 % |
| Taux de rupture | 1,31 % |
| Lignes non mesurables | 0,88 % |
| Erreurs de préparation | 0,27 % (équipe de nuit, semaine du 2 mars : 1,53 %) |
| Productivité / performance picking | 46,1 lignes/h / 85,3 % |
| Respect des créneaux | 94,95 % (Medi Express en juin : 72,7 %) |
| Réclamations imputées à l'entrepôt | 0,25 pour 1 000 lignes |

## Etape 6 : dérives et plan d'actions (amélioration continue)

Exécuter `sql/08_lean.sql` (ajoute des colonnes à sc_lean.derives et sc_lean.actions, crée la procédure et la vue
d'évaluation), puis `CALL sc_lean.detecter_derives();` après chaque `CALL sc_kpi.calculer_kpi();`.

**Détection** : une carte de contrôle hebdomadaire par KPI et par périmètre (`sc_lean.cartes_controle`).
- Taux : carte p' de Laney. Avec 17 000 lignes par semaine, une carte p classique a des limites si étroites que
  tout déclenche ; la carte p' mesure la variation réelle d'une semaine à l'autre et élargit les limites d'autant.
- Performance picking : carte des valeurs individuelles (XmR).
- Référence : les 12 semaines précédentes, recalculée sans les semaines hors contrôle (sinon une forte dérive gonfle
  la référence et masque les semaines suivantes).
- Règles : un point au-delà de 3 sigma, ou 2 points sur 3 au-delà de 2 sigma, côté défavorable ; filtres de
  pertinence (écart minimal en valeur métier, au moins 3 échecs de plus que l'attendu).
- Les épisodes simultanés d'une même famille sont regroupés en un incident : une dérive principale et des signaux liés.
  Une rupture fait aussi baisser l'OTIF du site, du donneur et des transporteurs : c'est un seul problème à traiter.

**Analyse des causes** (`backend/lean.py`) : pour chaque segment (équipe, contrat, ancienneté, opérateur, article,
classe ABC, type de destinataire, heure de créneau...), échecs observés pendant la dérive moins échecs attendus au
taux de référence du segment. Le taux d'un petit segment est rétréci vers le taux global (estimateur bayésien empirique).
Les dimensions sont classées par pouvoir explicatif : part de l'excès portée par le premier segment, pondérée par sa rareté.

**Plan d'actions** : cause, action, responsable, échéance, date d'effet. L'efficacité est mesurée automatiquement :
le KPI est-il revenu sous la limite de contrôle dans les 4 semaines qui suivent la date d'effet ?

Résultats sur les données simulées (12 mois) : 21 incidents, soit environ 2 par mois.

| Scénario injecté | Détecté | Première semaine | Ce que l'analyse fait ressortir |
|---|---|---|---|
| S1 erreurs équipe de nuit (mars) | oui | 2 mars (délai 0) | intérimaires : 65 % des heures perdues ; opérateurs arrivés depuis moins de 30 jours |
| S2 retards Medi Express au créneau de 16h (mai-juin) | partiellement | 1er juin (délai 21 j) | 5 enlèvements sur 5 en retard ; trop peu de volume (5 par semaine) pour détecter plus tôt |
| S3 ruptures ART-00121 (janvier) | oui | 12 janvier (délai 0) | l'article ART-00121 porte 92 % des ruptures en trop |
| S4 lot rappelé, S5 retours hors délai | hors périmètre | | non-conformités détectées par les contrôles DQ_CNF_002 à 004 |

`SELECT * FROM sc_eval.v_evaluation_derives;` refait cette évaluation dans BigQuery.

## Feuille de route

1. Modèle de données et DDL  (fait)
2. Générateur Python de données simulées (anomalies + scénarios de dérive)  (fait)
3. Outil 1 de l'agent : fiabilisation des données  (fait)
4. Agent Gemini + interface web React  (fait en local ; mise en ligne Cloud Run à venir)
5. KPI OMS et traçabilité (3 nouveaux outils de l'agent, page KPI)  (fait)
6. Détection de dérives, analyse des causes et plan d'actions (4 nouveaux outils, page Dérives et actions)  (fait)
7. Prévision de charge (BigQuery ML)
8. Mise en ligne Cloud Run, restitution Looker Studio et Power BI
