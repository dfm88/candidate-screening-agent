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
  // A live voice call is set up for the chat's current language.
  voice: boolean
}

export interface ChatMessage {
  id: number
  author: 'candidate' | 'assistant'
  // voice: said in a call, the candidate's words as transcribed and ours as heard.
  kind: 'text' | 'template' | 'voice'
  text: string
  // Our reply in a call, cut off: the text is the part the candidate heard.
  interrupted: boolean
  quick_replies: string[]
  client_id: string | null
  created_at: string
}

// SSE events on /api/chat/{token}/events: `message` ChatMessage, `typing` Typing, `status` ChatStatus,
// `corrected` Corrected.
export interface Typing {
  active: boolean
}

export interface ChatStatus {
  status: Status
}

// A message from a call, corrected once we know what the candidate heard: text null means none of it was played.
export interface Corrected {
  id: number
  text: string | null
  interrupted: boolean
}

export interface VoiceSession {
  conversation_token: string
  ticket: string
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
  replies_failed: boolean
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
  replies_failed: boolean
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

export type DropOffStage = 'not_started' | 'recap' | FieldName

// A rate is null while nothing counts towards it.
export interface Metrics {
  applied: number
  response_rate: number | null
  completion_rate: number | null
  drop_off: Partial<Record<DropOffStage, number>>
  review_rate: number | null
  overrides: number
  override_rate: number | null
  median_seconds_to_outcome: number | null
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
  // A live voice call: the ElevenLabs token, and the ticket that tells our server which chat it is.
  voiceSession: (token: string) => request<VoiceSession>(`/api/voice/chat/${token}/session`, { method: 'POST' }),
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
  metrics: () => request<Metrics>('/api/recruiter/metrics'),
}
