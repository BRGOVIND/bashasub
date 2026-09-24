import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { Health, Preview, Project, Runtime, Task, TaskEvent } from '../api/types'
import { Glyph } from '../components/Glyph'
import { BrandMark } from '../components/BrandMark'
import { EcosystemPage } from '../features/ecosystem/EcosystemPage'
import { HelpPage } from '../features/help/HelpPage'
import { LegalPage } from '../features/legal/LegalPage'
import { NotFoundPage } from '../features/not-found/NotFoundPage'
import { ProviderPage } from '../features/providers/ProviderPage'
import { Workspace } from '../features/workspace/Workspace'
import { CommandPalette } from './CommandPalette'

type Page = 'workspace' | 'providers' | 'ecosystem' | 'help' | 'legal' | 'not-found'
const paths: Record<Exclude<Page, 'not-found'>, string> = {
  workspace: '/', providers: '/providers', ecosystem: '/ecosystem', help: '/help', legal: '/legal',
}
const pageFromPath = (path: string): Page =>
  (Object.entries(paths).find(([, value]) => value === path)?.[0] as Page | undefined) ?? 'not-found'
const safeError = (error: unknown) => error instanceof ApiError ? error.message : 'Operation failed. Please retry.'

export function App() {
  const [page, setPage] = useState<Page>(() => pageFromPath(window.location.pathname))
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

  useEffect(() => {
    const onPopState = () => setPage(pageFromPath(window.location.pathname))
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const navigate = (next: Exclude<Page, 'not-found'>) => {
    window.history.pushState({}, '', paths[next])
    setPage(next)
  }

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
    { id: 'workspace', label: 'Open workspace', group: 'Navigation', run: () => navigate('workspace') },
    { id: 'providers', label: 'Explore providers', group: 'Navigation', run: () => navigate('providers') },
    { id: 'ecosystem', label: 'Explore skills and plugins', group: 'Navigation', run: () => navigate('ecosystem') },
    { id: 'help', label: 'Open help', group: 'Navigation', run: () => navigate('help') },
    { id: 'files', label: 'Browse project files', group: 'Coming with file API', run: () => {}, disabled: true },
    { id: 'plugins', label: 'Browse plugins', group: 'Future ecosystem', run: () => {}, disabled: true },
  ]

  const link = (target: Exclude<Page, 'not-found'>, label: string, icon?: 'grid' | 'orbit' | 'book' | 'spark') =>
    <a href={paths[target]} className={page === target ? 'selected' : ''} aria-current={page === target ? 'page' : undefined}
      onClick={event => { event.preventDefault(); navigate(target) }}>
      {icon && <Glyph name={icon}/>}<span>{label}</span>
    </a>

  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <header className="global-header">
      <a className="brand" href="/" aria-label="Redstone home" onClick={event => { event.preventDefault(); navigate('workspace') }}>
        <BrandMark/><span>REDSTONE<small>Development environment</small></span>
      </a>
      <nav className="main-nav" aria-label="Main navigation">
        {link('workspace', 'Workspace', 'grid')}
        {link('providers', 'Providers', 'orbit')}
        {link('ecosystem', 'Ecosystem', 'spark')}
        {link('help', 'Help', 'book')}
      </nav>
      <div className="header-actions"><button className="search-trigger" aria-label="Open command menu" onClick={() => setCommandOpen(true)}><Glyph name="search"/><span>Command</span><kbd>⌘ K</kbd></button></div>
    </header>
    <div id="main-content">
      {page === 'workspace' ? <Workspace health={health} connectionError={connectionError} project={project} projectName={projectName} creating={creating} createError={createError} onCreate={createProject} task={task} events={events} taskBusy={taskBusy} taskError={taskError} onTask={startTask} preview={preview} runtime={runtime} onRefreshStatus={refreshStatus} statusBusy={statusBusy} statusError={statusError}/>
        : page === 'providers' ? <ProviderPage/>
        : page === 'ecosystem' ? <EcosystemPage/>
        : page === 'help' ? <HelpPage/>
        : page === 'legal' ? <LegalPage/>
        : <NotFoundPage onHome={() => navigate('workspace')}/>}
    </div>
    <footer className="global-footer">
      <span>Redstone · Development preview</span>
      <span><a href="https://github.com/BRGOVIND" target="_blank" rel="noopener noreferrer">@BRGOVIND</a><a href="https://github.com/BRGOVIND/bashasub" target="_blank" rel="noopener noreferrer">GitHub</a><a href={paths.legal} onClick={event => { event.preventDefault(); navigate('legal') }}>Privacy & legal</a></span>
      <span>Not production ready</span>
    </footer>
    <CommandPalette open={commandOpen} onClose={() => setCommandOpen(false)} commands={commands}/>
  </div>
}
