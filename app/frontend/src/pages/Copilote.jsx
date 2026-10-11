// Page principale : discussion avec l'agent à gauche, panneau de résultats à droite.
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "../api.js";
import { ListeArtefacts } from "../components/Artefacts.jsx";
import { ActionTiroir, DeriveTiroir } from "../components/Lean.jsx";
import { LIBELLES_OUTILS, formater } from "../format.js";

const SUGGESTIONS = [
  "Mes données sont-elles fiables ?",
  "Quelles sont les anomalies critiques et que dois-je faire en priorité ?",
  "Que s'est-il passé avec le lot rappelé LOT005075 ?",
  "Pourquoi l'OTIF a-t-il baissé en janvier ?",
  "Compare l'OTIF des donneurs d'ordre à leurs objectifs sur les 3 derniers mois",
  "Quelles dérives dois-je traiter en priorité ?",
  "Trace la commande CMD00001057",
];

const ETAPES_ATTENTE = ["Analyse de la question", "Choix des outils", "Interrogation de BigQuery", "Rédaction de la réponse"];

export default function Copilote() {
  const [conversations, setConversations] = useState([]);
  const [convId, setConvId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [selection, setSelection] = useState(null);       // index du message dont on affiche les résultats
  const [question, setQuestion] = useState("");
  const [enCours, setEnCours] = useState(false);
  const [etape, setEtape] = useState(0);
  const [erreur, setErreur] = useState(null);
  const [tiroir, setTiroir] = useState(null);             // {type: "derive" | "action", id}
  const [menuId, setMenuId] = useState(null);             // conversation dont le menu (...) est ouvert
  const [renommeId, setRenommeId] = useState(null);       // conversation en cours de renommage
  const [renommeTexte, setRenommeTexte] = useState("");
  const [supprimeId, setSupprimeId] = useState(null);     // conversation dont on confirme la suppression
  const finRef = useRef(null);

  const rafraichirListe = () => api.conversations().then(setConversations).catch(() => {});
  useEffect(() => { rafraichirListe(); }, []);
  useEffect(() => { finRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, enCours]);
  useEffect(() => {
    if (!enCours) return;
    setEtape(0);
    const t = setInterval(() => setEtape((e) => Math.min(e + 1, ETAPES_ATTENTE.length - 1)), 1800);
    return () => clearInterval(t);
  }, [enCours]);

  async function envoyer(texte) {
    const q = (texte ?? question).trim();
    if (!q || enCours) return;
    setQuestion("");
    setErreur(null);
    setMessages((m) => [...m, { role: "user", texte: q }]);
    setEnCours(true);
    try {
      const r = await api.chat(q, convId);
      setConvId(r.conversation_id);
      setMessages((m) => {
        const suite = [...m, { role: "model", texte: r.reponse, artefacts: r.artefacts, outils: r.outils, usage: r.usage }];
        if (r.artefacts?.length) setSelection(suite.length - 1);
        return suite;
      });
      rafraichirListe();
    } catch (e) {
      setErreur(e.message);
      setMessages((m) => m.slice(0, -1));
      setQuestion(q);
    } finally {
      setEnCours(false);
    }
  }

  async function ouvrir(id) {
    try {
      const msgs = await api.conversation(id);
      setConvId(id);
      setMessages(msgs);
      const dernier = msgs.map((m, i) => (m.artefacts?.length ? i : -1)).filter((i) => i >= 0).pop();
      setSelection(dernier ?? null);
      setErreur(null);
    } catch (e) { setErreur(e.message); }
  }

  function nouvelle() {
    setConvId(null); setMessages([]); setSelection(null); setErreur(null);
  }

  function demarrerRenommer(c) {
    setMenuId(null); setRenommeId(c.id); setRenommeTexte(c.titre);
  }
  async function validerRenommer() {
    const id = renommeId;
    const titre = renommeTexte.trim();
    setRenommeId(null);
    if (!id || !titre) return;
    const actuel = conversations.find((c) => c.id === id);
    if (actuel && titre === actuel.titre) return;
    try { await api.renommerConversation(id, titre); rafraichirListe(); }
    catch (e) { setErreur(e.message); }
  }
  async function confirmerSupprimer() {
    const id = supprimeId;
    setSupprimeId(null);
    try {
      await api.supprimerConversation(id);
      if (id === convId) nouvelle();
      rafraichirListe();
    } catch (e) { setErreur(e.message); }
  }

  const affiche = selection !== null ? messages[selection] : null;

  return (
    <div className="copilote">
      <aside className="historique">
        <button className="bouton principal plein" onClick={nouvelle}>+ Nouvelle conversation</button>
        <div className="liste-conv">
          {conversations.map((c) => (
            <div key={c.id} className={`conv ${c.id === convId ? "active" : ""} ${renommeId === c.id ? "edition" : ""}`}>
              {renommeId === c.id ? (
                <input className="conv-edition" autoFocus value={renommeTexte} maxLength={80}
                       onChange={(e) => setRenommeTexte(e.target.value)}
                       onBlur={validerRenommer}
                       onKeyDown={(e) => {
                         if (e.key === "Enter") { e.preventDefault(); validerRenommer(); }
                         if (e.key === "Escape") { setRenommeId(null); }
                       }} />
              ) : (
                <button className="conv-ouvrir" onClick={() => ouvrir(c.id)} title={c.titre}>
                  <span className="conv-titre">{c.titre}</span>
                  <small>{formater(c.maj, "datetime")}</small>
                </button>
              )}
              {renommeId !== c.id && (
                <button className="conv-menu" aria-label="Options de la conversation"
                        onClick={(e) => { e.stopPropagation(); setMenuId(menuId === c.id ? null : c.id); }}>⋯</button>
              )}
              {menuId === c.id && (
                <div className="menu-conv" role="menu">
                  <button role="menuitem" onClick={() => demarrerRenommer(c)}>Renommer</button>
                  <button role="menuitem" className="danger" onClick={() => { setMenuId(null); setSupprimeId(c.id); }}>Supprimer</button>
                </div>
              )}
            </div>
          ))}
          {!conversations.length && <p className="note">Aucune conversation enregistrée.</p>}
        </div>
      </aside>
      {menuId && <div className="voile-menu" onClick={() => setMenuId(null)} />}

      <section className="discussion">
        <div className="fil">
          {!messages.length && (
            <div className="accueil">
              <h1>Bonjour, que voulez-vous vérifier ?</h1>
              <p>Logix interroge les données de l'entrepôt dans BigQuery et cite toujours ses sources.</p>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => <button key={s} onClick={() => envoyer(s)}>{s}</button>)}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`bulle ${m.role} ${i === selection ? "selection" : ""}`}
                 onClick={m.artefacts?.length ? () => setSelection(i) : undefined}>
              {m.role === "model" ? <ReactMarkdown>{m.texte}</ReactMarkdown> : <p>{m.texte}</p>}
              {m.role === "model" && (m.outils?.length > 0 || m.usage) && (
                <div className="meta">
                  {m.outils?.map((o, j) => (
                    <span key={j} className={`puce ${o.erreur ? "puce-erreur" : ""}`} title={o.erreur || JSON.stringify(o.arguments)}>
                      {LIBELLES_OUTILS[o.outil] || o.outil}{o.arguments && Object.values(o.arguments).length ? ` : ${Object.values(o.arguments).join(", ")}` : ""} ({o.duree_ms} ms)
                    </span>
                  ))}
                  {m.usage?.jetons_entree > 0 && (
                    <span className="cout" title="Jetons consommés et coût estimé de la question">
                      {formater(m.usage.jetons_entree + m.usage.jetons_sortie, "nombre")} jetons, ~{(m.usage.cout_estime_usd * 100).toFixed(2).replace(".", ",")} cts $
                    </span>
                  )}
                  {m.artefacts?.length > 0 && <span className="voir">Résultats dans le panneau →</span>}
                </div>
              )}
            </div>
          ))}
          {enCours && (
            <div className="bulle model attente">
              <span className="points"><i /><i /><i /></span> {ETAPES_ATTENTE[etape]}...
            </div>
          )}
          <div ref={finRef} />
        </div>
        {erreur && <div className="erreur">{erreur}</div>}
        <form className="saisie" onSubmit={(e) => { e.preventDefault(); envoyer(); }}>
          <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={2} maxLength={2000}
                    placeholder="Posez une question sur l'entrepôt (Entrée pour envoyer, Maj+Entrée pour aller à la ligne)"
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); envoyer(); } }} />
          <button className="bouton principal" disabled={enCours || !question.trim()}>Envoyer</button>
        </form>
      </section>

      <aside className="panneau">
        {affiche ? (
          <>
            <div className="entete-panneau">Résultats de la réponse sélectionnée</div>
            <ListeArtefacts artefacts={affiche.artefacts}
                            onAction={(type, id) => (type === "derive" || type === "action") && setTiroir({ type, id })} />
          </>
        ) : (
          <div className="panneau-vide">
            <p>Les tableaux, graphiques et fiches de traçabilité trouvés par Logix s'afficheront ici.</p>
            <p className="note">Cliquez sur un numéro de commande ou de lot dans un tableau pour ouvrir sa traçabilité.</p>
          </div>
        )}
      </aside>
      {tiroir?.type === "derive" && <DeriveTiroir id={tiroir.id} onFerme={() => setTiroir(null)} />}
      {tiroir?.type === "action" && (
        <ActionTiroir id={tiroir.id} onFerme={() => setTiroir(null)} onOuvrirDerive={(id) => setTiroir({ type: "derive", id })} />
      )}
      {supprimeId && (
        <div className="voile" onClick={() => setSupprimeId(null)}>
          <div className="modale" onClick={(e) => e.stopPropagation()}>
            <h2>Supprimer la conversation ?</h2>
            <p className="note">Cette action est définitive, la conversation et ses messages seront effacés.</p>
            <div className="actions-modale">
              <button className="bouton" onClick={() => setSupprimeId(null)}>Annuler</button>
              <button className="bouton principal danger" onClick={confirmerSupprimer}>Supprimer</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
