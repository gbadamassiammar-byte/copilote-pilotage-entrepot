"""
Réglages de l'application, lus dans les variables d'environnement.

Tout ce qui change entre ton PC et Cloud Run (projet, modèle, code d'accès)
passe par ici : le code ne contient aucune valeur propre à un environnement.
"""
import os


def _charger_fichier_env():
    """Lit backend/.env (lignes CLE=valeur) s'il existe, sans écraser les variables déjà définies.
    Pratique sous Windows : pas besoin de retaper les variables à chaque terminal."""
    chemin = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.isfile(chemin):
        return
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#") and "=" in ligne:
                cle, valeur = ligne.split("=", 1)
                os.environ.setdefault(cle.strip(), valeur.strip().strip('"'))


_charger_fichier_env()

# "gcp"  : BigQuery + Vertex AI (Gemini) + Firestore réels
# "demo" : données d'exemple embarquées et réponses simulées, sans Google Cloud
#          (pour développer l'interface hors ligne ou faire une démo sans réseau)
MODE = os.getenv("COPILOTE_MODE", "gcp").lower()

PROJET = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or ""
BQ_LOCATION = os.getenv("BQ_LOCATION", "EU")

# Gemini via Vertex AI : authentification par les identifiants Google (ADC),
# aucune clé API à stocker. Région européenne : les questions restent en Europe.
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "europe-west1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.2"))

# Garde-fous
MAX_ETAPES_AGENT = int(os.getenv("MAX_ETAPES_AGENT", "6"))          # allers-retours outils par question
MAX_QUESTIONS_PAR_JOUR = int(os.getenv("MAX_QUESTIONS_PAR_JOUR", "300"))
MAX_LONGUEUR_QUESTION = 2000
HISTORIQUE_TOURS = int(os.getenv("HISTORIQUE_TOURS", "8"))          # derniers échanges renvoyés au modèle
OCTETS_MAX_LECTURE = int(os.getenv("OCTETS_MAX_LECTURE", str(300 * 1024**2)))   # 300 Mo par requête de lecture
OCTETS_MAX_CONTROLES = int(os.getenv("OCTETS_MAX_CONTROLES", str(5 * 1024**3)))  # 5 Go pour l'exécution des contrôles
DELAI_MIN_ENTRE_CONTROLES_S = int(os.getenv("DELAI_MIN_ENTRE_CONTROLES_S", "300"))

# Si renseigné, l'API exige ce code (en-tête X-Code-Acces). Indispensable une fois
# l'application en ligne : sans lui, n'importe qui pourrait consommer tes crédits.
CODE_ACCES = os.getenv("CODE_ACCES", "")

# Prix indicatifs (dollars par million de jetons) pour afficher une estimation de coût.
PRIX_ENTREE_PAR_M = float(os.getenv("PRIX_ENTREE_PAR_M", "0.30"))
PRIX_SORTIE_PAR_M = float(os.getenv("PRIX_SORTIE_PAR_M", "2.50"))

FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "conversations")
PORT = int(os.getenv("PORT", "8080"))
