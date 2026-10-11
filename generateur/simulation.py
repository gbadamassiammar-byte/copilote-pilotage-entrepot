"""
Moteur de simulation à événements discrets.

Principe : une file d'événements horodatés (heapq) est traitée dans l'ordre
chronologique. Chaque événement modifie l'état du stock et écrit les lignes
correspondantes. Le traitement chronologique garantit que la photo de stock
de fin de journée est toujours égale à la photo de la veille plus les
mouvements de la journée, SAUF là où une anomalie a été injectée
volontairement (mouvement perdu, doublon). C'est ce qui rend le contrôle
de réconciliation de l'outil de fiabilisation significatif.

Convention des mouvements de stock : chaque ligne modifie un "bac" de stock
(emplacement, lot, statut qualité). Le statut du bac est statut_qualite_cible
si la quantité est positive, statut_qualite_origine si elle est négative.
Un changement de statut s'écrit donc en deux lignes (-q puis +q).
"""
import bisect
import heapq
import math
import random
from datetime import date, datetime, time, timedelta

import config as C
from outils import fts, fd

J = timedelta(days=1)


def dt(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m)


class Simulation:
    def __init__(self, ref, cal, ecrivain, verite, echelle=1.0):
        self.ref, self.cal, self.e, self.v = ref, cal, ecrivain, verite
        self.R = random.Random(C.GRAINE + 1)
        self.echelle = echelle
        self.A = C.ANOMALIES
        self.charge_le = fts(C.CHARGE_LE)
        self.site = C.SITE["site_id"]

        self.file = []
        self.seq = 0
        self.stock = {}                       # (emp, lot, statut) -> [qte, reservee]
        self.bacs_article = {}                # article -> set(clés)
        self.lots = {}
        self.peremptions = {}                 # date -> [lots]
        self.en_cours_appro = {}              # article -> quantité commandée non reçue
        self.receptions_attente = {}
        self.retours_attente = {}
        self.commandes_attente = {}
        self.expeditions_lot = {}             # lot -> [(ligne, commande, dest, qte, livraison)]
        self.compteurs = {}
        self.taches_par_poste = {}            # (jour_prod, equipe, op) -> secondes
        self.controles_par_poste = {}
        self.receptions_par_jour = {}
        self.demande_jour = {}
        self.s3_article = self.s4_lot = None
        self.s3_lots_bloques = set()
        self.s4_heure_rappel = self.s4_heure_blocage = None
        self.lots_zone_ko = 0

        self._fenetres()
        self._creneaux()
        self._presences = {}

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------
    def nid(self, prefixe, largeur):
        n = self.compteurs.get(prefixe, 0) + 1
        self.compteurs[prefixe] = n
        return f"{prefixe}{n:0{largeur}d}"

    def pousser(self, t, type_evt, charge=None):
        self.seq += 1
        heapq.heappush(self.file, (t, self.seq, type_evt, charge))

    def _fenetres(self):
        """Fenêtres de travail 3x8 : la nuit de 21h (veille) à 5h appartient
        au jour de production suivant."""
        f = []
        d = C.DEBUT - J
        while d <= C.FIN + 10 * J:
            if self.cal.ouvre(d):
                f.append((dt(d - J, 21), dt(d, 5), d, "NUIT"))
                f.append((dt(d, 5), dt(d, 13), d, "MATIN"))
                f.append((dt(d, 13), dt(d, 21), d, "APRES_MIDI"))
            d += J
        self.fen = f
        self.fen_debuts = [x[0] for x in f]

    def fenetre(self, t):
        """Renvoie (instant de travail effectif >= t, jour_prod, equipe, fin_fenetre)."""
        i = bisect.bisect_right(self.fen_debuts, t) - 1
        if i >= 0 and self.fen[i][0] <= t < self.fen[i][1]:
            deb, fin, jp, eq = self.fen[i]
            return t, jp, eq, fin, False
        deb, fin, jp, eq = self.fen[i + 1]
        return deb, jp, eq, fin, True

    def presents(self, jour, equipe):
        cle = (jour, equipe)
        if cle not in self._presences:
            pk, ct = [], []
            for oid, o in self.ref.operateurs.items():
                if o["equipe"] != equipe or o["arrivee"] > jour:
                    continue
                if o["depart"] is not None and o["depart"] < jour:
                    continue
                if self.R.random() < C.TAUX_ABSENCE:
                    continue
                (ct if o["controleur"] else pk).append(oid)
            if not pk:
                pk = [o for o, x in self.ref.operateurs.items() if x["equipe"] == equipe][:3]
            if not ct:
                ct = pk[:1]
            self._presences[cle] = (pk, ct)
        return self._presences[cle]

    def _creneaux(self):
        """Tous les créneaux d'enlèvement sont tirés à l'avance (reproductible)."""
        R, s2 = self.R, C.SCENARIOS["S2"]
        self.creneaux = {}
        self.creneaux_jour = {}
        d = C.DEBUT
        while d <= C.FIN + 10 * J:
            if self.cal.ouvre(d):
                liste = []
                for tid, t in self.ref.transporteurs.items():
                    for h in t["creneaux"]:
                        prevu = datetime.combine(d, h)
                        if tid == s2["transporteur"] and h == s2["creneau"] and s2["debut"] <= d <= s2["fin"] \
                                and R.random() < s2["proba_retard"]:
                            retard = R.uniform(60, 150)
                        elif R.random() < 0.04:
                            retard = R.uniform(30, 90)
                        else:
                            retard = min(25, max(-10, R.gauss(2, 6)))
                        arrivee = prevu + timedelta(minutes=retard)
                        cid = f"CR-{tid}-{d:%Y%m%d}-{h:%H%M}"
                        c = dict(id=cid, tid=tid, prevu=prevu, arrivee=arrivee,
                                 depart=arrivee + timedelta(minutes=R.uniform(20, 45)),
                                 retard=retard, colis=0)
                        self.creneaux[cid] = c
                        liste.append(c)
                self.creneaux_jour[d] = liste
            d += J

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------
    def bac(self, cle):
        b = self.stock.get(cle)
        if b is None:
            b = self.stock[cle] = [0, 0]
            self.bacs_article.setdefault(self.lots[cle[1]]["article"], set()).add(cle)
        return b

    def mouvement(self, t, emp, lot, type_mvt, code, st_orig, st_cible, q, ref=None, op=None,
                  appliquer=True):
        """Ecrit un mouvement et met à jour le bac concerné."""
        statut_bac = st_cible if q > 0 else st_orig
        if appliquer:
            self.bac((emp, lot, statut_bac))[0] += q
        mid = self.nid("MV", 10)
        ligne = {
            "mouvement_id": mid, "site_id": self.site, "emplacement_id": emp,
            "article_id": self.lots[lot]["article"], "lot_id": lot, "type_mouvement": type_mvt,
            "code_mvt_sap": code, "statut_qualite_origine": st_orig, "statut_qualite_cible": st_cible,
            "quantite": q, "horodatage": fts(t), "ref_document": ref, "operateur_id": op,
            "_charge_le": self.charge_le}
        if type_mvt == "SORTIE_LIVRAISON" and self.R.random() < self.A["mouvement_perdu"]:
            self.v.ajouter("ECART_STOCK_MOUVEMENT_PERDU", "mouvements_stock", f"{lot}|{t.date()}",
                           {"mouvement_id": mid, "emplacement_id": emp, "quantite": q})
            return mid
        self.e.ecrire("sc_raw.mouvements_stock", ligne)
        if self.R.random() < self.A["doublon_mouvement"]:
            self.e.ecrire("sc_raw.mouvements_stock", {**ligne, "_charge_le": fts(C.CHARGE_LE + timedelta(minutes=7))})
            self.v.ajouter("DOUBLON_TECHNIQUE", "mouvements_stock", mid)
        return mid

    def changer_statut(self, t, lot, st_orig, st_cible, code, ref=None):
        """Bascule tout le stock non réservé d'un lot d'un statut à l'autre."""
        art = self.lots[lot]["article"]
        for cle in list(self.bacs_article.get(art, ())):
            if cle[1] != lot or cle[2] != st_orig:
                continue
            q = self.stock[cle][0] - self.stock[cle][1]
            if q <= 0:
                continue
            self.mouvement(t, cle[0], lot, "CHANGEMENT_STATUT", code, st_orig, st_cible, -q, ref)
            self.mouvement(t, cle[0], lot, "CHANGEMENT_STATUT", code, st_orig, st_cible, q, ref)
        self.lots[lot]["statut"] = st_cible
        self.lots[lot]["maj"] = t
        self.lots[lot]["motif"] = ref

    def sortir_lot(self, t, lot, statut, type_mvt, code):
        art = self.lots[lot]["article"]
        for cle in list(self.bacs_article.get(art, ())):
            if cle[1] == lot and cle[2] == statut and self.stock[cle][0] > 0:
                self.mouvement(t, cle[0], lot, type_mvt, code, statut, None, -self.stock[cle][0])

    def creer_lot(self, art, fabrication, emp=None, peremption=None):
        a = self.ref.articles[art]
        lid = self.nid("LOT", 6)
        n = self.compteurs.get(("num", a["do"]), 0) + 1
        self.compteurs[("num", a["do"])] = n
        prefixe = self.ref.donneurs[a["do"]]["prefixe"]
        peremption = peremption or fabrication + timedelta(days=int(a["duree"] * 30.4))
        self.lots[lid] = dict(article=art, num=f"{prefixe}{fabrication:%y%m}{n:04d}", fab=fabrication,
                              peremption=peremption, statut="CONTROLE_QUALITE", rappele=False,
                              date_rappel=None, emp=emp or a["emp"], maj=None)
        self.peremptions.setdefault(peremption, []).append(lid)
        return lid

    # ------------------------------------------------------------------
    # Génération des commandes d'une journée
    # ------------------------------------------------------------------
    def generer_commandes(self, jour, R):
        f = C.FACTEUR_JOUR[jour.weekday()] * C.FACTEUR_MOIS[jour.month]
        f *= 1 + C.TENDANCE_ANNUELLE * (jour - C.DEBUT).days / 365
        if jour in self.cal.feries:
            f *= 0.1
        n = max(0, int(round(C.COMMANDES_JOUR_BASE * self.echelle * f * R.gauss(1, 0.06))))
        ouvre = self.cal.ouvre(jour)
        if ouvre:
            poids_h = [.3, .3, .3, .3, .3, .5, 6, 4, 6, 9, 10, 9, 6, 8, 8, 7, 6, 5, 3, 2, 1.5, 1, .6, .4]
        else:
            poids_h = [.2] * 8 + [2] * 10 + [.5] * 6
        dos = list(self.ref.donneurs)
        parts = [self.ref.donneurs[d]["part"] for d in dos]
        res = []
        for _ in range(n):
            h = R.choices(range(24), poids_h)[0]
            cree = dt(jour, h) + timedelta(seconds=R.randrange(3600))
            do = R.choices(dos, parts)[0]
            froid = R.random() < self.ref.donneurs[do]["part_froid"]
            dest = R.choices(self.ref.dest_ids, self.ref.dest_poids)[0]
            dinfo = self.ref.destinataires[dest]
            p = C.DESTINATAIRES[dinfo["type"]]
            ids, poids = self.ref.catalogue[(do, "FROID" if froid else "AMBIANT")]
            if not ids:
                ids, poids = self.ref.catalogue[(do, "AMBIANT")]
                froid = False
            nb = min(R.randint(*p["lignes"]), len(ids))
            choisis = []
            while len(choisis) < nb:
                a = R.choices(ids, poids)[0]
                if a not in choisis:
                    choisis.append(a)
            lignes = []
            for a in choisis:
                if p["qte"] is None:
                    q = self.ref.articles[a]["colisage"] * R.randint(1, 4)
                else:
                    q = R.randint(*p["qte"])
                lignes.append((a, q))
            urgent = dinfo["type"] != "GROSSISTE" and R.random() < C.PART_URGENT
            res.append(dict(cree=cree, do=do, froid=froid, dest=dest, urgent=urgent, lignes=lignes,
                            annule=R.random() < C.PART_ANNULATION,
                            canal=R.choices(["EDI", "PORTAIL", "EMAIL"],
                                            [0.9, 0.08, 0.02] if dinfo["type"] in ("HOPITAL", "GROSSISTE")
                                            else [0.45, 0.42, 0.13])[0]))
        return res

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------
    def initialiser(self):
        R = self.R
        # estimation de la demande quotidienne par article (pré-passe sur 28 jours)
        Rp = random.Random(C.GRAINE + 7)
        dem = {a: 0.0 for a in self.ref.articles}
        for k in range(28):
            for c in self.generer_commandes(C.DEBUT + k * J, Rp):
                for a, q in c["lignes"]:
                    dem[a] += q
        self.taux = {a: max(0.3, v / 28) for a, v in dem.items()}

        t0 = datetime.combine(C.DEBUT, time(0, 0))
        for a, art in self.ref.articles.items():
            col = art["colisage"]
            for frac, age in ((0.4, R.randint(200, 330)), (0.6, R.randint(30, 150))):
                q = max(col, int(math.ceil(self.taux[a] * 35 * frac / col)) * col)
                fab = C.DEBUT - timedelta(days=age)
                per = fab + timedelta(days=int(art["duree"] * 30.4))
                if per < C.DEBUT + timedelta(days=120):
                    per = C.DEBUT + timedelta(days=R.randint(150, 300))
                lot = self.creer_lot(a, fab, peremption=per)
                self.lots[lot]["statut"] = "LIBRE"
                self.mouvement(t0, art["emp"], lot, "REPRISE_STOCK", "561", None, "LIBRE", q, "REPRISE-INITIALE")
            self.en_cours_appro[a] = 0

        d = C.DEBUT
        while d <= C.FIN:
            self.pousser(dt(d, 0), "JOUR", d)
            d += J
        s4 = C.SCENARIOS["S4"]
        self.pousser(dt(s4["date_rappel"], 9), "RAPPEL")
        s3 = C.SCENARIOS["S3"]
        self.pousser(dt(s3["debut"], 9), "S3_BLOCAGE")
        self.pousser(dt(s3["fin"], 10), "S3_LIBERATION")

    # ------------------------------------------------------------------
    # Boucle principale
    # ------------------------------------------------------------------
    def executer(self):
        self.initialiser()
        while self.file:
            t, _, typ, ch = heapq.heappop(self.file)
            if t > C.EXTRACTION:
                break
            getattr(self, "evt_" + typ)(t, ch)
        self.finaliser()

    # ---- événements journaliers -------------------------------------
    def evt_JOUR(self, t, jour):
        # mise à jour de l'estimation de demande (moyenne mobile exponentielle)
        hier = self.demande_jour.pop(jour - J, None)
        if hier is not None:
            for a in self.taux:
                self.taux[a] = 0.97 * self.taux[a] + 0.03 * hier.get(a, 0)
        self.demande_jour[jour] = {}
        for lot in self.peremptions.pop(jour, []):
            if self.lots[lot]["statut"] == "LIBRE":
                self.pousser(dt(jour, 0, 30), "PEREMPTION", lot)
        for c in self.generer_commandes(jour, self.R):
            self.nouvelle_commande(c)
        if self.cal.ouvre(jour):
            self.pousser(dt(jour, 6), "REAPPRO", jour)
            self.pousser(dt(jour, 14), "INVENTAIRE", jour)
        self.pousser(datetime.combine(jour, time(23, 59, 59, 999999)), "PHOTO", jour)

    def evt_PEREMPTION(self, t, lot):
        if self.lots[lot]["statut"] == "LIBRE":
            self.changer_statut(t, lot, "LIBRE", "BLOQUE", "344", "PEREMPTION")
            self.pousser(t + 14 * J, "DESTRUCTION", lot)

    def evt_DESTRUCTION(self, t, lot):
        self.sortir_lot(t, lot, "BLOQUE", "DESTRUCTION", "555")

    def evt_RETOUR_LABO(self, t, lot):
        self.sortir_lot(t, lot, "BLOQUE", "RETOUR_LABO", "122")

    def evt_PHOTO(self, t, jour):
        vides = [k for k, v in self.stock.items() if v[0] == 0 and v[1] == 0]
        for k in vides:
            del self.stock[k]
            self.bacs_article[self.lots[k[1]]["article"]].discard(k)
        for (emp, lot, st), (q, r) in self.stock.items():
            if q > 0 or r > 0:
                self.e.ecrire("sc_raw.stock_photo", {
                    "date_photo": fd(jour), "site_id": self.site, "emplacement_id": emp,
                    "article_id": self.lots[lot]["article"], "lot_id": lot, "statut_qualite": st,
                    "qte_physique": q, "qte_reservee": r, "_charge_le": self.charge_le})

    def evt_INVENTAIRE(self, t, jour):
        """Inventaire tournant : quelques emplacements comptés, écarts
        réels enregistrés par un ajustement (donc tracés)."""
        R = self.R
        cles = [k for k, v in self.stock.items() if k[2] == "LIBRE" and v[0] - v[1] > 3]
        for cle in R.sample(cles, min(15, len(cles))):
            if R.random() < 0.10:
                delta = R.choice([-3, -2, -1, -1, 1, 1, 2])
                self.mouvement(t + timedelta(minutes=R.randrange(120)), cle[0], cle[1],
                               "AJUSTEMENT_INVENTAIRE", "701" if delta > 0 else "702",
                               "LIBRE", "LIBRE", delta, f"INV-{jour:%Y%m%d}")

    # ---- approvisionnement -------------------------------------------
    def evt_REAPPRO(self, t, jour):
        R, s3 = self.R, C.SCENARIOS["S3"]
        dispo = {}
        for (emp, lot, st), (q, r) in self.stock.items():
            if st in ("LIBRE", "CONTROLE_QUALITE") and emp in self.ref.emp_zone and \
                    self.ref.emp_zone[emp] != "Z06":
                a = self.lots[lot]["article"]
                dispo[a] = dispo.get(a, 0) + q - r
        for a, art in self.ref.articles.items():
            delai = self.ref.donneurs[art["do"]]["delai"]
            position = dispo.get(a, 0) + self.en_cours_appro[a]
            pr = self.taux[a] * (delai * 1.4 + 4 + 8)   # délai fournisseur + contrôle qualité + sécurité
            if position >= pr:
                continue
            col = art["colisage"] * 5
            q = int(math.ceil((self.taux[a] * 28 + pr - position) / col)) * col
            prevue = self.cal.ouvre_suivant(jour, max(2, delai + R.randint(-2, 2)))
            reelle = prevue
            x = R.random()
            if x < 0.10:
                reelle = self.cal.ouvre_suivant(prevue, R.randint(1, 3))
            elif x < 0.15:
                reelle = self.cal.ouvre_suivant(jour, max(1, delai - 3))
            heure = dt(reelle, R.randint(7, 12), R.randrange(60))
            if a == self.s3_article and s3["debut"] <= reelle <= s3["fin"]:
                heure = dt(self.cal.ouvre_suivant(s3["fin"]), 8, R.randrange(60))
            rid = self.nid("RC", 7)
            rec = dict(id=rid, article=a, do=art["do"], qte=q, prevue=prevue,
                       heure_prevue=dt(prevue, heure.hour), arrivee=heure)
            self.receptions_attente[rid] = rec
            self.en_cours_appro[a] += q
            self.pousser(heure, "RECEPTION", rid)

    def evt_RECEPTION(self, t, rid):
        R = self.R
        rec = self.receptions_attente.pop(rid)
        a = rec["article"]
        art = self.ref.articles[a]
        self.en_cours_appro[a] -= rec["qte"]
        q = rec["qte"] if R.random() < 0.97 else int(rec["qte"] * R.uniform(0.95, 0.99))
        emp = None
        if art["classe_t"] == "FROID" and self.lots_zone_ko < self.A["lots_zone_incompatible"] \
                and t.date() > date(2025, 11, 1) and R.random() < 0.08:
            emp = R.choice(self.ref.emplacements["Z03"])
            self.lots_zone_ko += 1
        lot = self.creer_lot(a, t.date() - timedelta(days=R.randint(20, 90)), emp)
        if emp:
            self.v.ajouter("ZONE_TEMPERATURE_INCOMPATIBLE", "stock_photo", lot,
                           {"emplacement_id": emp, "zone": "Z03", "article_id": a, "plage_article": "2-8"})
        self.mouvement(t, self.lots[lot]["emp"], lot, "RECEPTION", "101", None, "CONTROLE_QUALITE", q,
                       rid, R.choice(self.presents(*self.fenetre(t)[1:3])[0]))
        self.e.ecrire("sc_raw.receptions", {
            "reception_id": rid, "site_id": self.site, "donneur_ordre_id": rec["do"], "article_id": a,
            "lot_id": lot, "qte_attendue": rec["qte"], "qte_recue": q, "date_prevue": fd(rec["prevue"]),
            "date_reelle": fts(t), "statut_qualite_initial": "CONTROLE_QUALITE",
            "statut_reception": "CLOTUREE", "_charge_le": self.charge_le})
        self.e.ecrire("sc_raw.creneaux", {
            "creneau_id": f"CRI-{rid}", "site_id": self.site, "quai": f"R{R.randint(1, 4)}",
            "transporteur_id": R.choice(["T02", "T05"]), "type_flux": "LIVRAISON_ENTRANTE",
            "debut_prevu": fts(rec["heure_prevue"]), "fin_prevue": fts(rec["heure_prevue"] + timedelta(hours=1)),
            "arrivee_reelle": fts(t), "depart_reel": fts(t + timedelta(minutes=R.randint(30, 75))),
            "nb_colis_prevus": int(math.ceil(rec["qte"] / art["colisage"])),
            "nb_colis_charges": int(math.ceil(q / art["colisage"])), "_charge_le": self.charge_le})
        jp = t.date()
        self.receptions_par_jour[jp] = self.receptions_par_jour.get(jp, 0) + 1
        self.pousser(dt(self.cal.ouvre_suivant(t.date(), R.randint(1, 3)), 10), "LIBERATION", lot)

    def evt_LIBERATION(self, t, lot):
        s3 = C.SCENARIOS["S3"]
        if self.lots[lot]["article"] == self.s3_article and s3["debut"] <= t.date() < s3["fin"]:
            self.changer_statut(t, lot, "CONTROLE_QUALITE", "BLOQUE", "350", "S3")
            self.s3_lots_bloques.add(lot)
        elif self.R.random() < 0.02:
            self.changer_statut(t, lot, "CONTROLE_QUALITE", "BLOQUE", "350", "REFUS-QUALITE")
            self.pousser(t + 10 * J, "DESTRUCTION", lot)
        else:
            self.changer_statut(t, lot, "CONTROLE_QUALITE", "LIBRE", "321", "LIBERATION")

    # ---- scénarios ----------------------------------------------------
    def _article_top(self, donneur, zone="Z02"):
        arts = [a for a, x in self.ref.articles.items() if x["do"] == donneur and x["zone"] == zone]
        return max(arts, key=lambda a: self.ref.articles[a]["poids"])

    def evt_S3_BLOCAGE(self, t, _):
        s3 = C.SCENARIOS["S3"]
        a = self.s3_article = self._article_top(s3["donneur"])
        for cle in list(self.bacs_article.get(a, ())):
            if cle[2] == "LIBRE" and self.stock[cle][0] - self.stock[cle][1] > 0:
                self.changer_statut(t, cle[1], "LIBRE", "BLOQUE", "344", "S3")
                self.s3_lots_bloques.add(cle[1])
        # réceptions déjà planifiées pendant la période : décalées après la fin
        for i, (te, s, typ, ch) in enumerate(self.file):
            if typ == "RECEPTION" and self.receptions_attente[ch]["article"] == a and \
                    s3["debut"] <= te.date() <= s3["fin"]:
                nouv = dt(self.cal.ouvre_suivant(s3["fin"]), 8, 30)
                self.file[i] = (nouv, s, typ, ch)
                self.receptions_attente[ch]["arrivee"] = nouv
        heapq.heapify(self.file)

    def evt_S3_LIBERATION(self, t, _):
        for lot in self.s3_lots_bloques:
            if self.lots[lot]["peremption"] > t.date():
                self.changer_statut(t, lot, "BLOQUE", "LIBRE", "343", "S3-LEVEE")

    def evt_RAPPEL(self, t, _):
        s4 = C.SCENARIOS["S4"]
        # lot rappelé : parmi les 10 articles les plus demandés du donneur, le lot que le FEFO
        # sert en premier et qui a le plus de stock (donc encore expédié pendant le retard de blocage)
        arts = sorted((x for x, v in self.ref.articles.items() if v["do"] == s4["donneur"] and v["zone"] == "Z02"),
                      key=lambda x: -self.ref.articles[x]["poids"])[:10]
        limite = t.date() + timedelta(days=C.DUREE_VIE_MIN_EXPEDITION_JOURS)
        tetes = []
        for x in arts:
            cands = [c for c in self.bacs_article.get(x, ()) if c[2] == "LIBRE" and self.stock[c][0] > 0
                     and self.lots[c[1]]["peremption"] > limite and self.ref.emp_zone.get(c[0]) != "Z06"]
            if cands:
                tetes.append(min(cands, key=lambda c: (self.lots[c[1]]["peremption"], c[1])))
        cle = max(tetes, key=lambda c: self.stock[c][0])
        a = self.lots[cle[1]]["article"]
        lot = self.s4_lot = cle[1]
        self.lots[lot].update(rappele=True, date_rappel=t.date(), maj=t)
        self.s4_heure_rappel = t
        self.s4_heure_blocage = dt(t.date() + s4["delai_blocage_jours"] * J, 10)
        self.pousser(self.s4_heure_blocage, "BLOCAGE_RAPPEL", lot)
        # retours de rappel : les destinataires renvoient une partie des boîtes livrées
        R = self.R
        for ligne, cmd, dest, q, livr in self.expeditions_lot.get(lot, []):
            if livr is not None and livr >= t - 120 * J and R.random() < 0.7:
                arr = dt(self.cal.ouvre_ou_suivant(t.date() + R.randint(3, 15) * J), R.randint(9, 15))
                self.planifier_retour(arr, dict(ligne=ligne, cmd=cmd, dest=dest, art=a, lot=lot, q=q,
                                                exped=livr, motif="RAPPEL"))

    def evt_BLOCAGE_RAPPEL(self, t, lot):
        self.changer_statut(t, lot, "LIBRE", "BLOQUE", "344", "RAPPEL")
        self.pousser(t + 7 * J, "RETOUR_LABO", lot)
        # boîtes expédiées APRES le rappel (pendant le retard de blocage) : rappel ciblé
        R, a = self.R, self.lots[lot]["article"]
        for ligne, cmd, dest, q, livr in self.expeditions_lot.get(lot, []):
            if livr is not None and livr >= self.s4_heure_rappel and R.random() < 0.9:
                arr = dt(self.cal.ouvre_ou_suivant(max(livr, t).date() + R.randint(3, 10) * J), R.randint(9, 15))
                self.planifier_retour(arr, dict(ligne=ligne, cmd=cmd, dest=dest, art=a, lot=lot, q=q,
                                                exped=livr, motif="RAPPEL"))

    # ---- commandes ------------------------------------------------------
    def nouvelle_commande(self, c):
        R = self.R
        cid = self.nid("CMD", 8)
        c["id"] = cid
        dinfo = self.ref.destinataires[c["dest"]]
        if c["froid"]:
            c["tid"] = "T03"
        elif c["urgent"]:
            c["tid"] = "T01"
        elif dinfo["type"] in ("HOPITAL", "GROSSISTE"):
            c["tid"] = "T05"
        else:
            c["tid"] = dinfo["pref"]
        cutoff = self.ref.donneurs[c["do"]]["cutoff"]
        j = c["cree"].date()
        avant_cutoff = self.cal.ouvre(j) and c["cree"].time() < cutoff
        c["promise"] = self.cal.ouvre_suivant(j, 1 if avant_cutoff else 2)
        c["demandee"] = c["promise"]
        if R.random() < 0.08:
            c["demandee"] = max(j + J, c["promise"] - J)
        c["confirmee"] = c["cree"] + timedelta(minutes=R.uniform(1, 8) if c["canal"] == "EDI" else R.uniform(10, 60))
        for a, q in c["lignes"]:
            dj = self.demande_jour.setdefault(j, {})
            dj[a] = dj.get(a, 0) + q
        if c["annule"]:
            c["t_annul"] = c["cree"] + timedelta(hours=R.uniform(0.2, 3))
            self.commandes_attente[cid] = c
            self.pousser(c["t_annul"], "PREPARATION", cid)
            return
        charge = C.FACTEUR_JOUR[j.weekday()]
        if avant_cutoff:
            delai = R.uniform(0.2, 0.8) if c["urgent"] else R.uniform(0.3, 2.3) * (1 + 0.5 * (charge - 1))
        else:
            delai = R.uniform(0.5, 4)
        t, jp, eq, fin, decale = self.fenetre(c["cree"] + timedelta(hours=delai))
        if decale:
            t += timedelta(hours=R.uniform(0, 5.5))
        c["prep"] = t
        self.commandes_attente[cid] = c
        self.pousser(t, "PREPARATION", cid)

    def allouer(self, art, qte, t):
        """Allocation FEFO : lot libre qui périme le plus tôt, avec une durée de
        vie résiduelle minimale."""
        limite = t.date() + timedelta(days=C.DUREE_VIE_MIN_EXPEDITION_JOURS)
        cles = [k for k in self.bacs_article.get(art, ()) if k[2] == "LIBRE"
                and self.stock[k][0] - self.stock[k][1] > 0 and self.lots[k[1]]["peremption"] > limite
                and self.ref.emp_zone.get(k[0]) != "Z06"]
        cles.sort(key=lambda k: (self.lots[k[1]]["peremption"], k[1]))
        fefo_ko = False
        if len(cles) >= 2 and self.lots[cles[0][1]]["peremption"] < self.lots[cles[1][1]]["peremption"] \
                and self.R.random() < self.A["non_respect_fefo"]:
            cles[0], cles[1] = cles[1], cles[0]
            fefo_ko = True
        res, reste = [], qte
        for k in cles:
            if reste <= 0:
                break
            q = min(reste, self.stock[k][0] - self.stock[k][1])
            self.stock[k][1] += q
            res.append((k, q))
            reste -= q
        return res, fefo_ko and bool(res)

    def evt_PREPARATION(self, t, cid):
        R = self.R
        c = self.commandes_attente.pop(cid)
        if c["annule"]:
            self.ecrire_commande(c, [], None, None, statut_force="ANNULEE")
            return
        _, jp, eq, fin_fen, _ = self.fenetre(t)
        pickers, controleurs = self.presents(jp, eq)
        lignes = []
        fin_prep = t
        for i, (a, q) in enumerate(c["lignes"], 1):
            num = i * 10
            lid = f"{cid}-{num:05d}"
            alloc, fefo_ko = self.allouer(a, q, t)
            prep = sum(x[1] for x in alloc)
            motif = None
            if prep < q:
                bloque = any(k[2] == "BLOQUE" and self.stock[k][0] > 0 and self.lots[k[1]].get("motif") in ("S3", "RAPPEL")
                             for k in self.bacs_article.get(a, ()))
                motif = "LOT_BLOQUE" if bloque else "RUPTURE"
            taches = []
            for n_t, (cle, qa) in enumerate(alloc):
                tache = self.tache(t, jp, eq, pickers, controleurs, c, lid, a, cle, qa)
                if fefo_ko and n_t == 0:
                    self.v.ajouter("NON_RESPECT_FEFO", "preparations", tache["id"], {"lot_id": cle[1]})
                taches.append(tache)
                fin_prep = max(fin_prep, tache["fin"])
            lignes.append(dict(id=lid, num=num, art=a, q=q, prep=prep, motif=motif, taches=taches))

        if all(l["prep"] == 0 for l in lignes):
            self.ecrire_commande(c, lignes, None, None, statut_force="ANNULEE")
            return
        cr = self.choisir_creneau(c["tid"], fin_prep)
        livraison = self.livraison(c, cr)
        colis = max(1, round(len(lignes) / 3 + sum(l["prep"] / self.ref.articles[l["art"]]["colisage"]
                                                   for l in lignes)))
        cr["colis"] += colis
        c["fin_prep"] = fin_prep
        self.ecrire_commande(c, lignes, cr, livraison)

    def tache(self, t, jp, eq, pickers, controleurs, c, lid, art, cle, q):
        R = self.R
        emp, lot, _ = cle
        op = R.choice(pickers)
        o = self.ref.operateurs[op]
        zone = self.ref.emp_zone[emp]
        tz = self.ref.zones[zone]["type"]
        std = self.ref.standards.get(("PICKING", tz), self.ref.standards[("PICKING", "PICKING")])
        debut = t + timedelta(minutes=R.uniform(0, 12))
        anc = (debut.date() - o["arrivee"]).days
        vitesse = o["vitesse"] * (0.8 if o["contrat"] == "INTERIM" and anc < 14 else 1.0)
        duree = 3600 / std / vitesse * R.lognormvariate(0, 0.25)
        fin = debut + timedelta(seconds=duree)
        # probabilité d'erreur
        p = C.TAUX_ERREUR_BASE
        s1 = C.SCENARIOS["S1"]
        if o["s1"]:
            p *= s1["multiplicateur_erreur"] if anc < 21 else 1.5
        elif o["contrat"] == "INTERIM" and anc < 14:
            p *= 2.0
        if eq == "NUIT":
            p *= 1.15
        if tz == "FROID":
            p *= 1.3
        erreur = R.choices(["ECART_QUANTITE", "ERREUR_ARTICLE", "ERREUR_LOT"], [0.5, 0.25, 0.25])[0] \
            if R.random() < p else None
        controle = R.random() < C.TAUX_CONTROLE
        ctrl = R.choice(controleurs) if controle else None
        if not controle:
            resultat, echappee = "NON_CONTROLE", erreur
        elif erreur and R.random() < C.TAUX_DETECTION_CONTROLE:
            resultat, echappee = erreur, None
        else:
            resultat, echappee = "OK", erreur
        tid = self.nid("TO", 9)
        self.e.ecrire("sc_raw.preparations", {
            "tache_id": tid, "ligne_id": lid, "commande_id": c["id"],
            "vague_id": f"VG-{jp:%Y%m%d}-{eq[0]}{debut.hour:02d}", "operateur_id": op,
            "emplacement_id": emp, "lot_id": lot, "qte_a_prelever": q, "qte_prelevee": q,
            "debut": fts(debut), "fin": fts(fin), "resultat_controle": resultat, "controleur_id": ctrl,
            "_charge_le": self.charge_le})
        k = (jp, eq, op)
        self.taches_par_poste[k] = self.taches_par_poste.get(k, 0) + duree
        if ctrl:
            kc = (jp, eq, ctrl)
            self.controles_par_poste[kc] = self.controles_par_poste.get(kc, 0) + 1
        if lot == self.s4_lot and self.s4_heure_rappel and self.s4_heure_rappel <= debut < self.s4_heure_blocage:
            self.v.ajouter("EXPEDITION_LOT_RAPPELE", "preparations", tid,
                           {"lot_id": lot, "debut": fts(debut)}, scenario="S4")
        self.pousser(fin, "FIN_TACHE", (cle, q, lid, op))
        return dict(id=tid, lot=lot, q=q, fin=fin, op=op, echappee=echappee, debut=debut)

    def evt_FIN_TACHE(self, t, ch):
        cle, q, lid, op = ch
        b = self.stock[cle]
        b[1] -= q
        self.mouvement(t, cle[0], cle[1], "SORTIE_LIVRAISON", "601", "LIBRE", None, -q, lid, op)

    def choisir_creneau(self, tid, pret):
        limite = pret + timedelta(minutes=30)
        d = limite.date()
        for _ in range(10):
            for cr in self.creneaux_jour.get(d, []):
                if cr["tid"] == tid and cr["prevu"] >= limite:
                    return cr
            d += J
        raise RuntimeError("aucun créneau")

    def livraison(self, c, cr):
        R = self.R
        tr = self.ref.transporteurs[c["tid"]]
        jour = self.cal.ouvre_suivant(cr["depart"].date())
        p = tr["retard"]
        s2 = C.SCENARIOS["S2"]
        if cr["retard"] > 60 and cr["tid"] == s2["transporteur"]:
            p = 0.7
        elif cr["retard"] > 45:
            p = 0.25
        if R.random() < p:
            jour = self.cal.ouvre_suivant(jour)
        if tr["mode"] == "EXPRESS":
            h = R.uniform(8, 11)
        elif tr["mode"] == "FRIGORIFIQUE":
            h = R.uniform(7, 12)
        else:
            h = R.uniform(9, 17)
        return datetime.combine(jour, time(0)) + timedelta(hours=h)

    # ------------------------------------------------------------------
    # Ecriture d'une commande, de ses lignes et de son historique
    # ------------------------------------------------------------------
    def ecrire_commande(self, c, lignes, cr, livraison, statut_force=None):
        R, A, X = self.R, self.A, C.EXTRACTION
        cid = c["id"]
        jalons = [("CREEE", c["cree"]), ("CONFIRMEE", c["confirmee"])]
        if statut_force == "ANNULEE":
            jalons.append(("ANNULEE", c.get("t_annul") or c.get("prep") or c["confirmee"]))
        elif lignes and cr:
            jalons += [("EN_PREPARATION", c["prep"]), ("PREPAREE", c["fin_prep"]),
                       ("EXPEDIEE", cr["depart"]), ("LIVREE", livraison)]
        jalons = [(s, t) for s, t in jalons if t is not None and t <= X]
        statut = jalons[-1][0] if jalons else "CREEE"
        atteint = {s for s, _ in jalons}

        # historique (avec inversion EXPEDIEE / LIVREE injectée)
        hist = list(jalons)
        if "LIVREE" in atteint and R.random() < A["sequence_statuts_impossible"]:
            ie = [s for s, _ in hist].index("EXPEDIEE")
            il = [s for s, _ in hist].index("LIVREE")
            hist[ie], hist[il] = ("EXPEDIEE", hist[il][1]), ("LIVREE", hist[ie][1])
            self.v.ajouter("SEQUENCE_STATUTS_IMPOSSIBLE", "historique_statuts", cid)
        for s, t in hist:
            self.e.ecrire("sc_raw.historique_statuts", {
                "evenement_id": self.nid("EV", 10), "type_entite": "COMMANDE", "entite_id": cid,
                "statut": s, "horodatage": fts(t),
                "operateur_id": lignes[0]["taches"][0]["op"] if s == "EN_PREPARATION" and lignes and lignes[0]["taches"] else None,
                "source_systeme": "ERP" if s in ("CREEE", "CONFIRMEE", "ANNULEE") else ("TMS" if s == "LIVREE" else "WMS"),
                "_charge_le": self.charge_le})

        statut_entete = statut
        if statut == "LIVREE" and R.random() < A["entete_annulee_lignes_livrees"]:
            statut_entete = "ANNULEE"
            self.v.ajouter("STATUT_ENTETE_INCOHERENT", "commandes", cid, {"statut_lignes": "LIVREE"})
        entete = {
            "commande_id": cid, "donneur_ordre_id": c["do"], "destinataire_id": c["dest"],
            "site_id": self.site, "transporteur_id": c["tid"], "creneau_id": cr["id"] if cr and "EXPEDIEE" in atteint else None,
            "canal": c["canal"], "priorite": "URGENT" if c["urgent"] else "STANDARD",
            "date_creation": fts(c["cree"]), "date_livraison_demandee": fd(c["demandee"]),
            "statut_commande": statut_entete, "source_systeme": "ERP-SD", "_charge_le": self.charge_le}
        self.e.ecrire("sc_raw.commandes", entete)
        if R.random() < A["doublon_commande"]:
            self.e.ecrire("sc_raw.commandes", {**entete, "_charge_le": fts(C.CHARGE_LE + timedelta(minutes=7))})
            self.v.ajouter("DOUBLON_TECHNIQUE", "commandes", cid)

        lignes_ecrites = []
        if not lignes:      # commande annulée avant préparation
            lignes = [dict(id=f"{cid}-{i * 10:05d}", num=i * 10, art=a, q=q, prep=0, motif=None, taches=[])
                      for i, (a, q) in enumerate(c["lignes"], 1)]
        for l in lignes:
            exped = cr["depart"] if cr and "EXPEDIEE" in atteint and l["prep"] > 0 else None
            livr = livraison if exped and "LIVREE" in atteint else None
            if statut_force == "ANNULEE" or (l["prep"] == 0 and "EN_PREPARATION" in atteint):
                st_l = "ANNULEE"
            else:
                st_l = statut
            row = {
                "ligne_id": l["id"], "commande_id": cid, "num_ligne": l["num"], "article_id": l["art"],
                "site_id": self.site, "date_commande": fd(c["cree"].date()), "qte_commandee": l["q"],
                "qte_preparee": l["prep"] if "PREPAREE" in atteint or st_l == "ANNULEE" else None,
                "qte_expediee": l["prep"] if exped else (0 if st_l == "ANNULEE" else None),
                "qte_livree": l["prep"] if livr else (0 if st_l == "ANNULEE" else None),
                "date_promise": fd(c["promise"]), "date_expedition_reelle": fts(exped),
                "date_livraison_reelle": fts(livr), "statut_ligne": st_l, "motif_ecart": l["motif"],
                "_charge_le": self.charge_le}
            # ---- anomalies de données sur les lignes ----
            if st_l == "LIVREE":
                x = R.random()
                if x < A["date_livraison_manquante"]:
                    row["date_livraison_reelle"] = None
                    self.v.ajouter("DATE_LIVRAISON_MANQUANTE", "lignes_commande", l["id"])
                elif x < A["date_livraison_manquante"] + A["statut_non_mis_a_jour"]:
                    row["statut_ligne"] = "EXPEDIEE"
                    self.v.ajouter("STATUT_NON_MIS_A_JOUR", "lignes_commande", l["id"])
                elif x < A["date_livraison_manquante"] + A["statut_non_mis_a_jour"] + A["dates_inversees"]:
                    row["date_livraison_reelle"] = fts(exped - timedelta(hours=R.uniform(1, 20)))
                    self.v.ajouter("DATES_INVERSEES", "lignes_commande", l["id"])
            if exped and R.random() < A["date_expedition_manquante"]:
                row["date_expedition_reelle"] = None
                self.v.ajouter("DATE_EXPEDITION_MANQUANTE", "lignes_commande", l["id"])
            if exped and R.random() < A["qte_expediee_superieure"]:
                row["qte_expediee"] = l["prep"] + R.randint(1, 3)
                self.v.ajouter("QTE_EXPEDIEE_SUPERIEURE", "lignes_commande", l["id"])
            if R.random() < A["article_inconnu"]:
                row["article_id"] = f"ART-9{R.randrange(10000):04d}"
                self.v.ajouter("ARTICLE_INCONNU", "lignes_commande", l["id"], {"article_id": row["article_id"]})
            self.e.ecrire("sc_raw.lignes_commande", row)
            if R.random() < A["doublon_ligne"]:
                self.e.ecrire("sc_raw.lignes_commande", {**row, "_charge_le": fts(C.CHARGE_LE + timedelta(minutes=7))})
                self.v.ajouter("DOUBLON_TECHNIQUE", "lignes_commande", l["id"])
            lignes_ecrites.append((l, exped, livr))

        # doublon métier : commande renvoyée par EDI avec un nouveau numéro
        if statut_force is None and c["canal"] == "EDI" and R.random() < A["doublon_metier"] * 2:
            self.doublon_metier(c)

        if livraison and "LIVREE" in atteint:
            self.apres_livraison(c, lignes_ecrites, cr, livraison)

    def doublon_metier(self, c):
        R = self.R
        nid = self.nid("CMD", 8)
        cree = c["cree"] + timedelta(minutes=R.uniform(2, 25))
        if cree > C.EXTRACTION:
            return
        self.v.ajouter("DOUBLON_METIER", "commandes", nid, {"commande_origine": c["id"]})
        self.e.ecrire("sc_raw.commandes", {
            "commande_id": nid, "donneur_ordre_id": c["do"], "destinataire_id": c["dest"],
            "site_id": self.site, "transporteur_id": c["tid"], "creneau_id": None, "canal": "EDI",
            "priorite": "URGENT" if c["urgent"] else "STANDARD", "date_creation": fts(cree),
            "date_livraison_demandee": fd(c["demandee"]), "statut_commande": "CONFIRMEE",
            "source_systeme": "ERP-SD", "_charge_le": self.charge_le})
        for s, t in (("CREEE", cree), ("CONFIRMEE", cree + timedelta(minutes=R.uniform(1, 8)))):
            self.e.ecrire("sc_raw.historique_statuts", {
                "evenement_id": self.nid("EV", 10), "type_entite": "COMMANDE", "entite_id": nid,
                "statut": s, "horodatage": fts(t), "operateur_id": None, "source_systeme": "ERP",
                "_charge_le": self.charge_le})
        for i, (a, q) in enumerate(c["lignes"], 1):
            self.e.ecrire("sc_raw.lignes_commande", {
                "ligne_id": f"{nid}-{i * 10:05d}", "commande_id": nid, "num_ligne": i * 10, "article_id": a,
                "site_id": self.site, "date_commande": fd(cree.date()), "qte_commandee": q,
                "qte_preparee": None, "qte_expediee": None, "qte_livree": None,
                "date_promise": fd(c["promise"]), "date_expedition_reelle": None,
                "date_livraison_reelle": None, "statut_ligne": "CONFIRMEE", "motif_ecart": None,
                "_charge_le": self.charge_le})

    # ------------------------------------------------------------------
    # Après livraison : réclamations et retours
    # ------------------------------------------------------------------
    def reclamation(self, t, c, ligne_id, typ, q, cause, statut=None):
        if t > C.EXTRACTION:
            return
        if statut is None:
            statut = "OUVERTE" if (C.EXTRACTION - t).days < 7 else "JUSTIFIEE"
        self.e.ecrire("sc_raw.reclamations", {
            "reclamation_id": self.nid("RL", 6), "commande_id": c["id"], "ligne_id": ligne_id,
            "destinataire_id": c["dest"], "date_reclamation": fts(t), "type_reclamation": typ,
            "qte_concernee": q, "statut": statut, "cause_imputee": cause, "_charge_le": self.charge_le})

    def apres_livraison(self, c, lignes, cr, livr):
        R = self.R
        dtype = self.ref.destinataires[c["dest"]]["type"]
        en_retard = livr.date() > c["promise"]
        if en_retard and R.random() < 0.08:
            cause = "ENTREPOT" if cr["prevu"].date() >= c["promise"] else "TRANSPORT"
            self.reclamation(livr + timedelta(hours=R.uniform(2, 30)), c, None, "RETARD", None, cause)
        if c["froid"] and R.random() < 0.0005:
            self.reclamation(livr + timedelta(hours=R.uniform(1, 24)), c, None, "TEMPERATURE", None, "TRANSPORT")
        for l, exped, lv in lignes:
            for tch in l["taches"]:
                self.expeditions_lot.setdefault(tch["lot"], []).append((l["id"], c["id"], c["dest"], tch["q"], livr))
            if l["prep"] == 0:
                continue
            lot = l["taches"][0]["lot"]
            for tch in l["taches"]:
                if tch["echappee"] and R.random() < 0.75:
                    typ = {"ECART_QUANTITE": "MANQUANT", "ERREUR_ARTICLE": "ERREUR_ARTICLE",
                           "ERREUR_LOT": "ERREUR_LOT"}[tch["echappee"]]
                    self.reclamation(livr + timedelta(days=R.uniform(0.3, 4)), c, l["id"], typ,
                                     R.randint(1, max(1, min(2, tch["q"]))), "ENTREPOT")
                    if typ == "ERREUR_ARTICLE" and R.random() < 0.6:
                        self.retour_client(c, l, lot, tch["q"], livr, "ERREUR_PREPA", dtype)
            if R.random() < 0.0004:
                self.reclamation(livr + timedelta(hours=R.uniform(1, 30)), c, l["id"], "CASSE", 1, "TRANSPORT")
                if R.random() < 0.5:
                    self.retour_client(c, l, lot, 1, livr, "PRODUIT_ENDOMMAGE", dtype)
            if R.random() < 0.0005:
                self.reclamation(livr + timedelta(days=R.uniform(0.5, 5)), c, l["id"], "MANQUANT", 1,
                                 "DESTINATAIRE", "NON_JUSTIFIEE")
            if R.random() < 0.0025:
                self.retour_client(c, l, lot, l["prep"], livr, "ERREUR_COMMANDE", dtype)

    def retour_client(self, c, l, lot, q, livr, motif, dtype):
        R, s5 = self.R, C.SCENARIOS["S5"]
        s5_actif = s5["debut"] - 10 * J <= livr.date() <= s5["fin"]
        if dtype == "GROSSISTE":
            jours = R.randint(2, 20)
        elif R.random() < (0.65 if s5_actif else 0.88):
            jours = R.randint(1, 4)
        else:
            jours = R.randint(6, 15)
        arr = dt(self.cal.ouvre_ou_suivant(livr.date() + jours * J), R.randint(9, 15), R.randrange(60))
        self.planifier_retour(arr, dict(ligne=l["id"], cmd=c["id"], dest=c["dest"], art=l["art"], lot=lot,
                                        q=q, exped=livr, motif=motif))

    def planifier_retour(self, arr, r):
        r["id"] = self.nid("RT", 6)
        r["arrivee"] = arr
        self.retours_attente[r["id"]] = r
        self.pousser(arr, "RETOUR_ARRIVEE", r["id"])

    def evt_RETOUR_ARRIVEE(self, t, rid):
        r = self.retours_attente[rid]
        r["emp_q"] = self.R.choice(self.ref.emplacements["Z06"])
        self.mouvement(t, r["emp_q"], r["lot"], "RETOUR_CLIENT", "655", None, "CONTROLE_QUALITE", r["q"], rid)
        r["recu"] = True
        self.pousser(dt(self.cal.ouvre_suivant(t.date(), self.R.randint(1, 3)), 11), "RETOUR_DECISION", rid)

    def evt_RETOUR_DECISION(self, t, rid):
        R, s5 = self.R, C.SCENARIOS["S5"]
        r = self.retours_attente.pop(rid)
        lot = self.lots[r["lot"]]
        dinfo = self.ref.destinataires[r["dest"]]
        jours = (r["arrivee"].date() - r["exped"].date()).days
        hors_delai = (not dinfo["gros"]) and jours > s5["delai_max_jours"]
        violation = False
        if r["motif"] == "RAPPEL":
            decision = "RETOUR_LABO"
        elif r["motif"] == "PRODUIT_ENDOMMAGE" or lot["rappele"] or lot["statut"] != "LIBRE" \
                or lot["peremption"] <= t.date() + timedelta(days=C.DUREE_VIE_MIN_EXPEDITION_JOURS):
            decision = "DESTRUCTION"
        elif hors_delai:
            if s5["debut"] <= t.date() <= s5["fin"] and R.random() < 0.85:
                decision, violation = "REMISE_EN_STOCK", True
            else:
                decision = "DESTRUCTION"
        else:
            decision = "REMISE_EN_STOCK" if R.random() < 0.92 else "DESTRUCTION"
        if violation:
            self.v.ajouter("RETOUR_HORS_DELAI_REMIS_EN_STOCK", "retours", rid,
                           {"jours_depuis_livraison": jours}, scenario="S5")
        if decision == "REMISE_EN_STOCK":
            self.mouvement(t, r["emp_q"], r["lot"], "CHANGEMENT_STATUT", "321", "CONTROLE_QUALITE", "LIBRE", -r["q"], rid)
            self.mouvement(t, lot["emp"], r["lot"], "CHANGEMENT_STATUT", "321", "CONTROLE_QUALITE", "LIBRE", r["q"], rid)
        elif decision == "DESTRUCTION":
            self.mouvement(t, r["emp_q"], r["lot"], "DESTRUCTION", "553", "CONTROLE_QUALITE", None, -r["q"], rid)
        else:
            self.mouvement(t, r["emp_q"], r["lot"], "RETOUR_LABO", "122", "CONTROLE_QUALITE", None, -r["q"], rid)
        dec_ecrite, date_dec = decision, t
        if decision == "REMISE_EN_STOCK" and R.random() < self.A["remise_stock_sans_decision_rp"]:
            dec_ecrite, date_dec = "EN_ATTENTE", None
            self.v.ajouter("REMISE_STOCK_SANS_DECISION_RP", "retours", rid)
        self.ecrire_retour(r, dec_ecrite, date_dec)

    def ecrire_retour(self, r, decision, date_dec):
        self.e.ecrire("sc_raw.retours", {
            "retour_id": r["id"], "commande_id": r["cmd"], "ligne_id": r["ligne"], "destinataire_id": r["dest"],
            "article_id": r["art"], "lot_id": r["lot"], "qte_retournee": r["q"],
            "date_expedition_origine": fts(r["exped"]), "date_retour": fts(r["arrivee"]), "motif": r["motif"],
            "statut_qualite_entree": "CONTROLE_QUALITE", "decision_rp": decision,
            "date_decision_rp": fts(date_dec), "_charge_le": self.charge_le})

    # ------------------------------------------------------------------
    # Fin de simulation
    # ------------------------------------------------------------------
    def finaliser(self):
        R = self.R
        # commandes reçues mais pas encore préparées à l'extraction
        for c in self.commandes_attente.values():
            self.ecrire_commande(c, [], None, None, statut_force="ANNULEE" if c["annule"] and c["t_annul"] <= C.EXTRACTION else None)
        # retours reçus en attente de décision
        for r in self.retours_attente.values():
            if r.get("recu"):
                self.ecrire_retour(r, "EN_ATTENTE", None)
        # réceptions attendues
        for rec in self.receptions_attente.values():
            self.e.ecrire("sc_raw.receptions", {
                "reception_id": rec["id"], "site_id": self.site, "donneur_ordre_id": rec["do"],
                "article_id": rec["article"], "lot_id": None, "qte_attendue": rec["qte"], "qte_recue": None,
                "date_prevue": fd(rec["prevue"]), "date_reelle": None, "statut_qualite_initial": None,
                "statut_reception": "PREVUE", "_charge_le": self.charge_le})
        # créneaux d'enlèvement
        for cr in self.creneaux.values():
            if cr["prevu"].date() > C.FIN + 3 * J:
                continue
            passe = cr["depart"] <= C.EXTRACTION
            self.e.ecrire("sc_raw.creneaux", {
                "creneau_id": cr["id"], "site_id": self.site, "quai": f"E{int(cr['tid'][1:]) + 4}",
                "transporteur_id": cr["tid"], "type_flux": "ENLEVEMENT", "debut_prevu": fts(cr["prevu"]),
                "fin_prevue": fts(cr["prevu"] + timedelta(minutes=45)),
                "arrivee_reelle": fts(cr["arrivee"]) if cr["arrivee"] <= C.EXTRACTION else None,
                "depart_reel": fts(cr["depart"]) if passe else None,
                "nb_colis_prevus": cr["colis"],
                "nb_colis_charges": (cr["colis"] if R.random() < 0.99 else max(0, cr["colis"] - 1)) if passe else None,
                "_charge_le": self.charge_le})
        # lots
        for lid, l in self.lots.items():
            self.e.ecrire("sc_raw.dim_lot", {
                "lot_id": lid, "article_id": l["article"], "num_lot": l["num"], "date_fabrication": fd(l["fab"]),
                "date_peremption": fd(l["peremption"]), "statut_qualite": l["statut"], "rappele": l["rappele"],
                "date_rappel": fd(l["date_rappel"]), "date_maj": fts(l["maj"]), "_charge_le": self.charge_le})
        self.temps_travail()
        self.scenarios()

    def temps_travail(self):
        """Heures par opérateur et activité. Les heures de picking viennent des
        tâches réelles ; le reste du poste est réparti sur les autres activités."""
        R = self.R
        postes = sorted({(jp, eq) for (jp, eq) in self._presences if jp <= C.EXTRACTION.date()})
        for jp, eq in postes:
            pickers, ctrls = self._presences[(jp, eq)]
            for op in pickers + ctrls:
                heures = {}
                pk = self.taches_par_poste.get((jp, eq, op), 0) / 3600 * 1.08
                if pk:
                    heures["PICKING"] = pk
                nc = self.controles_par_poste.get((jp, eq, op), 0)
                if nc:
                    heures["CONTROLE"] = nc / self.ref.standards[("CONTROLE", "EXPEDITION")] * R.uniform(1.0, 1.2)
                heures["INDIRECT"] = 0.5
                if eq == "MATIN" and op in pickers[:2]:
                    heures["RECEPTION"] = self.receptions_par_jour.get(jp, 0) * 0.35
                if eq == "APRES_MIDI" and op in pickers[:3]:
                    heures["CHARGEMENT"] = 1.2
                reste = C.HEURES_POSTE - sum(heures.values())
                if reste > 0.25:
                    heures["RANGEMENT"] = reste
                for act, h in heures.items():
                    if h <= 0:
                        continue
                    self.e.ecrire("sc_raw.temps_travail", {
                        "pointage_id": self.nid("PT", 7), "date_travail": fd(jp), "site_id": self.site,
                        "equipe": eq, "operateur_id": op, "activite": act, "heures": round(h, 2),
                        "_charge_le": self.charge_le})

    def scenarios(self):
        lignes = []
        for sid, s in C.SCENARIOS.items():
            deb = s.get("debut") or s.get("date_rappel")
            fin = s.get("fin") or (s.get("date_rappel") + timedelta(days=s.get("delai_blocage_jours", 0)))
            if sid == "S1":
                pt, pv = "EQUIPE", s["equipe"]
            elif sid == "S2":
                pt, pv = "CRENEAU", f"{s['transporteur']} {s['creneau']:%H:%M}"
            elif sid == "S3":
                pt, pv = "ARTICLE", self.s3_article
            elif sid == "S4":
                pt, pv = "LOT", self.s4_lot
            else:
                pt, pv = "PROCESSUS", "RETOURS"
            lignes.append({"scenario_id": sid, "libelle": s["libelle"], "description": s["libelle"],
                           "debut": fd(deb), "fin": fd(fin), "perimetre_type": pt, "perimetre_valeur": pv,
                           "cause_reelle": s["cause"]})
        for l in lignes:
            self.e.ecrire("sc_eval.scenarios", l)
