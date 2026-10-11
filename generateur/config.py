"""
Paramètres du site simulé. Tout ce qui est "réglable" est ici, pour pouvoir
expliquer et modifier les hypothèses sans toucher au moteur de simulation.
"""
from datetime import date, datetime, time

GRAINE = 2026                      # même graine = mêmes données, reproductible
DEBUT = date(2025, 10, 1)
FIN = date(2026, 9, 30)            # inclus
EXTRACTION = datetime(2026, 9, 30, 23, 30)   # "photo" de l'extraction : rien n'existe après
CHARGE_LE = datetime(2026, 10, 1, 2, 15)     # horodatage technique de chargement

# --------------------------------------------------------------------------
# Site
# --------------------------------------------------------------------------
SITE = dict(site_id="WH-LS01", code_site="LS01", nom="Plateforme Loire Santé",
            ville="Saran", region="Centre-Val de Loire", nb_quais=12,
            capacite_lignes_jour=4200)

# zone_id, code, libellé, type_zone, temp_min, temp_max, nb emplacements, préfixe allée
ZONES = [
    ("Z01", "REC", "Quais de réception",      "RECEPTION",   15, 25, 10,  "R"),
    ("Z02", "PAM", "Picking ambiant",         "PICKING",     15, 25, 700, "A"),
    ("Z03", "RAM", "Réserve ambiante",        "STOCKAGE",    15, 25, 300, "B"),
    ("Z04", "PFR", "Chambre froide",          "FROID",        2,  8, 180, "F"),
    ("Z05", "SEC", "Zone sécurisée",          "SECURISEE",   15, 25, 50,  "S"),
    ("Z06", "QUA", "Quarantaine retours",     "QUARANTAINE", 15, 25, 30,  "Q"),
    ("Z07", "EXP", "Expédition",              "EXPEDITION",  15, 25, 12,  "E"),
]
# Sondes de température : capteur_id, zone_id, consigne moyenne, écart-type
CAPTEURS = [("TS-02-1", "Z02", 20.0, 0.7), ("TS-03-1", "Z03", 20.0, 0.7),
            ("TS-04-1", "Z04", 5.0, 0.45), ("TS-04-2", "Z04", 5.2, 0.5),
            ("TS-05-1", "Z05", 20.0, 0.7), ("TS-06-1", "Z06", 20.0, 0.7)]
PAS_RELEVE_MIN = 15

# --------------------------------------------------------------------------
# Donneurs d'ordre (laboratoires fictifs)
# id, nom, objectif OTIF %, objectif précision %, heure limite, part du volume,
# part de commandes "froid", nb articles, délai d'appro (jours ouvrés)
# --------------------------------------------------------------------------
DONNEURS = [
    ("DO-01", "Laboratoires Alvena", 97.0, 99.8, time(14, 0), 0.45, 0.15, 220, 8),
    ("DO-02", "Kerys Pharma",        96.0, 99.7, time(13, 0), 0.35, 0.25, 200, 12),
    ("DO-03", "Nordline Santé",      95.0, 99.6, time(15, 0), 0.20, 0.10, 180, 6),
]

# Destinataires : type, part du parc, poids de fréquence de commande,
# (min, max) lignes par commande, (min, max) unités par ligne, autorisation de gros
DESTINATAIRES = {
    "OFFICINE":  dict(part=0.75, poids=1.0, lignes=(1, 5),  qte=(1, 6),   gros=False),
    "CLINIQUE":  dict(part=0.10, poids=1.5, lignes=(2, 6),  qte=(2, 12),  gros=False),
    "HOPITAL":   dict(part=0.12, poids=2.0, lignes=(3, 10), qte=(5, 40),  gros=False),
    "GROSSISTE": dict(part=0.03, poids=4.0, lignes=(5, 15), qte=None,    gros=True),
}
NB_DESTINATAIRES = 1500
REGIONS = ["Centre-Val de Loire", "Ile-de-France", "Pays de la Loire", "Bretagne",
           "Normandie", "Hauts-de-France", "Grand Est", "Bourgogne-Franche-Comté",
           "Nouvelle-Aquitaine", "Auvergne-Rhône-Alpes", "Occitanie",
           "Provence-Alpes-Côte d'Azur"]

# --------------------------------------------------------------------------
# Transporteurs : id, nom, mode, créneaux d'enlèvement (heure), retard de
# livraison de base (probabilité de J+1 supplémentaire)
# --------------------------------------------------------------------------
TRANSPORTEURS = [
    ("T01", "Medi Express",        "EXPRESS",      [time(16, 0)],                0.015),
    ("T02", "Messagerie Santé",    "MESSAGERIE",   [time(11, 30), time(17, 30)], 0.025),
    ("T03", "Cryo Transport",      "FRIGORIFIQUE", [time(7, 30), time(16, 30)],  0.015),
    ("T04", "Pharma Route",        "MESSAGERIE",   [time(18, 0)],                0.025),
    ("T05", "Fret Hospitalier",    "MESSAGERIE",   [time(17, 0)],                0.020),
]

# --------------------------------------------------------------------------
# Volumes
# --------------------------------------------------------------------------
COMMANDES_JOUR_BASE = 650
FACTEUR_JOUR = {0: 1.30, 1: 1.10, 2: 1.00, 3: 0.95, 4: 0.85, 5: 0.20, 6: 0.08}
FACTEUR_MOIS = {1: 1.15, 2: 1.10, 3: 1.00, 4: 0.97, 5: 0.95, 6: 0.95, 7: 0.90,
                8: 0.80, 9: 1.00, 10: 1.00, 11: 1.05, 12: 1.10}
TENDANCE_ANNUELLE = 0.06
PART_URGENT = 0.05
PART_ANNULATION = 0.01

# --------------------------------------------------------------------------
# Ressources et productivité
# --------------------------------------------------------------------------
EQUIPES = {"NUIT": (21, 5), "MATIN": (5, 13), "APRES_MIDI": (13, 21)}
PERMANENTS_PAR_EQUIPE = 6
CONTROLEURS_PAR_EQUIPE = 2         # parmi les permanents
POSTES_INTERIM_PAR_EQUIPE = 2
TAUX_ABSENCE = 0.07
HEURES_POSTE = 7.5
# activité, type_zone, unité d'oeuvre, cadence standard / heure
STANDARDS = [
    ("PICKING", "PICKING", "LIGNE", 60.0),
    ("PICKING", "FROID", "LIGNE", 42.0),
    ("PICKING", "SECURISEE", "LIGNE", 35.0),
    ("CONTROLE", "EXPEDITION", "LIGNE", 150.0),
    ("RECEPTION", "RECEPTION", "PALETTE", 6.0),
    ("RANGEMENT", "STOCKAGE", "PALETTE", 8.0),
    ("CHARGEMENT", "EXPEDITION", "COLIS", 120.0),
]

# --------------------------------------------------------------------------
# Qualité de préparation
# --------------------------------------------------------------------------
TAUX_ERREUR_BASE = 0.0025          # par tâche de prélèvement
TAUX_CONTROLE = 0.95               # part des tâches contrôlées
TAUX_DETECTION_CONTROLE = 0.90     # part des erreurs détectées quand contrôlé
DUREE_VIE_MIN_EXPEDITION_JOURS = 60

# --------------------------------------------------------------------------
# Anomalies de données injectées (probabilités par ligne concernée)
# --------------------------------------------------------------------------
ANOMALIES = dict(
    doublon_commande=0.004,
    doublon_metier=0.003,
    entete_annulee_lignes_livrees=0.001,
    doublon_ligne=0.002,
    date_livraison_manquante=0.008,
    date_expedition_manquante=0.003,
    statut_non_mis_a_jour=0.004,
    dates_inversees=0.001,
    qte_expediee_superieure=0.001,
    article_inconnu=0.0005,
    sequence_statuts_impossible=0.0015,
    mouvement_perdu=0.0004,
    doublon_mouvement=0.0003,
    non_respect_fefo=0.004,
    remise_stock_sans_decision_rp=0.005,
    lots_zone_incompatible=6,      # nombre de lots
)

# --------------------------------------------------------------------------
# Scénarios de dérive (la cause réelle est cachée dans sc_eval.scenarios)
# --------------------------------------------------------------------------
SCENARIOS = dict(
    S1=dict(libelle="Hausse des erreurs de préparation en équipe de nuit",
            debut=date(2026, 3, 2), fin=date(2026, 3, 22), equipe="NUIT",
            nb_interimaires=6, multiplicateur_erreur=6.0, vitesse=0.75,
            cause="Intégration de 6 intérimaires sans formation au poste en équipe de nuit"),
    S2=dict(libelle="Retards sur le créneau d'enlèvement de 16h",
            debut=date(2026, 5, 11), fin=date(2026, 6, 5), transporteur="T01",
            creneau=time(16, 0), proba_retard=0.45,
            cause="Le transporteur Medi Express arrive en retard au créneau de 16h après réorganisation de sa tournée"),
    S3=dict(libelle="Ruptures sur un article à forte rotation",
            debut=date(2026, 1, 12), fin=date(2026, 1, 26), donneur="DO-01",
            cause="Blocage qualité des lots disponibles de l'article et réapprovisionnement retardé"),
    S4=dict(libelle="Expéditions d'un lot rappelé",
            date_rappel=date(2026, 6, 22), delai_blocage_jours=2, donneur="DO-02",
            cause="Rappel de lot enregistré mais blocage du stock dans le WMS effectué avec 2 jours de retard"),
    S5=dict(libelle="Retours remis en stock hors délai",
            debut=date(2026, 8, 17), fin=date(2026, 9, 11), delai_max_jours=5,
            cause="Retours d'officines reçus après le délai de 5 jours remis en stock vendable par erreur"),
)
EXCURSION_TEMPERATURE = dict(capteur="TS-04-1", debut=datetime(2026, 7, 21, 14, 0),
                             duree_min=150, pic=9.6)
TROUS_CAPTEUR = [("TS-04-2", datetime(2026, 2, 9, 1, 0), 6 * 60),
                 ("TS-02-1", datetime(2026, 8, 3, 10, 0), 4 * 60)]
