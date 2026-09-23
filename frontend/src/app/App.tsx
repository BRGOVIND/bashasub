import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { Health, Preview, Project, Runtime, Task, TaskEvent } from '../api/types'
import { Glyph } from '../components/Glyph'
import { GuidePage } from '../features/guide/GuidePage'
import { ProviderPage } from '../features/providers/ProviderPage'
import { Workspace } from '../features/workspace/Workspace'
import { CommandPalette } from './CommandPalette'

type Page = 'workspace' | 'providers' | 'guide'
const safeError = (error: unknown) => error instanceof ApiError ? error.message : 'Operation failed. Please retry.'

export function App() {
  const [page, setPage] = useState<Page>('workspace')
  const [commandOpen, setCommandOpen] = useState(false)
  const [health, setHealth] = useState<Health | null>(null)
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [project, setProject] = useState<Project | null>(null)
  const [projectName, setProjectName] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [task, setTask] = useState<Task | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [taskBusy, setTaskBusy] = useState(false)
  const [taskError, setTaskError] = useState<string | null>(null)
  const [runtime, setRuntime] = useState<Runtime | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [statusBusy, setStatusBusy] = useState(false)
  const [statusError, setStatusError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    api.health().then(data => { if (active) { setHealth(data); setConnectionError(null) } }).catch(error => { if (active) setConnectionError(safeError(error)) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); setCommandOpen(open => !open) }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const createProject = async (name: string) => {
    setCreating(true); setCreateError(null)
    try {
      const next = await api.createProject(name)
      setProject(next); setProjectName(name); setTask(null); setEvents([]); setRuntime(null); setPreview(null)
      setConnectionError(null)
    } catch (error) { setCreateError(safeError(error)) }
    finally { setCreating(false) }
  }

  const startTask = async (message: string) => {
    if (!project) return
    setTaskBusy(true); setTaskError(null); setEvents([]); setTask(null)
    try {
      const started = await api.startTask(project.project_id, message)
      const [result, history] = await Promise.all([api.task(started.task_id), api.taskEvents(started.task_id)])
      setTask(result); setEvents(history)
    } catch (error) { setTaskError(safeError(error)) }
    finally { setTaskBusy(false) }
  }

  const refreshStatus = async () => {
    if (!project) return
    setStatusBusy(true); setStatusError(null)
    const [runtimeResult, previewResult] = await Promise.allSettled([
      api.runtime(project.project_id), api.preview(project.project_id),
    ])
    const noResource = (error: unknown) => error instanceof ApiError && error.status === 404
    if (runtimeResult.status === 'fulfilled') setRuntime(runtimeResult.value)
    else if (noResource(runtimeResult.reason)) setRuntime(null)
    else setStatusError(safeError(runtimeResult.reason))
    if (previewResult.status === 'fulfilled') setPreview(previewResult.value)
    else if (noResource(previewResult.reason)) setPreview(null)
    else setStatusError(safeError(previewResult.reason))
    setStatusBusy(false)
  }

  const commands = [
    { id: 'workspace', label: 'Open workspace', group: 'Navigation', run: () => setPage('workspace') },
    { id: 'providers', label: 'Explore providers', group: 'Navigation', run: () => setPage('providers') },
    { id: 'guide', label: 'Read field guide', group: 'Navigation', run: () => setPage('guide') },
    { id: 'files', label: 'Browse project files', group: 'Coming with file API', run: () => {}, disabled: true },
    { id: 'plugins', label: 'Browse plugins', group: 'Future ecosystem', run: () => {}, disabled: true },
  ]

  return <div className="app-shell"><a className="skip-link" href="#main-content">Skip to content</a><header className="global-header"><button className="brand" onClick={() => setPage('workspace')} aria-label="Redstone home"><span className="brand-symbol"><span/></span><span>REDSTONE<small>DEVELOPMENT ENVIRONMENT</small></span></button><nav className="main-nav" aria-label="Main navigation"><button className={page === 'workspace' ? 'selected' : ''} onClick={() => setPage('workspace')} aria-current={page === 'workspace' ? 'page' : undefined}><Glyph name="grid"/>Workspace</button><button className={page === 'providers' ? 'selected' : ''} onClick={() => setPage('providers')} aria-current={page === 'providers' ? 'page' : undefined}><Glyph name="orbit"/>Providers</button><button className={page === 'guide' ? 'selected' : ''} onClick={() => setPage('guide')} aria-current={page === 'guide' ? 'page' : undefined}><Glyph name="book"/>Field guide</button></nav><div className="header-actions"><button className="search-trigger" aria-label="Open command menu" onClick={() => setCommandOpen(true)}><Glyph name="search"/><span>Command</span><kbd>⌘ K</kbd></button><span className="header-avatar" aria-label="Local session">R</span></div></header><div id="main-content">{page === 'workspace' ? <Workspace health={health} connectionError={connectionError} project={project} projectName={projectName} creating={creating} createError={createError} onCreate={createProject} task={task} events={events} taskBusy={taskBusy} taskError={taskError} onTask={startTask} preview={preview} runtime={runtime} onRefreshStatus={refreshStatus} statusBusy={statusBusy} statusError={statusError}/> : page === 'providers' ? <ProviderPage/> : <GuidePage/>}</div><footer className="global-footer"><span>REDSTONE / FIELD STATION 01</span><span>BUILD WHAT COMES NEXT.</span><span>FOUNDATION · NOT PRODUCTION READY</span></footer><CommandPalette open={commandOpen} onClose={() => setCommandOpen(false)} commands={commands}/></div>
}
