import { Glyph } from '../../components/Glyph'
import { providers } from '../../state/providers'

export function ProviderPage() {
  return <main className="interior-page provider-page">
    <div className="eyebrow">02 / THE POWER SOURCE</div>
    <div className="interior-heading"><div><h1>Choose your intelligence.</h1><p>Redstone is designed for your infrastructure, your models, your terms.</p></div><div className="sun-dial" aria-hidden="true"><span/></div></div>
    <div className="section-bar"><span>Provider architecture</span><span>Connections are not configurable in this frontend yet</span></div>
    <div className="provider-grid">{providers.map((provider, index) => <article className="provider-row" key={provider.id}>
      <div className="provider-index">0{index + 1}</div><div className="provider-mark"><Glyph name={provider.kind === 'local' ? 'orbit' : 'spark'}/></div>
      <div className="provider-copy"><h2>{provider.name}</h2><p>{provider.note}</p><div className="provider-modes">{provider.modes.map(mode => <span key={mode}>{mode === 'byok' ? 'Bring your key' : mode === 'local' ? 'On your machine' : 'Server managed'}</span>)}</div></div>
      <span className="availability">{provider.availability === 'backend-supported' ? 'Gateway adapter' : 'Planned'}</span>
    </article>)}</div>
    <aside className="security-note"><Glyph name="orbit"/><div><strong>Keys stay out of this interface.</strong><p>No credential is stored or requested here. A one-request BYOK handoff and local connection flow need dedicated security review before controls are enabled.</p></div></aside>
  </main>
}
