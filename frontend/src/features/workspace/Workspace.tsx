import { useRef, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import type { Health, Preview, Project, Runtime, Task, TaskEvent } from '../../api/types'
import { Glyph } from '../../components/Glyph'
import './workspace.css'

type Surface = 'preview' | 'agent' | 'files'

interface Props {
  health: Health | null
  connectionError: string | null
  project: Project | null
  projectName: string
  creating: boolean
  createError: string | null
  onCreate: (name: string) => Promise<void>
  task: Task | null
  events: TaskEvent[]
  taskBusy: boolean
  taskError: string | null
  onTask: (message: string) => Promise<void>
  preview: Preview | null
  runtime: Runtime | null
  onRefreshStatus: () => Promise<void>
  statusBusy: boolean
  statusError: string | null
}

function ProjectPanel({ project, projectName, onCreate, creating, error }: Pick<Props, 'project' | 'projectName' | 'onCreate' | 'creating'> & { error: string | null }) {
  const [name, setName] = useState('')
  const submit = async (event: FormEvent) => { event.preventDefault(); if (name.trim()) await onCreate(name.trim()) }
  return <section className="project-panel" aria-labelledby="project-heading">
    <div className="panel-kicker">Project index</div>
    <div className="panel-title"><Glyph name="folder"/><h2 id="project-heading">Your project</h2></div>
    {project ? <><div className="project-entry"><div><strong>{projectName}</strong><small>{project.project_id}</small></div></div><div className="panel-divider"/><div className="panel-kicker subtle">Files & workspace</div><div className="unavailable-surface"><Glyph name="folder"/><p>File explorer waiting for workspace API.</p><small>No file list endpoint exists yet.</small></div></> : <><p className="panel-intro">Begin with a real workspace. Each request continues on same project.</p><form className="project-form" onSubmit={submit}><label htmlFor="project-name">Project name</label><input id="project-name" maxLength={200} placeholder="e.g. Atlas portfolio" value={name} onChange={event => setName(event.target.value)}/><button className="primary-button" disabled={creating || !name.trim()} type="submit"><Glyph name="plus"/>{creating ? 'Creating…' : 'Create project'}</button></form>{error && <p className="inline-error" role="alert">{error}</p>}<div className="panel-divider"/><div className="unavailable-surface"><Glyph name="folder"/><p>Files appear here when project file API lands.</p></div></>}
    <div className="project-panel-bottom"><span>Local session</span><span>No saved library yet</span></div>
  </section>
}

function AgentPanel({ project, task, events, taskBusy, taskError, onTask }: Pick<Props, 'project' | 'task' | 'events' | 'taskBusy' | 'taskError' | 'onTask'>) {
  const [message, setMessage] = useState('')
  const [ideaError, setIdeaError] = useState<string | null>(null)
  const ideaInput = useRef<HTMLInputElement>(null)
  const submit = async (event: FormEvent) => { event.preventDefault(); const text = message.trim(); if (!text) return; await onTask(text); setMessage('') }
  const readIdea = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    setIdeaError(null)
    if (!/\.(txt|md)$/i.test(file.name) || file.size > 16 * 1024) {
      setIdeaError('Choose a .txt or .md file under 16 KB.')
    } else {
      try {
        const text = await file.text()
        if (text.length > 20_000) setIdeaError('Idea is too long for one agent request.')
        else setMessage(text)
      } catch { setIdeaError('Could not read that file locally.') }
    }
    if (ideaInput.current) ideaInput.current.value = ''
  }
  return <section className="agent-panel" aria-labelledby="agent-heading"><div className="panel-kicker">Engineering channel</div><div className="agent-header"><div><h2 id="agent-heading">Build with an agent.</h2><p>One request at a time. One real project.</p></div><Glyph name="spark"/></div>
    <div className="agent-feed" aria-live="polite">{!task && !taskBusy ? <div className="agent-empty"><div className="agent-orbit"><Glyph name="spark"/></div><h3>Tell us what you want to make.</h3><p>Agent can inspect and change project files. Its activity appears here only when backend reports it.</p><div className="agent-note">Project-bound · Real files · No simulated steps</div></div> : <><div className="task-card"><small>{taskBusy ? 'Request in progress' : `Task ${task?.task_id ?? ''}`}</small><h3>{taskBusy ? 'Working on your request…' : task?.status.replaceAll('_', ' ')}</h3><p>{taskBusy ? 'Current API returns only after task finishes. Live progress is not available yet.' : task?.current_step ? `Last step: ${task.current_step}` : 'Task result recorded by backend.'}</p></div>{events.length > 0 && <ol className="event-list">{events.map(event => <li key={event.id}><div><strong>{event.type.replaceAll('.', ' / ')}</strong><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time></div></li>)}</ol>}</>}
    </div>
    {taskError && <p className="inline-error" role="alert">{taskError}</p>}
    <div className="idea-intake"><div><strong>Have an idea written down?</strong><small>Choose text or Markdown. It fills your draft locally; nothing is sent yet.</small></div><input ref={ideaInput} id="idea-file" type="file" accept=".txt,.md,text/plain,text/markdown" onChange={readIdea} disabled={taskBusy}/><label htmlFor="idea-file">Add idea file</label>{ideaError && <p role="alert">{ideaError}</p>}</div>
    <form className="agent-composer" onSubmit={submit}><label htmlFor="agent-message">Describe a change</label><textarea id="agent-message" rows={3} placeholder={project ? 'What should we build next?' : 'Draft an idea now, then create a project to send it.'} value={message} onChange={event => setMessage(event.target.value)} disabled={taskBusy} onKeyDown={event => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }}/><div><span>{project ? '⌘ / Ctrl + Enter' : 'Create a project before sending'}</span><button className="send-button" type="submit" disabled={!project || !message.trim() || taskBusy} aria-label="Send request"><Glyph name="arrow"/></button></div></form>
  </section>
}

function safePreviewUrl(value: string | null): string | null {
  if (!value) return null
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && url.origin !== window.location.origin && !url.username && !url.password ? url.href : null } catch { return null }
}

function PreviewStage({ preview, project, health, runtime, onRefreshStatus, statusBusy, statusError }: Pick<Props, 'preview' | 'project' | 'health' | 'runtime' | 'onRefreshStatus' | 'statusBusy' | 'statusError'>) {
  const url = safePreviewUrl(preview?.status === 'ready' ? preview.url : null)
  return <section className="preview-stage" aria-labelledby="preview-heading"><div className="stage-top"><div><span className="stage-overline">The artifact</span><h2 id="preview-heading">Live preview</h2></div><div className="stage-actions"><span className="stage-status">{url ? 'Ready' : 'Awaiting app'}</span>{project && <button className="status-refresh" type="button" onClick={onRefreshStatus} disabled={statusBusy} aria-label="Refresh runtime and preview status"><Glyph name="refresh"/></button>}</div></div><div className="preview-window"><div className="browser-chrome"><span className="chrome-rule"/><span className="address-bar">{url ? 'Project preview · isolated origin' : 'Your application will appear here'}</span><span className="chrome-mark">R</span></div>{url ? <iframe title="Project live preview" src={url} sandbox="allow-scripts allow-forms allow-same-origin" referrerPolicy="no-referrer"/> : <div className="preview-illustration"><img src="/art/solid-studio-detail.jpg" alt="Original sculpture of terracotta, saffron, and teal forms in a sunlit room" loading="lazy" decoding="async"/><div className="illustration-caption"><span>Preview</span><p>Your app will appear here.</p></div></div>}</div><div className="stage-bottom"><div className="stage-reading"><small>Preview signal</small><strong>{url ? 'Live application' : project ? 'Not started' : 'No project yet'}</strong></div><div className="stage-reading"><small>Runtime</small><strong>{runtime?.state ?? (health?.runtime.isolated ? 'Isolated host ready' : 'Unavailable')}</strong></div><div className="stage-reading"><small>Build</small><strong>Not reported</strong></div></div>{statusError && <p className="inline-error" role="alert">{statusError}</p>}<p className="stage-footnote">{preview?.status === 'ready' && !url ? 'Preview URL rejected or embedding blocked by gateway policy.' : project ? 'New projects use a static framework. Preview needs a supported dev-server project and explicit gateway frame permission.' : 'Create a project to start building. Preview remains empty until backend starts a real app.'}</p></section>
}

export function Workspace(props: Props) {
  const [surface, setSurface] = useState<Surface>('preview')
  return <main className="workspace-page">
    <section className="landing-hero" aria-labelledby="landing-heading">
      <figure className="landing-visual">
        <img src="/art/solid-studio-hero.jpg" alt="Original installation of solid terracotta, saffron, and teal sculptures in a sunlit room" fetchPriority="high" decoding="async"/>
      </figure>
      <div className="workspace-masthead"><div>
        <div className="eyebrow">Development observatory</div>
        <h1 id="landing-heading">Start with the <em>messy version.</em></h1>
        <p>A note, a rough brief, a half-built idea. Start a project, tell the agent what to change, and see what actually runs.</p>
        <div className="landing-actions">
          <a className="landing-primary" href="#workbench">Open the workbench <Glyph name="arrow"/></a>
          <a className="landing-secondary" href="#approach">See how it works</a>
        </div>
      </div></div>
    </section>
    <section className="landing-approach" id="approach" aria-labelledby="approach-heading">
      <div className="landing-approach-intro">
        <div className="eyebrow">A clear path in</div>
        <h2 id="approach-heading">From a thought to something you can inspect.</h2>
        <p>Project, agent, and preview stay connected. Redstone shows work as ready only when the backend reports it.</p>
      </div>
      <div className="landing-steps">
        <article><Glyph name="folder"/><h3>Start a project</h3><p>Give your idea a home. Every request stays tied to that project.</p></article>
        <article><Glyph name="spark"/><h3>Describe the change</h3><p>Write a request or bring a small text brief. Review the draft before sending.</p></article>
        <article><Glyph name="orbit"/><h3>Inspect the result</h3><p>See a live app here when a supported runtime and preview are available.</p></article>
      </div>
    </section>
    <section className="workbench" id="workbench" aria-labelledby="workbench-heading">
      <div className="workbench-heading"><div><div className="eyebrow">Your workbench</div><h2 id="workbench-heading">Make the next change.</h2></div><p>Project, agent, and preview stay together here. Nothing is simulated.</p></div>
      <div className="workspace-meta"><span>{props.connectionError ? 'API offline' : props.health ? 'API connected' : 'Connecting'}</span><span>Workspace / {props.project?.project_id ?? 'unassigned'}</span><span>Local development</span></div>
      <div className="surface-tabs" role="group" aria-label="Workspace panels"><button className={surface === 'preview' ? 'active' : ''} onClick={() => setSurface('preview')}>Preview</button><button className={surface === 'agent' ? 'active' : ''} onClick={() => setSurface('agent')}>Agent</button><button className={surface === 'files' ? 'active' : ''} onClick={() => setSurface('files')}>Project</button></div>
      <div className={'workspace-grid surface-' + surface}><ProjectPanel project={props.project} projectName={props.projectName} onCreate={props.onCreate} creating={props.creating} error={props.createError}/><AgentPanel project={props.project} task={props.task} events={props.events} taskBusy={props.taskBusy} taskError={props.taskError} onTask={props.onTask}/><PreviewStage project={props.project} preview={props.preview} health={props.health} runtime={props.runtime} onRefreshStatus={props.onRefreshStatus} statusBusy={props.statusBusy} statusError={props.statusError}/></div>
      {props.connectionError && <div className="connection-banner" role="status"><Glyph name="orbit"/><span>{props.connectionError} Interface remains available; backend operations require connection.</span></div>}
    </section>
  </main>
}
