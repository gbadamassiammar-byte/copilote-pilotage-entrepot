// Rendu des "artefacts" renvoyés par l'API : indicateurs, graphiques à barres,
// tableaux, chronologies. Le même composant sert au panneau du copilote et
// aux pages de tableau de bord : un seul format, plusieurs écrans.
import { useMemo, useState } from "react";
import { Area, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, Line, LineChart, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { cibleTrace, etiquettePeriode, formater, formaterKpi } from "../format.js";
import { couleursGraphes, useTheme } from "../theme.jsx";
import { useTrace } from "./Trace.jsx";

// Infobulle Recharts aux couleurs du thème (par défaut elle est toujours blanche).
function styleInfobulle(g) {
  return {
    contentStyle: { background: g.infobulle.fond, border: `1px solid ${g.infobulle.bord}`, borderRadius: 8, color: g.infobulle.texte, fontSize: 12 },
    labelStyle: { color: g.infobulle.texte, fontWeight: 600 },
    itemStyle: { color: g.infobulle.texte },
    cursor: { fill: g.grille, stroke: g.grille },
  };
}

export function Artefact({ a, onAction }) {
  switch (a.type) {
    case "indicateurs": return <Indicateurs a={a} onAction={onAction} />;
    case "courbes": return <Courbes a={a} />;
    case "carte_controle": return <CarteControle a={a} />;
    case "prevision": return <Prevision a={a} />;
    case "barres": return <Barres a={a} />;
    case "tableau": return <Tableau a={a} onAction={onAction} />;
    case "chronologie": return <Chronologie a={a} />;
    default: return null;
  }
}

export function ListeArtefacts({ artefacts, onAction }) {
  if (!artefacts?.length) return null;
  return <div className="artefacts">{artefacts.map((a, i) => <Artefact key={i} a={a} onAction={onAction} />)}</div>;
}

function Indicateurs({ a, onAction }) {
  return (
    <section className="carte">
      {a.titre && <h3>{a.titre}</h3>}
      <div className="indicateurs">
        {a.items.map((it, i) => {
          const cliquable = onAction && it.action;
          return (
            <div key={i} className={`indicateur ton-${it.ton || "neutre"} ${cliquable ? "cliquable" : ""}`}
                 onClick={cliquable ? () => onAction(it.action, it.kpi) : undefined}
                 title={cliquable ? "Voir les lignes en échec" : undefined}>
              <span className="valeur">{formater(it.valeur, it.format) || "n.d."}</span>
              <span className="label">{it.label}</span>
              {it.detail && <span className="detail">{it.detail}</span>}
            </div>
          );
        })}
      </div>
      {a.note && <p className="note">{a.note}</p>}
    </section>
  );
}

// Courbes d'évolution : une ligne par série (périmètre, ou OTIF fiabilisé / brut).
function Courbes({ a }) {
  const g = couleursGraphes(useTheme().theme);
  const { donnees, noms } = useMemo(() => {
    const parX = new Map();
    const noms = (a.series || []).filter((s) => s.points?.length).map((s) => s.nom);
    for (const s of a.series || []) {
      for (const p of s.points || []) {
        if (!parX.has(p.x)) parX.set(p.x, { x: p.x });
        parX.get(p.x)[s.nom] = p.y;
      }
    }
    return { donnees: [...parX.values()].sort((u, v) => (u.x < v.x ? -1 : 1)), noms };
  }, [a.series]);
  if (!donnees.length) return null;
  const unite = a.unite;
  // taux proches de 100 % (OTIF, service...) : axe borné aux points entiers pour lire les écarts
  const valeurs = donnees.flatMap((d) => noms.map((n) => d[n])).filter((v) => typeof v === "number");
  const grandsTaux = unite === "%" && Math.min(...valeurs) >= 50;
  return (
    <section className="carte">
      <h3>{a.titre}</h3>
      <ResponsiveContainer width="100%" height={240}>
        <LineChart data={donnees} margin={{ left: 0, right: 16, top: 6, bottom: 0 }}>
          <CartesianGrid stroke={g.grille} vertical={false} />
          <XAxis dataKey="x" tickFormatter={(x) => etiquettePeriode(x, a.granularite)} fontSize={11} stroke={g.axe} minTickGap={14} />
          <YAxis domain={grandsTaux ? [(m) => Math.floor(m), (m) => Math.min(100, Math.ceil(m))] : ["auto", "auto"]} fontSize={11} stroke={g.axe} width={48}
                 tickFormatter={(v) => v.toLocaleString("fr-FR", { maximumFractionDigits: 1 })} />
          <Tooltip {...styleInfobulle(g)} formatter={(v, nom) => [formaterKpi(v, unite), nom]}
                   labelFormatter={(x) => (a.granularite === "semaine" ? `Semaine du ${etiquettePeriode(x, "jour")}` : etiquettePeriode(x, a.granularite))} />
          {noms.length > 1 && <Legend wrapperStyle={{ fontSize: 11, color: g.axe }} />}
          {noms.map((n, i) => (
            <Line key={n} type="monotone" dataKey={n} stroke={g.palette[i % g.palette.length]} strokeWidth={2}
                  strokeDasharray={/brut/i.test(n) ? "5 4" : undefined} dot={donnees.length <= 16 ? { r: 3 } : false} connectNulls />
          ))}
        </LineChart>
      </ResponsiveContainer>
      {a.note && <p className="note">{a.note}</p>}
    </section>
  );
}

// Carte de contrôle : valeur hebdomadaire, ligne centrale, bande des limites à 3 sigma,
// points hors contrôle en rouge, période de la dérive surlignée.
function CarteControle({ a }) {
  const g = couleursGraphes(useTheme().theme);
  const donnees = useMemo(() => (a.points || []).map((p) => ({ ...p, bande: [p.lci, p.lcs] })), [a.points]);
  if (!donnees.length) return null;
  const semaines = donnees.map((p) => p.x);
  const x1 = semaines.find((x) => x >= a.fenetre?.debut) || semaines[0];
  const x2 = [...semaines].reverse().find((x) => x <= a.fenetre?.fin) || x1;
  const valeurs = donnees.flatMap((p) => [p.valeur, p.lci, p.lcs]).filter((v) => typeof v === "number");
  const min = Math.min(...valeurs), max = Math.max(...valeurs);
  const marge = (max - min) * 0.08 || 1;
  const Point = (props) => {
    const { cx, cy, payload } = props;
    if (cx == null || cy == null) return null;
    return <circle cx={cx} cy={cy} r={payload.alerte ? 5 : 3} fill={payload.alerte ? g.tons.critique : g.palette[0]}
                   stroke={payload.alerte ? g.tons.critique : g.palette[0]} />;
  };
  return (
    <section className="carte">
      <h3>{a.titre}</h3>
      <ResponsiveContainer width="100%" height={260}>
        <ComposedChart data={donnees} margin={{ left: 0, right: 16, top: 6, bottom: 0 }}>
          <CartesianGrid stroke={g.grille} vertical={false} />
          <ReferenceArea x1={x1} x2={x2} fill={g.tons.critique} fillOpacity={0.08} stroke="none" ifOverflow="extendDomain" />
          <XAxis dataKey="x" tickFormatter={(x) => etiquettePeriode(x, "jour")} fontSize={11} stroke={g.axe} minTickGap={14} />
          <YAxis domain={[Math.max(0, min - marge), max + marge]} fontSize={11} stroke={g.axe} width={48}
                 tickFormatter={(v) => v.toLocaleString("fr-FR", { maximumFractionDigits: 1 })} />
          <Tooltip {...styleInfobulle(g)}
                   labelFormatter={(x) => `Semaine du ${etiquettePeriode(x, "jour")}`}
                   formatter={(v, nom) => (Array.isArray(v)
                     ? [`${formaterKpi(v[0], a.unite)} à ${formaterKpi(v[1], a.unite)}`, "Limites (3 sigma)"]
                     : [formaterKpi(v, a.unite), nom])} />
          <Area dataKey="bande" stroke="none" fill={g.palette[1]} fillOpacity={0.18} name="Limites" isAnimationActive={false} />
          <Line dataKey="centre" stroke={g.palette[1]} strokeDasharray="5 4" dot={false} name="Référence" isAnimationActive={false} />
          <Line dataKey="valeur" stroke={g.palette[0]} strokeWidth={2} dot={<Point />} activeDot={{ r: 5 }} name="Valeur" isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
      {a.note && <p className="note">{a.note}</p>}
    </section>
  );
}

// Prévision : historique réel (trait plein) puis prévision (pointillé) avec bande d'intervalle.
function Prevision({ a }) {
  const g = couleursGraphes(useTheme().theme);
  const donnees = useMemo(() => {
    const m = new Map();
    for (const h of a.historique || []) m.set(h.x, { x: h.x, reel: h.reel });
    for (const p of a.prevision || []) {
      const e = m.get(p.x) || { x: p.x };
      e.prevu = p.prevu; e.bande = p.bande; m.set(p.x, e);
    }
    return [...m.values()].sort((u, v) => (u.x < v.x ? -1 : 1));
  }, [a.historique, a.prevision]);
  if (!donnees.length) return null;
  const debutPrev = a.prevision?.[0]?.x;
  return (
    <section className="carte">
      <h3>{a.titre}</h3>
      <ResponsiveContainer width="100%" height={260}>
        <ComposedChart data={donnees} margin={{ left: 0, right: 16, top: 6, bottom: 0 }}>
          <CartesianGrid stroke={g.grille} vertical={false} />
          {debutPrev && <ReferenceArea x1={debutPrev} x2={donnees[donnees.length - 1].x} fill={g.palette[0]} fillOpacity={0.05} stroke="none" />}
          <XAxis dataKey="x" tickFormatter={(x) => etiquettePeriode(x, "jour")} fontSize={11} stroke={g.axe} minTickGap={24} />
          <YAxis fontSize={11} stroke={g.axe} width={48} tickFormatter={(v) => v.toLocaleString("fr-FR")} />
          <Tooltip {...styleInfobulle(g)} labelFormatter={(x) => etiquettePeriode(x, "jour")}
                   formatter={(v, nom) => (Array.isArray(v)
                     ? [`${v[0].toLocaleString("fr-FR")} à ${v[1].toLocaleString("fr-FR")}`, "Intervalle 90 %"]
                     : [v?.toLocaleString("fr-FR"), nom === "reel" ? "Réel" : "Prévu"])} />
          <Area dataKey="bande" stroke="none" fill={g.palette[0]} fillOpacity={0.15} name="Intervalle" isAnimationActive={false} />
          <Line dataKey="reel" stroke={g.palette[0]} strokeWidth={2} dot={false} name="reel" connectNulls={false} isAnimationActive={false} />
          <Line dataKey="prevu" stroke={g.palette[0]} strokeWidth={2} strokeDasharray="5 4" dot={false} name="prevu" connectNulls={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
      {a.note && <p className="note">{a.note}</p>}
    </section>
  );
}

function Barres({ a }) {
  const g = couleursGraphes(useTheme().theme);
  const donnees = [...(a.donnees || [])].sort((x, y) => y.valeur - x.valeur);
  if (!donnees.length) return null;
  const hauteur = Math.max(160, donnees.length * 26 + 30);
  const largeurLabel = Math.min(240, Math.max(92, 6.4 * Math.max(...donnees.map((d) => String(d.label).length))));
  return (
    <section className="carte">
      <h3>{a.titre}</h3>
      <ResponsiveContainer width="100%" height={hauteur}>
        <BarChart data={donnees} layout="vertical" margin={{ left: 8, right: 40, top: 4, bottom: 4 }}>
          <XAxis type="number" scale={a.echelle === "log" ? "log" : "auto"} domain={a.echelle === "log" ? [1, "auto"] : [0, "auto"]}
                 allowDataOverflow tickFormatter={(v) => formater(v, "nombre")} fontSize={11} stroke={g.axe} />
          <YAxis type="category" dataKey="label" width={largeurLabel} fontSize={11} stroke={g.axe} />
          <Tooltip {...styleInfobulle(g)} formatter={(v) => formater(v, "nombre")}
                   labelFormatter={(l) => donnees.find((d) => d.label === l)?.description || l} />
          <Bar dataKey="valeur" radius={[0, 4, 4, 0]} label={{ position: "right", fontSize: 11, fill: g.axe, formatter: (v) => formater(v, "nombre") }}>
            {donnees.map((d, i) => <Cell key={i} fill={g.tons[d.ton] || g.tons.info} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      {a.echelle === "log" && <p className="note">Echelle logarithmique : les écarts de volume entre règles sont très importants.</p>}
    </section>
  );
}

function Valeur({ v, format, unite }) {
  const { tracer } = useTrace();
  if (v === null || v === undefined || v === "") return <span className="vide">-</span>;
  if (format === "kpi") return <>{formaterKpi(v, unite)}</>;
  if (format === "badge") return <span className={`badge badge-${String(v).toLowerCase()}`}>{v}</span>;
  if (format === "json" && typeof v === "object") {
    return (
      <span className="json">
        {Object.entries(v).map(([k, x]) => (
          <span key={k} className="paire"><b>{k.replaceAll("_", " ")}</b> {typeof x === "object" ? JSON.stringify(x) : formater(x, /^\d{4}-\d\d-\d\dT/.test(x) ? "datetime" : undefined)}</span>
        ))}
      </span>
    );
  }
  if (format === "trace") {
    const c = cibleTrace(v);
    if (c) return <button className="lien-trace" onClick={() => tracer(c.type, c.id)} title={`Tracer ${c.type} ${c.id}`}>{v}</button>;
  }
  return <>{formater(v, format)}</>;
}

function Tableau({ a, onAction }) {
  const [tri, setTri] = useState(null);
  const [filtre, setFiltre] = useState("");
  const lignes = useMemo(() => {
    let l = a.lignes || [];
    if (filtre) {
      const f = filtre.toLowerCase();
      l = l.filter((r) => JSON.stringify(r).toLowerCase().includes(f));
    }
    if (tri) {
      l = [...l].sort((x, y) => {
        const vx = x[tri.cle], vy = y[tri.cle];
        if (vx === vy) return 0;
        if (vx === null || vx === undefined) return 1;
        if (vy === null || vy === undefined) return -1;
        return (vx > vy ? 1 : -1) * (tri.sens);
      });
    }
    return l;
  }, [a.lignes, tri, filtre]);
  if (!a.lignes?.length) {
    return <section className="carte"><h3>{a.titre}</h3><p className="note">Aucune donnée.</p></section>;
  }
  const cliquable = a.action && onAction;
  return (
    <section className="carte">
      <div className="entete-carte">
        <h3>{a.titre}</h3>
        {a.lignes.length > 8 && (
          <input className="filtre" placeholder="Filtrer..." value={filtre} onChange={(e) => setFiltre(e.target.value)} />
        )}
      </div>
      <div className="tableau-defile">
        <table>
          <thead>
            <tr>
              {a.colonnes.map((c) => (
                <th key={c.cle} className={c.format === "nombre" || c.format === "kpi" ? "nombre" : ""}
                    onClick={() => setTri((t) => ({ cle: c.cle, sens: t?.cle === c.cle ? -t.sens : 1 }))}>
                  {c.label}{tri?.cle === c.cle ? (tri.sens > 0 ? " ▲" : " ▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {lignes.map((r, i) => (
              <tr key={i} className={cliquable ? "cliquable" : ""}
                  onClick={cliquable ? () => onAction(a.action.type, r[a.action.cle]) : undefined}>
                {a.colonnes.map((c) => (
                  <td key={c.cle} className={`${c.format === "nombre" || c.format === "kpi" ? "nombre" : ""} ${c.ton && r[c.ton] ? `texte-${r[c.ton]}` : ""}`}>
                    <Valeur v={r[c.cle]} format={c.format} unite={c.unite} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cliquable && <p className="note">Cliquez sur une ligne pour voir le détail.</p>}
    </section>
  );
}

function Chronologie({ a }) {
  if (!a.etapes?.length) return null;
  return (
    <section className="carte">
      <h3>{a.titre}</h3>
      <ol className="chronologie">
        {a.etapes.map((e, i) => (
          <li key={i}>
            <span className="etape">{e.label}</span>
            <span className="quand">{formater(e.date, "datetime")}</span>
            {e.source && <span className="source">{e.source}</span>}
          </li>
        ))}
      </ol>
    </section>
  );
}
