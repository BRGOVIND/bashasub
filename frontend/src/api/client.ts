import type { Health, Preview, Project, Runtime, Task, TaskEvent } from './types'

export class ApiError extends Error {
  constructor(readonly status: number, message: string) { super(message) }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
      cache: 'no-store',
    })
  } catch {
    throw new ApiError(0, 'Redstone API is unreachable. Start the local API and retry.')
  }
  if (!response.ok) {
    const message = response.status === 404
      ? 'Resource not found in this Redstone session.'
      : response.status === 409
        ? 'Resource is busy or cannot make that transition yet.'
        : response.status === 503
          ? 'Capability is unavailable on this runtime.'
          : `Request failed (${response.status}).`
    throw new ApiError(response.status, message)
  }
  return response.json() as Promise<T>
}

const projectPath = (id: string) => `/projects/${encodeURIComponent(id)}`
const taskPath = (id: string) => `/agent/tasks/${encodeURIComponent(id)}`

export const api = {
  health: () => request<Health>('/health'),
  createProject: (name: string) => request<Project>('/projects', {
    method: 'POST', body: JSON.stringify({ name }),
  }),
  startTask: (projectId: string, message: string) => request<Pick<Task, 'task_id' | 'status'>>(
    `${projectPath(projectId)}/agent`, { method: 'POST', body: JSON.stringify({ message }) },
  ),
  task: (id: string) => request<Task>(taskPath(id)),
  taskEvents: async (id: string) => (await request<{ events: TaskEvent[] }>(`${taskPath(id)}/events`)).events,
  runtime: (projectId: string) => request<Runtime>(`${projectPath(projectId)}/runtime`),
  startRuntime: (projectId: string) => request<Runtime>(`${projectPath(projectId)}/runtime/start`, { method: 'POST' }),
  stopRuntime: (projectId: string) => request<Runtime>(`${projectPath(projectId)}/runtime/stop`, { method: 'POST' }),
  preview: (projectId: string) => request<Preview>(`${projectPath(projectId)}/preview`),
  startPreview: (projectId: string) => request<Preview>(`${projectPath(projectId)}/preview`, { method: 'POST' }),
  stopPreview: (projectId: string) => request<Preview>(`${projectPath(projectId)}/preview/stop`, { method: 'POST' }),
}
