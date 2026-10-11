"""
Charge les fichiers produits par generer.py dans BigQuery.

    python charger_bigquery.py --projet MON-PROJET --dossier donnees

Choix techniques :
* Jobs de chargement (load jobs) : ils sont GRATUITS dans BigQuery,
  contrairement aux insertions en streaming ou aux INSERT SQL.
* Le schéma est relu sur la table existante : les types et descriptions
  définis dans les scripts SQL restent la référence.
* Chaque table est vidée (TRUNCATE) puis rechargée : relancer le script
  remplace proprement les données au lieu de les dupliquer.
"""
import argparse
import glob
import os
from collections import defaultdict

from google.cloud import bigquery


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--projet", required=True)
    p.add_argument("--dossier", default="donnees")
    p.add_argument("--tables", nargs="*", help="limiter à certaines tables, ex : sc_raw.commandes")
    args = p.parse_args()

    client = bigquery.Client(project=args.projet, location="EU")
    fichiers = defaultdict(list)
    for f in sorted(glob.glob(os.path.join(args.dossier, "*.json.gz"))):
        nom = os.path.basename(f).split(".part")[0]          # ex : sc_raw.commandes
        fichiers[nom].append(f)
    if args.tables:
        fichiers = {k: v for k, v in fichiers.items() if k in args.tables}
    if not fichiers:
        raise SystemExit(f"Aucun fichier trouvé dans {args.dossier}")

    for nom, parts in sorted(fichiers.items()):
        table_id = f"{args.projet}.{nom}"
        table = client.get_table(table_id)                    # erreur claire si la table n'existe pas
        client.query(f"TRUNCATE TABLE `{table_id}`").result()
        for f in parts:
            config = bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                schema=table.schema,
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            )
            with open(f, "rb") as fh:
                job = client.load_table_from_file(fh, table_id, job_config=config)
            job.result()
        n = client.get_table(table_id).num_rows
        print(f"{nom:<38} {n:>10,} lignes".replace(",", " "))
    print("\nChargement terminé.")


if __name__ == "__main__":
    main()
