export type Loadable<T> =
  | { state: 'idle' | 'loading'; data?: never }
  | { state: 'ready'; data: T }
  | { state: 'error'; data?: never; message: string }

export interface Project {
  project_id: string
  workspace_id: string
  status: string
}

export interface Task {
  task_id: string
  project_id: string
  status: string
  current_step: string | null
  iterations_used: number
  changeset_id: string | null
  error: { error_code: string; message: string } | null
  created_at: string
  updated_at: string
}

export interface TaskEvent {
  id: string
  type: string
  project_id: string | null
  task_id: string | null
  payload: Record<string, unknown>
  created_at: string
}

export interface Runtime {
  runtime_id: string
  project_id: string
  state: string
  framework: string
  last_error: { error_code: string; message: string } | null
  created_at: string
  last_activity: string
}

export interface Preview {
  preview_id: string
  project_id: string
  status: string
  url: string | null
  last_error: { error_code: string; message: string } | null
  created_at: string
  last_activity: string
}

export interface Health {
  status: string
  runtime: { provider: string; available: boolean; isolated: boolean }
}
