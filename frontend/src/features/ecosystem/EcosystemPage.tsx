import { Glyph } from '../../components/Glyph'

const capabilities = [
  { title: 'Skills', icon: 'spark' as const, description: 'Repeatable procedures that guide how an agent works with a project.', detail: 'Discovery and installation are planned.' },
  { title: 'Plugins', icon: 'orbit' as const, description: 'Connections to external services, tools, and workflows.', detail: 'No plugin catalog is connected yet.' },
  { title: 'Tools', icon: 'terminal' as const, description: 'Filesystem, validation, runtime, and preview operations built into the agent.', detail: 'Backend capabilities exist; public discovery does not.' },
  { title: 'Documentation', icon: 'book' as const, description: 'Project-aware Markdown guides and reference material.', detail: 'Search and versioned publishing are planned.' },
]

export function EcosystemPage() {
  return <main className="interior-page ecosystem-page">
    <div className="eyebrow">The wider workshop</div>
    <div className="interior-heading"><div><h1>Room for more ways to build.</h1><p>Redstone's ecosystem has a place in the product before it has a marketplace. These are categories, not pretend installs.</p></div><div className="ecosystem-emblem" aria-hidden="true"><Glyph name="orbit" /></div></div>
    <div className="section-bar"><span>Explore the structure</span><span>Catalog integration comes later</span></div>
    <div className="ecosystem-grid">{capabilities.map(item => <article key={item.title} className="ecosystem-entry"><div className="ecosystem-icon"><Glyph name={item.icon}/></div><div><h2>{item.title}</h2><p>{item.description}</p><small>{item.detail}</small></div></article>)}</div>
    <div className="ecosystem-outro"><strong>Nothing to install yet.</strong><p>This page holds the map. Working discovery, permissions, and real catalog entries will arrive with backend contracts.</p></div>
  </main>
}
