import { useState } from 'react'
import type { FormEvent } from 'react'
import type { Health, Preview, Project, Runtime, Task, TaskEvent } from '../../api/types'
import { Glyph } from '../../components/Glyph'
import { ObservatoryArt } from './ObservatoryArt'
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
    <div className="panel-kicker"><span>01</span> PROJECT INDEX</div>
    <div className="panel-title"><Glyph name="folder"/><h2 id="project-heading">Your project</h2></div>
    {project ? <><div className="project-entry"><span className="project-indicator"/><div><strong>{projectName}</strong><small>{project.project_id}</small></div></div><div className="panel-divider"/><div className="panel-kicker subtle">FILES / WORKSPACE</div><div className="unavailable-surface"><Glyph name="folder"/><p>File explorer waiting for workspace API.</p><small>No file list endpoint exists yet.</small></div></> : <><p className="panel-intro">Begin with a real workspace. Each request continues on same project.</p><form className="project-form" onSubmit={submit}><label htmlFor="project-name">Project name</label><input id="project-name" maxLength={200} placeholder="e.g. Atlas portfolio" value={name} onChange={event => setName(event.target.value)}/><button className="primary-button" disabled={creating || !name.trim()} type="submit"><Glyph name="plus"/>{creating ? 'Creating…' : 'Create project'}</button></form>{error && <p className="inline-error" role="alert">{error}</p>}<div className="panel-divider"/><div className="unavailable-surface"><Glyph name="folder"/><p>Files appear here when project file API lands.</p></div></>}
    <div className="project-panel-bottom"><span>LOCAL SESSION</span><span>NO PERSISTENT LIBRARY YET</span></div>
  </section>
}

function AgentPanel({ project, task, events, taskBusy, taskError, onTask }: Pick<Props, 'project' | 'task' | 'events' | 'taskBusy' | 'taskError' | 'onTask'>) {
  const [message, setMessage] = useState('')
  const submit = async (event: FormEvent) => { event.preventDefault(); const text = message.trim(); if (!text) return; await onTask(text); setMessage('') }
  return <section className="agent-panel" aria-labelledby="agent-heading"><div className="panel-kicker"><span>02</span> ENGINEERING CHANNEL</div><div className="agent-header"><div><h2 id="agent-heading">Build with an agent.</h2><p>One request at a time. One real project.</p></div><Glyph name="spark"/></div>
    <div className="agent-feed" aria-live="polite">{!task && !taskBusy ? <div className="agent-empty"><div className="agent-orbit"><Glyph name="spark"/></div><h3>Tell us what you want to make.</h3><p>Agent can inspect and change project files. Its activity appears here only when backend reports it.</p><div className="agent-note">Project-bound · Real files · No simulated steps</div></div> : <><div className="task-card"><small>{taskBusy ? 'REQUEST IN PROGRESS' : `TASK ${task?.task_id ?? ''}`}</small><h3>{taskBusy ? 'Working on your request…' : task?.status.replaceAll('_', ' ')}</h3><p>{taskBusy ? 'Current API returns only after task finishes. Live progress is not available yet.' : task?.current_step ? `Last step: ${task.current_step}` : 'Task result recorded by backend.'}</p></div>{events.length > 0 && <ol className="event-list">{events.map(event => <li key={event.id}><span className="event-dot"/><div><strong>{event.type.replaceAll('.', ' / ')}</strong><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleTimeString()}</time></div></li>)}</ol>}</>}
    </div>
    {taskError && <p className="inline-error" role="alert">{taskError}</p>}
    <form className="agent-composer" onSubmit={submit}><label htmlFor="agent-message">Describe a change</label><textarea id="agent-message" rows={3} placeholder={project ? 'What should we build next?' : 'Create a project to begin…'} value={message} onChange={event => setMessage(event.target.value)} disabled={!project || taskBusy} onKeyDown={event => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }}/><div><span>⌘ / Ctrl + Enter</span><button className="send-button" type="submit" disabled={!project || !message.trim() || taskBusy} aria-label="Send request"><Glyph name="arrow"/></button></div></form>
  </section>
}

function safePreviewUrl(value: string | null): string | null {
  if (!value) return null
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && url.origin !== window.location.origin && !url.username && !url.password ? url.href : null } catch { return null }
}

function PreviewStage({ preview, project, health, runtime, onRefreshStatus, statusBusy, statusError }: Pick<Props, 'preview' | 'project' | 'health' | 'runtime' | 'onRefreshStatus' | 'statusBusy' | 'statusError'>) {
  const url = safePreviewUrl(preview?.status === 'ready' ? preview.url : null)
  return <section className="preview-stage" aria-labelledby="preview-heading"><div className="stage-top"><div><span className="stage-overline">03 / THE ARTIFACT</span><h2 id="preview-heading">Live preview</h2></div><div className="stage-actions"><span className="stage-status"><i/>{url ? 'READY' : 'AWAITING APP'}</span>{project && <button className="status-refresh" type="button" onClick={onRefreshStatus} disabled={statusBusy} aria-label="Refresh runtime and preview status"><Glyph name="refresh"/></button>}</div></div><div className="preview-window"><div className="browser-chrome"><span className="chrome-dots"><i/><i/><i/></span><span className="address-bar">{url ? 'Project preview · isolated origin' : 'YOUR APPLICATION WILL APPEAR HERE'}</span><span className="chrome-mark">R</span></div>{url ? <iframe title="Project live preview" src={url} sandbox="allow-scripts allow-forms allow-same-origin" referrerPolicy="no-referrer"/> : <div className="preview-illustration"><ObservatoryArt/><div className="illustration-caption"><span>PLATE 001 — THE DEVELOPMENT OBSERVATORY</span><p>A world waiting to be built.</p></div></div>}</div><div className="stage-bottom"><div className="stage-reading"><small>PREVIEW SIGNAL</small><strong>{url ? 'Live application' : project ? 'Not started' : 'No project yet'}</strong></div><div className="stage-reading"><small>RUNTIME</small><strong>{runtime?.state ?? (health?.runtime.isolated ? 'Isolated host ready' : 'Unavailable')}</strong></div><div className="stage-reading"><small>BUILD</small><strong>Not reported</strong></div></div>{statusError && <p className="inline-error" role="alert">{statusError}</p>}<p className="stage-footnote">{preview?.status === 'ready' && !url ? 'Preview URL rejected or embedding blocked by gateway policy.' : project ? 'New projects use a static framework. Preview needs a supported dev-server project and explicit gateway frame permission.' : 'Create a project to start building. Preview remains empty until backend starts a real app.'}</p></section>
}

export function Workspace(props: Props) {
  const [surface, setSurface] = useState<Surface>('preview')
  return <main className="workspace-page"><div className="workspace-masthead"><div><div className="eyebrow">DEVELOPMENT OBSERVATORY / ISSUE 001</div><h1>Make something <em>remarkable.</em></h1><p>Real projects. Real files. Your AI engineering partner at the controls.</p></div><div className="masthead-stamp" aria-hidden="true"><span>R</span><small>EST. TOMORROW</small></div></div><div className="workspace-meta"><span><i className={props.health?.runtime.isolated ? 'signal-on' : ''}/>{props.connectionError ? 'API OFFLINE' : props.health ? 'API CONNECTED' : 'CONNECTING'}</span><span>WORKSPACE / {props.project?.project_id ?? 'UNASSIGNED'}</span><span>LOCAL DEVELOPMENT</span></div>
    <div className="surface-tabs" role="group" aria-label="Workspace panels"><button className={surface === 'preview' ? 'active' : ''} onClick={() => setSurface('preview')}>Preview</button><button className={surface === 'agent' ? 'active' : ''} onClick={() => setSurface('agent')}>Agent</button><button className={surface === 'files' ? 'active' : ''} onClick={() => setSurface('files')}>Project</button></div>
    <div className={`workspace-grid surface-${surface}`}><ProjectPanel project={props.project} projectName={props.projectName} onCreate={props.onCreate} creating={props.creating} error={props.createError}/><AgentPanel project={props.project} task={props.task} events={props.events} taskBusy={props.taskBusy} taskError={props.taskError} onTask={props.onTask}/><PreviewStage project={props.project} preview={props.preview} health={props.health} runtime={props.runtime} onRefreshStatus={props.onRefreshStatus} statusBusy={props.statusBusy} statusError={props.statusError}/></div>
    {props.connectionError && <div className="connection-banner" role="status"><Glyph name="orbit"/><span>{props.connectionError} Interface remains available; backend operations require connection.</span></div>}
  </main>
}
