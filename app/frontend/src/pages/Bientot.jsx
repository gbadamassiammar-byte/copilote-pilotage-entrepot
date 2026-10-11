// Pages des couches suivantes du projet, affichées pour montrer la feuille de route.
export default function Bientot({ titre, couche, contenu }) {
  return (
    <div className="page">
      <header className="entete-page"><div><h1>{titre}</h1><p className="note">Prévu à la couche {couche} du projet.</p></div></header>
      <section className="carte bientot">
        <h3>Ce que contiendra cette page</h3>
        <ul>{contenu.map((c) => <li key={c}>{c}</li>)}</ul>
        <p className="note">L'agent recevra en même temps les outils correspondants : les mêmes données seront accessibles par le chat et par cette page.</p>
      </section>
    </div>
  );
}
