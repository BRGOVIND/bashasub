import { Glyph } from '../../components/Glyph'

const sections = [
  ['01', 'Workspace', 'One project, iterative work. Real files and changes will live here when API access lands.'],
  ['02', 'Agent', 'Project-bound requests use the current synchronous agent endpoint. Task events come from the backend.'],
  ['03', 'Preview', 'Generated apps run on separate origins. Embedding requires explicit gateway permission.'],
  ['04', 'Ecosystem', 'Skills, tools, plugins, providers, models, and Markdown guides will share a discoverable catalog.'],
]

export function GuidePage() {
  return <main className="interior-page guide-page"><div className="eyebrow">03 / FIELD GUIDE</div><div className="interior-heading"><div><h1>Build with clear instruments.</h1><p>A map of the environment as it exists and as it grows.</p></div><Glyph name="book" className="guide-emblem"/></div><div className="section-bar"><span>System map</span><span>Foundation edition</span></div><div className="guide-grid">{sections.map(([number, title, body]) => <article key={number}><span>{number}</span><h2>{title}</h2><p>{body}</p></article>)}</div><div className="guide-footer"><Glyph name="terminal"/><p>Technical architecture lives in <code>docs/redstone/FRONTEND.md</code>. This catalog is a destination, not a simulated marketplace.</p></div></main>
}
