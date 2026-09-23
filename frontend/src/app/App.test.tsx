import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { App } from './App'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => { vi.stubGlobal('fetch', vi.fn(async () => json({ status: 'ok', runtime: { provider: 'docker', available: true, isolated: true } }))) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Redstone foundation', () => {
  it('keeps disconnected workspace useful without inventing project data', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('network') }))
    render(<App />)
    expect(screen.getByRole('heading', { name: /make something remarkable/i })).toBeTruthy()
    await screen.findByText(/API OFFLINE/)
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
})
