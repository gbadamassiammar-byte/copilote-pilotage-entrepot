"""
Point d'entrée du générateur.

    python generer.py --sortie donnees            # volume complet (~5-10 min)
    python generer.py --sortie test --echelle 0.1 # essai rapide

Produit un dossier de fichiers NDJSON gzip, un par table et par partie,
nommés <dataset>.<table>.partNNN.json.gz, prêts pour charger_bigquery.py.
"""
import argparse
import math
import random
import time as chrono
from datetime import datetime, timedelta

import config as C
from outils import Calendrier, Ecrivain, Verite, fts
from referentiels import Referentiels
from simulation import Simulation


def temperatures(ecrivain, verite):
    """Relevés des sondes toutes les 15 minutes, avec une excursion en
    chambre froide et deux coupures d'enregistrement (vérité terrain)."""
    R = random.Random(C.GRAINE + 3)
    charge = fts(C.CHARGE_LE)
    exc = C.EXCURSION_TEMPERATURE
    exc_fin = exc["debut"] + timedelta(minutes=exc["duree_min"])
    verite.ajouter("EXCURSION_TEMPERATURE", "releves_temperature", f"{exc['capteur']}|{fts(exc['debut'])}",
                   {"duree_min": exc["duree_min"], "pic_c": exc["pic"], "zone_id": "Z04"})
    for cap, debut, duree in C.TROUS_CAPTEUR:
        verite.ajouter("RELEVES_TEMPERATURE_MANQUANTS", "releves_temperature", f"{cap}|{fts(debut)}",
                       {"duree_min": duree})
    for cap, zone, moy, sd in C.CAPTEURS:
        bruit = 0.0
        t = datetime.combine(C.DEBUT, datetime.min.time())
        trous = [(d, d + timedelta(minutes=m)) for c, d, m in C.TROUS_CAPTEUR if c == cap]
        while t <= C.EXTRACTION:
            if not any(a <= t < b for a, b in trous):
                bruit = 0.85 * bruit + R.gauss(0, sd * 0.5)
                jour = 0.3 * math.sin((t.hour + t.minute / 60 - 9) / 24 * 2 * math.pi)
                saison = 0 if zone == "Z04" else 1.5 * math.sin((t.timetuple().tm_yday - 110) / 365 * 2 * math.pi)
                v = moy + jour + saison + bruit
                if cap == exc["capteur"] and exc["debut"] <= t < exc_fin:
                    x = (t - exc["debut"]) / (exc_fin - exc["debut"])
                    v = max(v, 8.0 + (exc["pic"] - 8.0) * math.sin(x * math.pi) + 0.1)
                ecrivain.ecrire("sc_raw.releves_temperature", {
                    "capteur_id": cap, "zone_id": zone, "horodatage": fts(t),
                    "temperature_c": round(v, 2), "_charge_le": charge})
            t += timedelta(minutes=C.PAS_RELEVE_MIN)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sortie", default="donnees")
    p.add_argument("--echelle", type=float, default=1.0, help="multiplie le volume de commandes")
    args = p.parse_args()

    debut = chrono.time()
    e = Ecrivain(args.sortie)
    v = Verite(e, C.CHARGE_LE)
    cal = Calendrier()
    ref = Referentiels(random.Random(C.GRAINE), cal, e)
    print(f"Référentiels : {len(ref.articles)} articles, {len(ref.destinataires)} destinataires, "
          f"{len(ref.operateurs)} opérateurs")
    sim = Simulation(ref, cal, e, v, args.echelle)
    sim.executer()
    temperatures(e, v)
    e.fermer()

    print(f"\nTerminé en {chrono.time() - debut:.0f} s. Lignes écrites :")
    for t, n in sorted(e.compteurs.items()):
        print(f"  {t:<38} {n:>10,}".replace(",", " "))
    print("\nAnomalies et scénarios injectés (vérité terrain) :")
    for t, n in sorted(v.compteur_types.items()):
        print(f"  {t:<38} {n:>6}")
    print(f"\nArticle du scénario S3 : {sim.s3_article} ; lot rappelé (S4) : {sim.s4_lot}")


if __name__ == "__main__":
    main()
