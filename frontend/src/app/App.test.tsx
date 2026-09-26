import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { App } from './App'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => { window.history.replaceState({}, '', '/'); vi.stubGlobal('fetch', vi.fn(async () => json({ status: 'ok', runtime: { provider: 'docker', available: true, isolated: true } }))) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Redstone foundation', () => {
  it('keeps disconnected workspace useful without inventing project data', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('network') }))
    render(<App />)
    expect(screen.getByRole('heading', { name: /start with the messy version/i })).toBeTruthy()
    expect(screen.getByRole('img', { name: /original installation of solid terracotta/i }).getAttribute('src')).toBe('/art/solid-studio-hero.jpg')
    expect(screen.getByRole('img', { name: /original sculpture of terracotta/i }).getAttribute('src')).toBe('/art/solid-studio-detail.jpg')
    expect(screen.getByRole('heading', { name: /from a thought to something you can inspect/i })).toBeTruthy()
    expect(screen.getByRole('link', { name: /open the workbench/i }).getAttribute('href')).toBe('#workbench')
    await screen.findByText(/API offline/i)
    expect(screen.queryByText(/preview ready/i)).toBeNull()
  })

  it('creates real project and sends request to that project only', async () => {
    const calls: { path: string; body?: string }[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
      calls.push({ path, body: init?.body as string | undefined })
      if (path === '/api/health') return json({ status: 'ok', runtime: { provider: 'docker', available: true, isolated: true } })
      if (path === '/api/projects') return json({ project_id: 'prj_test', workspace_id: 'ws_private', status: 'created' })
      if (path === '/api/projects/prj_test/agent') return json({ task_id: 'task_test', status: 'completed' })
      if (path === '/api/agent/tasks/task_test') return json({ task_id: 'task_test', project_id: 'prj_test', status: 'completed', current_step: 'completion', iterations_used: 1, changeset_id: null, error: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' })
      if (path === '/api/agent/tasks/task_test/events') return json({ events: [{ id: 'evt_test', type: 'agent.completed', project_id: 'prj_test', task_id: 'task_test', payload: {}, created_at: '2026-01-01T00:00:00Z' }] })
      return json({}, 404)
    }))
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: 'Project' }))
    await user.type(screen.getByLabelText('Project name'), 'Atlas portfolio')
    await user.click(screen.getByRole('button', { name: /create project/i }))
    await screen.findByText('Atlas portfolio')
    await user.click(screen.getByRole('button', { name: 'Agent' }))
    await user.type(screen.getByLabelText('Describe a change'), 'Build a portfolio')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    await screen.findByText('agent / completed')
    expect(calls.find(call => call.path.endsWith('/agent'))?.body).toBe(JSON.stringify({ message: 'Build a portfolio' }))
    expect(calls.some(call => call.path.includes('ws_private'))).toBe(false)
    expect(calls.some(call => call.body?.includes('credential'))).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Preview' }))
    await user.click(screen.getByRole('button', { name: 'Refresh runtime and preview status' }))
    await waitFor(() => expect(calls.some(call => call.path === '/api/projects/prj_test/runtime')).toBe(true))
    expect(calls.some(call => call.path === '/api/projects/prj_test/preview')).toBe(true)
    expect(screen.getByText('Not started')).toBeTruthy()
  })

  it('shows safe error without rendering upstream response text', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path: string) => path === '/api/health' ? json({ status: 'ok', runtime: { provider: 'none', available: false, isolated: false } }) : json({ error: 'SECRET: upstream traceback' }, 500)))
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: 'Project' }))
    await user.type(screen.getByLabelText('Project name'), 'Broken')
    await user.click(screen.getByRole('button', { name: /create project/i }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Request failed (500)'))
    expect(screen.queryByText(/SECRET/)).toBeNull()
  })

  it('shows ecosystem, legal notice, and a recoverable custom 404 route', async () => {
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('link', { name: 'Ecosystem' }))
    expect(window.location.pathname).toBe('/ecosystem')
    expect(screen.getByRole('heading', { name: 'Skills' })).toBeTruthy()
    expect(screen.getByText(/Nothing to install yet/)).toBeTruthy()
    await user.click(screen.getByRole('link', { name: 'Privacy & legal' }))
    expect(screen.getByRole('heading', { name: 'Privacy note' })).toBeTruthy()
    window.history.pushState({}, '', '/unmapped-route')
    window.dispatchEvent(new PopStateEvent('popstate'))
    expect(await screen.findByRole('heading', { name: /isn't on the map/i })).toBeTruthy()
    await user.click(screen.getByRole('link', { name: 'Return to workspace' }))
    expect(window.location.pathname).toBe('/')
  }, 15000)

  it('reads a text idea into draft locally without submitting it', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn(async (path: string) => path === '/api/health'
      ? json({ status: 'ok', runtime: { provider: 'none', available: false, isolated: false } })
      : json({}, 404))
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    const file = new File(['Build a garden planner'], 'idea.md', { type: 'text/markdown' })
    Object.defineProperty(file, 'text', { value: async () => 'Build a garden planner' })
    await user.upload(screen.getByLabelText('Add idea file'), file)
    expect(await screen.findByDisplayValue('Build a garden planner')).toBeTruthy()
    expect(fetchMock.mock.calls.some(call => String(call[0]).includes('/agent'))).toBe(false)
  }, 15000)
})
