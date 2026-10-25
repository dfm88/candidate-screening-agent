// The API contract, mirroring backend/src/screening/contract.py: change both together.

export type Language = 'es' | 'en'
export type Country = 'ES' | 'MX'
export type Status = 'pending' | 'invited' | 'in_progress' | 'completed' | 'abandoned' | 'withdrawn'

export interface Vacancy {
  id: string
  name: string
  city: string
  country: Country
}

export interface ApplyForm {
  client_name: string
  vacancies: Vacancy[]
  access_code_required: boolean
}

export interface ApplicationRequest {
  submission_id: string
  full_name: string
  phone: string
  vacancy_location_id: string
  language: Language
  whatsapp_opt_in: boolean
  access_code: string | null
}

export interface ApplicationCreated {
  candidate_id: string
  chat_url: string
}

export interface ChatSession {
  persona_name: string
  client_name: string
  first_name: string
  language: Language
  status: Status
}

export interface ChatMessage {
  id: number
  author: 'candidate' | 'assistant'
  text: string
  quick_replies: string[]
  client_id: string | null
  created_at: string
}

// SSE events on /api/chat/{token}/events: `message` ChatMessage, `typing` Typing, `status` ChatStatus.
export interface Typing {
  active: boolean
}

export interface ChatStatus {
  status: Status
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'content-type': 'application/json' } })
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    throw new ApiError(response.status, typeof body?.detail === 'string' ? body.detail : response.statusText)
  }
  return body as T
}

export const api = {
  applyForm: () => request<ApplyForm>('/api/apply-form'),
  apply: (application: ApplicationRequest) =>
    request<ApplicationCreated>('/api/applications', { method: 'POST', body: JSON.stringify(application) }),
  chatSession: (token: string) => request<ChatSession>(`/api/chat/${token}`),
  send: (token: string, text: string, clientId: string) =>
    request<{ id: number }>(`/api/chat/${token}/messages`, {
      method: 'POST',
      body: JSON.stringify({ text, client_id: clientId }),
    }),
  eventsUrl: (token: string) => `/api/chat/${token}/events`,
}
