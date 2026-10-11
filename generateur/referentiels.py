"""
Construction et écriture des référentiels (dimensions).
Les objets renvoyés sont aussi utilisés par la simulation.
"""
import random
from datetime import date, timedelta

import config as C
from outils import fts, fd

VILLES = [  # ville, code département, région
    ("Orléans", "45", "Centre-Val de Loire"), ("Tours", "37", "Centre-Val de Loire"),
    ("Blois", "41", "Centre-Val de Loire"), ("Bourges", "18", "Centre-Val de Loire"),
    ("Paris", "75", "Ile-de-France"), ("Versailles", "78", "Ile-de-France"),
    ("Créteil", "94", "Ile-de-France"), ("Nantes", "44", "Pays de la Loire"),
    ("Angers", "49", "Pays de la Loire"), ("Le Mans", "72", "Pays de la Loire"),
    ("Rennes", "35", "Bretagne"), ("Brest", "29", "Bretagne"), ("Caen", "14", "Normandie"),
    ("Rouen", "76", "Normandie"), ("Lille", "59", "Hauts-de-France"),
    ("Amiens", "80", "Hauts-de-France"), ("Reims", "51", "Grand Est"),
    ("Strasbourg", "67", "Grand Est"), ("Dijon", "21", "Bourgogne-Franche-Comté"),
    ("Besançon", "25", "Bourgogne-Franche-Comté"), ("Bordeaux", "33", "Nouvelle-Aquitaine"),
    ("Poitiers", "86", "Nouvelle-Aquitaine"), ("Limoges", "87", "Nouvelle-Aquitaine"),
    ("Lyon", "69", "Auvergne-Rhône-Alpes"), ("Clermont-Ferrand", "63", "Auvergne-Rhône-Alpes"),
    ("Grenoble", "38", "Auvergne-Rhône-Alpes"), ("Toulouse", "31", "Occitanie"),
    ("Montpellier", "34", "Occitanie"), ("Marseille", "13", "Provence-Alpes-Côte d'Azur"),
    ("Nice", "06", "Provence-Alpes-Côte d'Azur"),
]
FAMILLES_AMB = ["Antalgiques", "Antibiotiques", "Cardiologie", "Dermatologie",
                "Gastro-entérologie", "Pneumologie", "Neurologie", "Endocrinologie"]
FAMILLES_FROID = ["Vaccins", "Insulines", "Biothérapies"]
FORMES = ["cp B30", "cp B90", "gél B28", "sol buv 150 ml", "pdre sach B20", "crème 30 g"]
FORMES_FROID = ["ser PE 1 ml", "stylo 3 ml", "flacon 10 ml", "ser PE 0,5 ml"]


class Referentiels:
    def __init__(self, rng: random.Random, cal, ecrivain):
        self.R, self.cal, self.e = rng, cal, ecrivain
        self.charge_le = fts(C.CHARGE_LE)
        self._zones()
        self._donneurs()
        self._articles()
        self._destinataires()
        self._transporteurs()
        self._operateurs()
        self._calendrier()
        self._standards()

    # ------------------------------------------------------------------
    def _zones(self):
        s = C.SITE
        self.e.ecrire("sc_raw.dim_site", {**s, "actif": True, "_charge_le": self.charge_le})
        self.zones, self.emplacements, self.emp_zone = {}, {}, {}
        for zid, code, lib, typ, tmin, tmax, nb, pref in C.ZONES:
            self.zones[zid] = dict(type=typ, tmin=tmin, tmax=tmax)
            self.e.ecrire("sc_raw.dim_zone", {
                "zone_id": zid, "site_id": s["site_id"], "code_zone": code, "libelle": lib,
                "type_zone": typ, "temp_min_c": tmin, "temp_max_c": tmax, "actif": True,
                "_charge_le": self.charge_le})
            emps = []
            for i in range(nb):
                allee, pos, niv = i // 60 + 1, (i % 60) // 4 + 1, i % 4 + 1
                eid = f"EMP-{zid[1:]}{i + 1:04d}"
                emps.append(eid)
                self.emp_zone[eid] = zid
                self.e.ecrire("sc_raw.dim_emplacement", {
                    "emplacement_id": eid, "zone_id": zid,
                    "code_emplacement": f"{pref}-{allee:02d}-{pos:02d}-{niv}",
                    "allee": f"{pref}{allee:02d}", "niveau": niv,
                    "type_emplacement": "RESERVE" if typ == "STOCKAGE" else "PICKING",
                    "actif": True, "_charge_le": self.charge_le})
            self.emplacements[zid] = emps

    def _donneurs(self):
        self.donneurs = {}
        for did, nom, otif, prec, cutoff, part, part_froid, nb_art, delai in C.DONNEURS:
            self.donneurs[did] = dict(nom=nom, cutoff=cutoff, part=part, part_froid=part_froid,
                                      nb_art=nb_art, delai=delai, prefixe=nom.split()[-1][:2].upper())
            self.e.ecrire("sc_raw.dim_donneur_ordre", {
                "donneur_ordre_id": did, "nom": nom, "objectif_otif_pct": otif,
                "objectif_precision_pct": prec, "heure_limite_commande": cutoff.strftime("%H:%M:%S"),
                "date_debut_contrat": fd(date(2021 + len(self.donneurs), 1, 1)),
                "_charge_le": self.charge_le})

    def _articles(self):
        R = self.R
        self.articles = {}
        self.catalogue = {}        # (donneur, "FROID"/"AMBIANT") -> (ids, poids)
        libres = {z: list(self.emplacements[z]) for z in ("Z02", "Z04", "Z05")}
        n = 0
        for did, d in self.donneurs.items():
            ids, poids = [], []
            for k in range(d["nb_art"]):
                n += 1
                aid = f"ART-{n:05d}"
                if R.random() < d["part_froid"]:
                    classe_t, zone = "FROID", "Z04"
                    fam, tmin, tmax = R.choice(FAMILLES_FROID), 2, 8
                    duree = R.choice([12, 18, 24])
                    forme = R.choice(FORMES_FROID)
                elif R.random() < 0.05:
                    classe_t, zone, fam, tmin, tmax, duree = "AMBIANT", "Z05", "Stupéfiants", 15, 25, 36
                    forme = R.choice(FORMES[:3])
                else:
                    classe_t, zone = "AMBIANT", "Z02"
                    fam = R.choice(FAMILLES_AMB)
                    tmin, tmax = (2, 25) if R.random() < 0.3 else (15, 25)
                    duree = R.choice([24, 36, 36, 48])
                    forme = R.choice(FORMES)
                emp = libres[zone].pop(R.randrange(len(libres[zone])))
                dosage = R.choice([5, 10, 20, 25, 50, 100, 200, 250, 500, 1000])
                self.articles[aid] = dict(
                    id=aid, do=did, classe_t=classe_t, zone=zone, emp=emp, famille=fam,
                    tmin=tmin, tmax=tmax, duree=duree,
                    colisage=R.choice([10, 12, 20, 24, 30, 40, 50, 60]),
                    libelle=f"{d['prefixe']}-{fam[:5]} {dosage} mg {forme}")
                ids.append(aid)
            # popularité en loi de Zipf, rangs mélangés
            rangs = list(range(1, len(ids) + 1))
            R.shuffle(rangs)
            for aid, r in zip(ids, rangs):
                self.articles[aid]["poids"] = 1.0 / (r ** 0.9)
            # classes ABC par cumul de poids
            tri = sorted(ids, key=lambda a: -self.articles[a]["poids"])
            total = sum(self.articles[a]["poids"] for a in ids)
            cumul = 0.0
            for a in tri:
                cumul += self.articles[a]["poids"] / total
                self.articles[a]["abc"] = "A" if cumul <= 0.80 else ("B" if cumul <= 0.95 else "C")
            for ct in ("FROID", "AMBIANT"):
                sel = [a for a in ids if self.articles[a]["classe_t"] == ct]
                self.catalogue[(did, ct)] = (sel, [self.articles[a]["poids"] for a in sel])

        for a in self.articles.values():
            self.e.ecrire("sc_raw.dim_article", {
                "article_id": a["id"], "sku": f"{self.donneurs[a['do']]['prefixe']}{a['id'][4:]}",
                "libelle": a["libelle"], "donneur_ordre_id": a["do"], "famille": a["famille"],
                "classe_abc": a["abc"], "temp_min_c": a["tmin"], "temp_max_c": a["tmax"],
                "gestion_lot": True, "duree_vie_mois": a["duree"], "unite_vente": "BOITE",
                "colisage": a["colisage"],
                "poids_kg": round(R.uniform(0.02, 0.4), 3), "volume_dm3": round(R.uniform(0.05, 1.0), 3),
                "statut_article": "ACTIF",
                "date_creation": fd(date(2015, 1, 1) + timedelta(days=R.randrange(3600))),
                "_charge_le": self.charge_le})

    def _destinataires(self):
        R = self.R
        types = list(C.DESTINATAIRES)
        parts = [C.DESTINATAIRES[t]["part"] for t in types]
        noms = {"OFFICINE": "Pharmacie", "CLINIQUE": "Clinique", "HOPITAL": "PUI Centre hospitalier",
                "GROSSISTE": "Répartiteur"}
        suffixes = ["du Centre", "de la Gare", "des Halles", "du Marché", "Saint-Jean", "de la Mairie",
                    "du Parc", "des Tilleuls", "Pasteur", "Victor Hugo", "de l'Europe", "du Lac"]
        self.destinataires, self.dest_ids, self.dest_poids = {}, [], []
        for i in range(1, C.NB_DESTINATAIRES + 1):
            t = R.choices(types, parts)[0]
            ville, dep, region = R.choice(VILLES)
            did = f"DST{i:05d}"
            pref = R.choices(["T02", "T04"], [0.6, 0.4])[0]
            self.destinataires[did] = dict(type=t, pref=pref, gros=C.DESTINATAIRES[t]["gros"])
            self.dest_ids.append(did)
            self.dest_poids.append(C.DESTINATAIRES[t]["poids"] * R.uniform(0.3, 1.7))
            self.e.ecrire("sc_raw.dim_destinataire", {
                "destinataire_id": did, "nom": f"{noms[t]} {R.choice(suffixes)} {ville}",
                "type_destinataire": t, "autorisation_gros": C.DESTINATAIRES[t]["gros"],
                "ville": ville, "code_postal": f"{dep}{R.randrange(0, 1000):03d}", "region": region,
                "_charge_le": self.charge_le})

    def _transporteurs(self):
        self.transporteurs = {}
        for tid, nom, mode, creneaux, retard in C.TRANSPORTEURS:
            self.transporteurs[tid] = dict(nom=nom, mode=mode, creneaux=creneaux, retard=retard)
            self.e.ecrire("sc_raw.dim_transporteur", {
                "transporteur_id": tid, "nom": nom, "mode": mode, "actif": True,
                "_charge_le": self.charge_le})

    def _operateurs(self):
        R = self.R
        self.operateurs = {}
        n = 0

        def ajouter(equipe, contrat, arrivee, depart, controleur=False, s1=False, vitesse=None):
            nonlocal n
            n += 1
            oid = f"OP{n:03d}"
            if vitesse is None:
                vitesse = R.gauss(1.02, 0.07) if contrat != "INTERIM" else R.gauss(0.92, 0.08)
            self.operateurs[oid] = dict(equipe=equipe, contrat=contrat, arrivee=arrivee, depart=depart,
                                        controleur=controleur, s1=s1, vitesse=max(0.6, vitesse))

        for eq in C.EQUIPES:
            for k in range(C.PERMANENTS_PAR_EQUIPE):
                contrat = "CDI" if (k < C.CONTROLEURS_PAR_EQUIPE or R.random() < 0.85) else "CDD"
                arr = date(2016, 1, 1) + timedelta(days=R.randrange(3400))
                ajouter(eq, contrat, arr, None, controleur=k < C.CONTROLEURS_PAR_EQUIPE)
            for _ in range(C.POSTES_INTERIM_PAR_EQUIPE):
                d = C.DEBUT - timedelta(days=R.randrange(10, 60))
                while d <= C.FIN:
                    fin = d + timedelta(days=R.randrange(40, 120))
                    ajouter(eq, "INTERIM", d, fin if fin <= C.FIN else None)
                    d = fin + timedelta(days=R.randrange(1, 8))
        s1 = C.SCENARIOS["S1"]
        for _ in range(s1["nb_interimaires"]):
            ajouter(s1["equipe"], "INTERIM", s1["debut"], date(2026, 5, 29), s1=True,
                    vitesse=R.gauss(0.92, 0.06) * s1["vitesse"])

        for oid, o in self.operateurs.items():
            actif = o["depart"] is None or o["depart"] >= C.FIN
            self.e.ecrire("sc_raw.dim_operateur", {
                "operateur_id": oid, "site_id": C.SITE["site_id"], "equipe": o["equipe"],
                "type_contrat": o["contrat"], "date_arrivee": fd(o["arrivee"]),
                "date_depart": fd(o["depart"]), "actif": actif, "_charge_le": self.charge_le})

    def _calendrier(self):
        d = date(2025, 1, 1)
        while d <= date(2027, 12, 31):
            iso = d.isocalendar()
            self.e.ecrire("sc_raw.dim_calendrier", {
                "date_jour": fd(d), "annee": d.year, "mois": d.month, "semaine_iso": iso[1],
                "jour_semaine": iso[2], "jour_ouvre": self.cal.ouvre(d),
                "ferie": d in self.cal.feries, "libelle_ferie": self.cal.feries.get(d)})
            d += timedelta(days=1)

    def _standards(self):
        self.standards = {}
        for i, (act, tz, uo, cad) in enumerate(C.STANDARDS, 1):
            self.standards[(act, tz)] = cad
            self.e.ecrire("sc_raw.standards_activite", {
                "standard_id": f"STD-{i:02d}", "site_id": C.SITE["site_id"], "activite": act,
                "type_zone": tz, "unite_oeuvre": uo, "cadence_std_par_heure": cad,
                "valide_depuis": "2025-01-01", "valide_jusqu": None, "_charge_le": self.charge_le})
