export type CredentialMode = 'server' | 'byok' | 'local'
export interface ProviderDescriptor {
  id: string
  name: string
  kind: 'cloud' | 'local'
  modes: CredentialMode[]
  note: string
  availability: 'backend-supported' | 'planned'
}

// Descriptors are capability documentation, not detected connections.
export const providers: ProviderDescriptor[] = [
  { id: 'gemini', name: 'Gemini', kind: 'cloud', modes: ['server', 'byok'], note: 'Native gateway adapter', availability: 'backend-supported' },
  { id: 'openai-compatible', name: 'OpenAI-compatible', kind: 'cloud', modes: ['server', 'byok'], note: 'Configured endpoint and model', availability: 'backend-supported' },
  { id: 'ollama', name: 'Ollama', kind: 'local', modes: ['local'], note: 'Local connection workflow', availability: 'planned' },
  { id: 'lm-studio', name: 'LM Studio', kind: 'local', modes: ['local'], note: 'Local connection workflow', availability: 'planned' },
]
