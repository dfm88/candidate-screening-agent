// The API contract, mirroring backend/src/screening/contract.py: change both together.

export type Language = 'es' | 'en'
export type Country = 'ES' | 'MX'
export type Status = 'pending' | 'invited' | 'in_progress' | 'completed' | 'abandoned' | 'withdrawn'
export type Outcome = 'qualified' | 'not_qualified' | 'needs_human_review'

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

// --- Recruiter tool ---

export interface Recruiter {
  name: string
}

export type Tab = 'ready' | 'review' | 'in_progress' | 'closed'
export type FieldName = 'full_name' | 'license' | 'service_area' | 'availability' | 'schedule' | 'experience' | 'start_date'
export type FieldMark = 'saved' | 'gap' | 'review' | 'missing'

export interface CandidateRow {
  id: string
  full_name: string
  vacancy: string
  country: Country
  status: Status
  outcome: Outcome | null
  decided_outcome: Outcome | null
  reasons: string[]
  tab: Tab
  created_at: string
}

export interface CandidateList {
  candidates: CandidateRow[]
  counts: Record<Tab, number>
}

export interface KeyData {
  answers: Record<FieldName, string>
  outcome: Outcome | null
  reasons: string[]
  talent_pool_opt_in: boolean | null
  open_questions: string[]
}

export interface RecruiterSummary {
  caveats: string[]
  notes: string[]
}

export interface Decision {
  outcome: Outcome
  by: string
  note: string | null
  at: string
}

export interface TranscriptMessage extends ChatMessage {
  template_key: string | null
}

export interface CandidateDetail {
  id: string
  full_name: string
  phone: string
  vacancy: Vacancy
  language: Language
  whatsapp_opt_in: boolean
  status: Status
  tab: Tab
  created_at: string
  completed_at: string | null
  key_data: KeyData
  marks: Record<FieldName, FieldMark>
  matched_location: Vacancy | null
  summary: RecruiterSummary | null
  decision: Decision | null
  chat_url: string
  transcript: TranscriptMessage[]
}

export interface DecisionRequest {
  outcome: 'qualified' | 'not_qualified'
  note: string | null
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
  // The recruiter tool: the login sets an HttpOnly session cookie, which fetch sends on same-origin requests.
  login: (name: string, password: string) =>
    request<Recruiter>('/api/recruiter/login', { method: 'POST', body: JSON.stringify({ name, password }) }),
  logout: () => request<null>('/api/recruiter/logout', { method: 'POST' }),
  me: () => request<Recruiter>('/api/recruiter/me'),
  candidates: (tab: Tab | null, q: string) => {
    const params = new URLSearchParams(q ? { q } : tab ? { tab } : {})
    return request<CandidateList>(`/api/recruiter/candidates?${params}`)
  },
  candidate: (id: string) => request<CandidateDetail>(`/api/recruiter/candidates/${id}`),
  decide: (id: string, decision: DecisionRequest) =>
    request<Decision>(`/api/recruiter/candidates/${id}/decision`, { method: 'POST', body: JSON.stringify(decision) }),
}
