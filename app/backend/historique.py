"""
Historique des conversations.

Firestore plutôt que BigQuery : il s'agit de petites écritures et lectures
fréquentes, instantanées. BigQuery met environ une seconde par requête et
n'est pas fait pour ça ; Firestore répond en quelques millisecondes et son
palier gratuit (base "(default)") couvre très largement une démo.

Si Firestore n'est pas disponible (base pas encore créée, droits manquants),
l'application bascule automatiquement sur un stockage en mémoire : elle reste
utilisable, seul l'historique est perdu au redémarrage. L'état est visible
dans l'endpoint /api/etat.
"""
import datetime as dt
import logging
import threading
import uuid

import config as C

log = logging.getLogger("historique")


def maintenant():
    return dt.datetime.now(dt.timezone.utc)


def titre_depuis(question):
    q = " ".join(question.split())
    return q[:60] + ("..." if len(q) > 60 else "")


class HistoriqueMemoire:
    nom = "memoire"

    def __init__(self):
        self.conv = {}
        self.verrou = threading.Lock()

    def lister(self, n=30):
        with self.verrou:
            l = sorted(self.conv.values(), key=lambda c: c["maj"], reverse=True)[:n]
            return [{"id": c["id"], "titre": c["titre"], "maj": c["maj"].isoformat()} for c in l]

    def lire(self, conv_id):
        with self.verrou:
            c = self.conv.get(conv_id)
            return list(c["messages"]) if c else None

    def ajouter_echange(self, conv_id, question, reponse):
        with self.verrou:
            if not conv_id or conv_id not in self.conv:
                conv_id = conv_id or uuid.uuid4().hex[:16]
                self.conv[conv_id] = {"id": conv_id, "titre": titre_depuis(question), "maj": maintenant(), "messages": []}
            c = self.conv[conv_id]
            t = maintenant().isoformat()
            c["messages"].append({"role": "user", "texte": question, "date": t})
            c["messages"].append({"role": "model", "texte": reponse["reponse"], "date": t,
                                  "artefacts": reponse["artefacts"], "outils": reponse["outils"],
                                  "usage": reponse["usage"]})
            c["maj"] = maintenant()
            return conv_id


class HistoriqueFirestore:
    nom = "firestore"

    def __init__(self):
        from google.cloud import firestore
        self.fs = firestore
        self.db = firestore.Client(project=C.PROJET or None)
        self.col = self.db.collection(C.FIRESTORE_COLLECTION)
        list(self.col.limit(1).stream())          # vérifie tout de suite que la base existe

    def lister(self, n=30):
        docs = self.col.order_by("maj", direction=self.fs.Query.DESCENDING).limit(n).stream()
        return [{"id": d.id, "titre": d.get("titre"), "maj": d.get("maj").isoformat()} for d in docs]

    def lire(self, conv_id):
        ref = self.col.document(conv_id)
        if not ref.get().exists:
            return None
        return [m.to_dict() for m in ref.collection("messages").order_by("ordre").stream()]

    def ajouter_echange(self, conv_id, question, reponse):
        nouveau = not conv_id
        conv_id = conv_id or uuid.uuid4().hex[:16]
        ref = self.col.document(conv_id)
        t = maintenant()
        lot = self.db.batch()                      # une seule écriture groupée par échange
        if nouveau or not ref.get().exists:
            lot.set(ref, {"titre": titre_depuis(question), "creee": t, "maj": t})
        else:
            lot.update(ref, {"maj": t})
        ordre = int(t.timestamp() * 1000)
        msgs = ref.collection("messages")
        lot.set(msgs.document(f"{ordre}-q"), {"ordre": ordre, "role": "user", "texte": question, "date": t.isoformat()})
        lot.set(msgs.document(f"{ordre}-r"), {"ordre": ordre + 1, "role": "model", "texte": reponse["reponse"],
                                             "date": t.isoformat(), "artefacts": reponse["artefacts"],
                                             "outils": reponse["outils"], "usage": reponse["usage"]})
        lot.commit()
        return conv_id


class HistoriqueResilient:
    """Utilise Firestore, et bascule en mémoire à la première panne."""

    def __init__(self, essayer_firestore=True):
        self.secours = HistoriqueMemoire()
        self.principal = None
        self.raison_secours = None
        if essayer_firestore:
            try:
                self.principal = HistoriqueFirestore()
            except Exception as ex:
                self._basculer(ex)
        else:
            self.raison_secours = "mode démo"

    @property
    def nom(self):
        return self.principal.nom if self.principal else self.secours.nom

    def _basculer(self, ex):
        self.principal = None
        self.raison_secours = f"{type(ex).__name__}: {str(ex)[:200]}"
        log.warning("Firestore indisponible, historique en mémoire : %s", self.raison_secours)

    def _appel(self, methode, *args):
        if self.principal:
            try:
                return getattr(self.principal, methode)(*args)
            except Exception as ex:
                self._basculer(ex)
        return getattr(self.secours, methode)(*args)

    def lister(self, n=30):
        return self._appel("lister", n)

    def lire(self, conv_id):
        return self._appel("lire", conv_id)

    def ajouter_echange(self, conv_id, question, reponse):
        return self._appel("ajouter_echange", conv_id, question, reponse)
