import { Glyph } from '../../components/Glyph'

const questions = [
  ['Where do I start?', 'Create a project in Workspace, then describe what you want the agent to change. The request uses your current project.'],
  ['Why is preview empty?', 'The current project-creation endpoint makes a static project. Live preview needs a supported dev-server project, an isolated runtime, and gateway framing permission.'],
  ['Can I add an idea file?', 'Yes. Choose a small .txt or .md file in the Agent panel. Its text enters the draft locally; nothing is sent until you submit the request.'],
  ['Where are keys and providers?', 'The provider page maps supported gateway adapters and future local connections. Credential entry is intentionally not available in this foundation.'],
]

export function HelpPage() {
  return <main className="interior-page help-page">
    <div className="eyebrow">Field notes</div>
    <div className="interior-heading"><div><h1>A little help for the journey.</h1><p>Short answers about what works today and what still needs building.</p></div></div>
    <blockquote className="help-quote"><p>“Make the thing. Look at what it became. Then make it better.”</p><cite>Redstone field note</cite></blockquote>
    <div className="section-bar"><span>Common questions</span><span>Foundation edition</span></div>
    <div className="help-questions">{questions.map(([question, answer]) => <article key={question}><h2>{question}</h2><p>{answer}</p></article>)}</div>
    <div className="help-contact"><div><h2>Still stuck?</h2><p>Report a reproducible issue or ask a question in the public repository. Never include API keys or private project files.</p></div><a href="https://github.com/BRGOVIND/bashasub/issues" target="_blank" rel="noopener noreferrer">Visit GitHub issues <Glyph name="external"/></a></div>
  </main>
}
