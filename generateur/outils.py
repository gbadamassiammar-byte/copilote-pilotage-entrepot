"""
Outils communs : formats de dates, calendrier, écriture des fichiers NDJSON
compressés, enregistrement de la vérité terrain.
"""
import gzip
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
_cache_offset = {}


def fts(dt):
    """Horodatage local Paris -> texte avec décalage horaire, lu par BigQuery
    comme un TIMESTAMP (stocké en UTC). None reste None."""
    if dt is None:
        return None
    d = dt.date()
    off = _cache_offset.get(d)
    if off is None:
        o1 = datetime(d.year, d.month, d.day, 0, tzinfo=PARIS).strftime("%z")
        o2 = datetime(d.year, d.month, d.day, 23, tzinfo=PARIS).strftime("%z")
        off = o1[:3] + ":" + o1[3:] if o1 == o2 else ""     # "" = jour de changement d'heure
        _cache_offset[d] = off
    if not off:
        o = dt.replace(tzinfo=PARIS).strftime("%z")
        off_dt = o[:3] + ":" + o[3:]
        return f"{dt:%Y-%m-%d %H:%M:%S}{off_dt}"
    return f"{dt:%Y-%m-%d %H:%M:%S}{off}"


def fd(d):
    return None if d is None else d.isoformat()


# --------------------------------------------------------------------------
# Calendrier français
# --------------------------------------------------------------------------
def paques(annee):
    """Dimanche de Pâques (algorithme de Meeus)."""
    a, b, c = annee % 19, annee // 100, annee % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mois = (h + l - 7 * m + 114) // 31
    jour = ((h + l - 7 * m + 114) % 31) + 1
    return date(annee, mois, jour)


def feries(annee):
    p = paques(annee)
    return {
        date(annee, 1, 1): "Jour de l'an",
        p + timedelta(days=1): "Lundi de Pâques",
        date(annee, 5, 1): "Fête du travail",
        date(annee, 5, 8): "Victoire 1945",
        p + timedelta(days=39): "Ascension",
        p + timedelta(days=50): "Lundi de Pentecôte",
        date(annee, 7, 14): "Fête nationale",
        date(annee, 8, 15): "Assomption",
        date(annee, 11, 1): "Toussaint",
        date(annee, 11, 11): "Armistice",
        date(annee, 12, 25): "Noël",
    }


class Calendrier:
    def __init__(self, a_min=2024, a_max=2028):
        self.feries = {}
        for a in range(a_min, a_max + 1):
            self.feries.update(feries(a))

    def ouvre(self, d):
        return d.weekday() < 5 and d not in self.feries

    def ouvre_suivant(self, d, n=1):
        """n-ième jour ouvré strictement après d."""
        while n > 0:
            d += timedelta(days=1)
            if self.ouvre(d):
                n -= 1
        return d

    def ouvre_ou_suivant(self, d):
        return d if self.ouvre(d) else self.ouvre_suivant(d)


# --------------------------------------------------------------------------
# Ecriture
# --------------------------------------------------------------------------
class Ecrivain:
    """Ecrit chaque table en fichiers NDJSON gzip découpés en parties, format
    accepté directement par les jobs de chargement BigQuery (gratuits)."""

    def __init__(self, dossier, lignes_par_fichier=250_000):
        self.dossier = dossier
        self.lpf = lignes_par_fichier
        os.makedirs(dossier, exist_ok=True)
        for f in os.listdir(dossier):
            if f.endswith(".json.gz"):
                os.remove(os.path.join(dossier, f))
        self.fichiers, self.compteurs, self.parties = {}, {}, {}

    def ecrire(self, table, ligne):
        n = self.compteurs.get(table, 0)
        if n % self.lpf == 0:
            if table in self.fichiers:
                self.fichiers[table].close()
            p = self.parties.get(table, 0) + 1
            self.parties[table] = p
            chemin = os.path.join(self.dossier, f"{table}.part{p:03d}.json.gz")
            self.fichiers[table] = gzip.open(chemin, "wt", encoding="utf-8", compresslevel=5)
        self.fichiers[table].write(json.dumps(ligne, ensure_ascii=False, separators=(",", ":")))
        self.fichiers[table].write("\n")
        self.compteurs[table] = n + 1

    def fermer(self):
        for f in self.fichiers.values():
            f.close()


class Verite:
    """Vérité terrain : tout ce qui a été injecté volontairement."""

    def __init__(self, ecrivain, charge_le):
        self.e = ecrivain
        self.n = 0
        self.charge_le = fts(charge_le)
        self.compteur_types = {}

    def ajouter(self, type_anomalie, table, cle, detail=None, scenario=None):
        self.n += 1
        self.compteur_types[type_anomalie] = self.compteur_types.get(type_anomalie, 0) + 1
        self.e.ecrire("sc_eval.anomalies_injectees", {
            "injection_id": f"INJ{self.n:07d}",
            "scenario_id": scenario,
            "type_anomalie": type_anomalie,
            "table_cible": table,
            "cle_ligne": cle,
            "detail": detail or {},
            "injectee_le": self.charge_le,
        })
