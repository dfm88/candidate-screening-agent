import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, NavLink, Outlet, useParams, useSearchParams } from 'react-router'
import {
  api,
  ApiError,
  type CandidateDetail,
  type CandidateRow,
  type DropOffStage,
  type FieldMark,
  type FieldName,
  type Outcome,
  type Status,
  type Tab,
} from './api'

// Recruiter-facing copy is in English, like the key data the backend builds.
const TABS: { tab: Tab; label: string }[] = [
  { tab: 'ready', label: 'Ready' },
  { tab: 'review', label: 'Review' },
  { tab: 'in_progress', label: 'In progress' },
  { tab: 'closed', label: 'Closed' },
]
const TAB_LABEL = Object.fromEntries(TABS.map(({ tab, label }) => [tab, label])) as Record<Tab, string>
const OUTCOME_LABEL: Record<Outcome, string> = {
  qualified: 'Qualified',
  not_qualified: 'Not qualified',
  needs_human_review: 'Needs review',
}
const STATUS_LABEL: Record<Status, string> = {
  pending: 'Pending',
  invited: 'Invited',
  in_progress: 'In progress',
  completed: 'Completed',
  abandoned: 'Abandoned',
  withdrawn: 'Withdrawn',
}
const FIELD_LABEL: Record<FieldName, string> = {
  full_name: 'Full name',
  license: 'Driving license',
  service_area: 'Service area',
  availability: 'Availability',
  schedule: 'Schedule',
  experience: 'Experience',
  start_date: 'Start date',
}
const MARK: Record<FieldMark, { symbol: string; title: string; className: string }> = {
  saved: { symbol: '✓', title: 'Answered', className: 'text-green-700' },
  gap: { symbol: 'gap', title: 'Unclear after two tries: noted for you, the outcome is unchanged', className: 'text-amber-700' },
  review: { symbol: '⚑', title: 'Required and unclear: a review reason', className: 'text-red-700' },
  missing: { symbol: '–', title: 'Not asked yet', className: 'text-stone-400' },
}
const STAGE_LABEL: Record<DropOffStage, string> = { not_started: 'Never replied', ...FIELD_LABEL, recap: 'Recap' }
// The backend's reason codes, as compute_outcome writes them ("code: detail").
const REASON_LABEL: Record<string, string> = {
  no_license: 'No valid driving license',
  outside_service_area: 'Lives outside every service area',
  human_requested: 'Asked for a person',
  knockout_changed: 'Changed a knockout answer after the outcome',
  conduct: 'Ended for conduct',
}

function reasonLabel(reason: string): string {
  const [code, ...rest] = reason.split(': ')
  const unclear = code.match(/^(\w+) unclear$/)
  const label = unclear ? `${FIELD_LABEL[unclear[1] as FieldName] ?? unclear[1]} unclear` : (REASON_LABEL[code] ?? code)
  return rest.length ? `${label}: ${rest.join(': ')}` : label
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

function percent(rate: number | null): string {
  return rate === null ? '–' : `${Math.round(rate * 100)}%`
}

function duration(seconds: number | null): string {
  if (seconds === null) return '–'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h`
  return `${(seconds / 86400).toFixed(1)} d`
}

const field = 'mt-1 w-full rounded-lg border border-stone-300 bg-white px-3 py-2 focus:border-brand-500 focus:outline-none'
const primaryButton =
  'rounded-lg bg-brand-600 px-4 py-2 font-semibold text-white hover:bg-brand-700 disabled:opacity-60'

/** Every recruiter page: the login form until the session cookie is there, then the header and the page. */
export function RecruiterLayout() {
  const queryClient = useQueryClient()
  const me = useQuery({ queryKey: ['recruiter-me'], queryFn: api.me, retry: false })
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => queryClient.resetQueries({ queryKey: ['recruiter-me'] }),
  })

  if (me.isPending) return null
  if (me.isError) {
    if (me.error instanceof ApiError && me.error.status === 401) return <LoginForm />
    return <p className="p-6 text-center text-stone-700">{me.error.message}</p>
  }
  return (
    <div className="min-h-dvh bg-stone-50">
      <title>Recruiter tool</title>
      <header className="bg-brand-600 text-white shadow">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <Link to="/recruiter" className="font-semibold">
            Recruiter tool
          </Link>
          <div className="flex items-center gap-3 text-sm">
            <span>{me.data.name}</span>
            <button type="button" onClick={() => logout.mutate()} className="rounded border border-white/60 px-2 py-0.5">
              Log out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}

function LoginForm() {
  const queryClient = useQueryClient()
  const login = useMutation({
    mutationFn: ({ name, password }: { name: string; password: string }) => api.login(name, password),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['recruiter-me'] }),
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    login.mutate({ name: String(data.get('name')), password: String(data.get('password')) })
  }

  return (
    <main className="mx-auto max-w-sm px-4 py-16">
      <title>Recruiter tool · Log in</title>
      <h1 className="text-2xl font-bold text-stone-900">Recruiter tool</h1>
      <p className="mt-2 text-sm text-stone-600">Your name is shown on the decisions you make.</p>
      <form onSubmit={submit} className="mt-6 space-y-4">
        <label className="block text-sm font-medium text-stone-700">
          Your name
          <input name="name" required maxLength={60} autoComplete="name" className={field} />
        </label>
        <label className="block text-sm font-medium text-stone-700">
          Password
          <input name="password" type="password" required autoComplete="current-password" className={field} />
        </label>
        {login.error && <p className="text-sm text-red-700">{login.error.message}</p>}
        <button type="submit" disabled={login.isPending} className={`w-full ${primaryButton}`}>
          Log in
        </button>
      </form>
    </main>
  )
}

function OutcomeBadge({ status, outcome, decided }: { status: Status; outcome: Outcome | null; decided: Outcome | null }) {
  const shown = decided ?? outcome
  const colour =
    shown === 'qualified'
      ? 'bg-green-100 text-green-800'
      : shown === 'not_qualified'
        ? 'bg-stone-200 text-stone-700'
        : shown === 'needs_human_review'
          ? 'bg-amber-100 text-amber-800'
          : 'bg-sky-100 text-sky-800'
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${colour}`}>
      {shown ? OUTCOME_LABEL[shown] : STATUS_LABEL[status]}
      {decided && ' · decided'}
    </span>
  )
}

export function CandidatesPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = (searchParams.get('tab') as Tab | null) ?? 'ready'
  const q = searchParams.get('q') ?? ''
  const [draft, setDraft] = useState(q)
  const list = useQuery({
    queryKey: ['recruiter-candidates', tab, q],
    queryFn: () => api.candidates(tab, q),
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  })

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSearchParams(draft.trim() ? { q: draft.trim() } : { tab })
  }

  return (
    <>
      <MetricsRow />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <nav className="flex flex-wrap gap-1" aria-label="Tabs">
          {TABS.map(({ tab: option, label }) => (
            <button
              key={option}
              type="button"
              onClick={() => {
                setDraft('')
                setSearchParams({ tab: option })
              }}
              aria-pressed={!q && tab === option}
              className={`rounded-full px-4 py-1.5 text-sm font-medium ${!q && tab === option ? 'bg-brand-600 text-white' : 'bg-white text-stone-700 shadow-sm'}`}
            >
              {label} <span className="opacity-70">{list.data?.counts[option] ?? ''}</span>
            </button>
          ))}
        </nav>
        <form onSubmit={search} role="search">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Search name or phone"
            aria-label="Search name or phone"
            className="rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-sm"
          />
        </form>
      </div>
      {q && <p className="mt-4 text-sm text-stone-600">Results for “{q}” in every tab.</p>}
      {list.isError && <p className="mt-6 text-red-700">{list.error.message}</p>}
      {list.data && <CandidateTable rows={list.data.candidates} showTab={Boolean(q)} />}
    </>
  )
}

/** The funnel (do candidates answer, and finish?) and what still takes a recruiter's time, from the database. */
function MetricsRow() {
  const metrics = useQuery({ queryKey: ['recruiter-metrics'], queryFn: api.metrics, refetchInterval: 30_000 })
  if (!metrics.data) return null
  const data = metrics.data
  const dropOff = (Object.keys(STAGE_LABEL) as DropOffStage[])
    .filter((stage) => data.drop_off[stage])
    .map((stage) => `${STAGE_LABEL[stage]} ${data.drop_off[stage]}`)
    .join(' · ')
  const items = [
    { label: 'Response', value: percent(data.response_rate), title: 'Applicants who replied at least once' },
    { label: 'Completion', value: percent(data.completion_rate), title: 'Of those who replied, screenings that finished' },
    { label: 'Drop-off', value: dropOff || '–', title: 'Abandoned candidates, by where they stopped' },
    { label: 'Sent to review', value: percent(data.review_rate), title: 'Finished screenings the rules sent to a person' },
    {
      label: 'Overrides',
      value: data.override_rate === null ? `${data.overrides}` : `${data.overrides} (${percent(data.override_rate)})`,
      title: "Decisions that differ from a rules' outcome of qualified or not qualified",
    },
    { label: 'Median to outcome', value: duration(data.median_seconds_to_outcome), title: "From applying to the rules' outcome" },
  ]
  return (
    <dl className="mb-4 flex flex-wrap items-baseline gap-x-5 gap-y-1 text-sm" aria-label="Metrics">
      {items.map(({ label, value, title }) => (
        <div key={label} title={title} className="flex gap-1.5">
          <dt className="text-stone-500">{label}</dt>
          <dd className="font-semibold text-stone-900">{value}</dd>
        </div>
      ))}
      <div className="text-stone-400">of {data.applied} applications</div>
    </dl>
  )
}

function CandidateTable({ rows, showTab }: { rows: CandidateRow[]; showTab: boolean }) {
  if (rows.length === 0) return <p className="mt-10 text-center text-stone-500">Nobody here yet.</p>
  return (
    <div className="mt-4 overflow-x-auto rounded-lg bg-white shadow-sm">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-stone-200 text-xs text-stone-500 uppercase">
          <tr>
            <th className="px-4 py-2 font-medium">Candidate</th>
            <th className="px-4 py-2 font-medium">Vacancy</th>
            <th className="px-4 py-2 font-medium">Outcome</th>
            <th className="px-4 py-2 font-medium">Reasons</th>
            <th className="px-4 py-2 font-medium">Applied</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-stone-100">
          {rows.map((row) => (
            <tr key={row.id} className="hover:bg-brand-50">
              <td className="px-4 py-2">
                <NavLink to={`/recruiter/${row.id}`} className="font-medium text-brand-700 hover:underline">
                  {row.full_name}
                </NavLink>
                {showTab && <span className="ml-2 text-xs text-stone-500">{TAB_LABEL[row.tab]}</span>}
              </td>
              <td className="px-4 py-2 text-stone-700">{row.vacancy}</td>
              <td className="px-4 py-2">
                <OutcomeBadge status={row.status} outcome={row.outcome} decided={row.decided_outcome} />
              </td>
              <td className="px-4 py-2 text-stone-600">
                {row.replies_failed && <span className="mr-2 font-medium text-red-700">Our replies are failing</span>}
                {row.reasons.map(reasonLabel).join(' · ')}
              </td>
              <td className="px-4 py-2 whitespace-nowrap text-stone-500">{formatDate(row.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg bg-white p-4 shadow-sm">
      <h2 className="text-sm font-semibold tracking-wide text-stone-500 uppercase">{title}</h2>
      <div className="mt-3">{children}</div>
    </section>
  )
}

export function CandidatePage() {
  const { id = '' } = useParams()
  const candidate = useQuery({
    queryKey: ['recruiter-candidate', id],
    queryFn: () => api.candidate(id),
    // The chat may still be running: show its progress.
    refetchInterval: 5_000,
  })

  if (candidate.isError) return <p className="text-red-700">{candidate.error.message}</p>
  if (!candidate.data) return null
  const detail = candidate.data
  const data = detail.key_data

  return (
    <>
      <title>{`${detail.full_name} · Recruiter tool`}</title>
      <Link to="/recruiter" className="text-sm text-brand-700 hover:underline">
        ← Candidates
      </Link>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-stone-900">{detail.full_name}</h1>
        <OutcomeBadge status={detail.status} outcome={data.outcome} decided={detail.decision?.outcome ?? null} />
        <span className="text-sm text-stone-500">{TAB_LABEL[detail.tab]}</span>
      </div>
      <p className="mt-1 text-sm text-stone-600">
        {detail.phone} · {detail.vacancy.name} ({detail.vacancy.city}) · chat in {detail.language === 'es' ? 'Spanish' : 'English'}
        {' · '}applied {formatDate(detail.created_at)}
      </p>
      {detail.replies_failed && (
        <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800">
          Our replies to this candidate keep failing, so the automatic retry has stopped and their last message is
          unanswered. It's tried again when they write or the service restarts.
        </p>
      )}

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <div className="space-y-4">
          <DecisionCard detail={detail} />

          <Card title="Key data">
            <dl className="divide-y divide-stone-100 text-sm">
              {(Object.keys(FIELD_LABEL) as FieldName[]).map((name) => (
                <div key={name} className="flex gap-3 py-1.5">
                  <dt className="w-32 shrink-0 text-stone-500">{FIELD_LABEL[name]}</dt>
                  <dd className="flex-1 text-stone-900">
                    {name === 'availability' || name === 'schedule' ? data.answers[name].replaceAll('_', ' ') : data.answers[name]}
                  </dd>
                  <span title={MARK[detail.marks[name]].title} className={`text-xs font-semibold ${MARK[detail.marks[name]].className}`}>
                    {MARK[detail.marks[name]].symbol}
                  </span>
                </div>
              ))}
            </dl>
            <p className="mt-3 text-sm">
              <span className="text-stone-500">The rules' outcome: </span>
              {data.outcome ? OUTCOME_LABEL[data.outcome] : 'none yet'}
            </p>
            {data.reasons.length > 0 && (
              <ul className="mt-1 list-disc pl-5 text-sm text-stone-700">
                {data.reasons.map((reason) => (
                  <li key={reason}>{reasonLabel(reason)}</li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-sm">
              <span className="text-stone-500">Matching restaurant: </span>
              {detail.matched_location ? `${detail.matched_location.name} (${detail.matched_location.city})` : '–'}
            </p>
            <p className="mt-1 text-sm">
              <span className="text-stone-500">Other roles (talent pool): </span>
              {data.talent_pool_opt_in === null ? '–' : data.talent_pool_opt_in ? 'yes' : 'no'}
            </p>
            {data.open_questions.length > 0 && (
              <>
                <p className="mt-2 text-sm text-stone-500">Questions for the team:</p>
                <ul className="list-disc pl-5 text-sm text-stone-700">
                  {data.open_questions.map((question) => (
                    <li key={question}>{question}</li>
                  ))}
                </ul>
              </>
            )}
          </Card>

          <Card title="Summary · written by AI from the chat">
            {detail.summary ? (
              <div className="space-y-2 text-sm">
                <SummaryList title="Check before deciding" items={detail.summary.caveats} />
                <SummaryList title="Notes" items={detail.summary.notes} />
              </div>
            ) : (
              <p className="text-sm text-stone-500">Written when the screening ends.</p>
            )}
          </Card>

          <ChatLinkCard chatUrl={detail.chat_url} />
        </div>

        <Card title="Chat">
          <Transcript detail={detail} />
        </Card>
      </div>
    </>
  )
}

function SummaryList({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <p className="text-stone-500">{title}</p>
      {items.length ? (
        <ul className="list-disc pl-5 text-stone-800">
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="text-stone-400">Nothing to add.</p>
      )}
    </div>
  )
}

function DecisionCard({ detail }: { detail: CandidateDetail }) {
  const queryClient = useQueryClient()
  const [outcome, setOutcome] = useState<'qualified' | 'not_qualified' | null>(null)
  const [note, setNote] = useState('')
  const decide = useMutation({
    mutationFn: () => api.decide(detail.id, { outcome: outcome!, note: note.trim() || null }),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['recruiter-candidate', detail.id] }),
        queryClient.invalidateQueries({ queryKey: ['recruiter-candidates'] }),
      ]),
  })

  if (detail.decision) {
    return (
      <Card title="Decision">
        <p className="text-sm">
          <span className="font-semibold">{OUTCOME_LABEL[detail.decision.outcome]}</span>, by {detail.decision.by} on{' '}
          {formatDate(detail.decision.at)}. The candidate was told in the chat.
        </p>
        {detail.decision.note && <p className="mt-1 text-sm text-stone-700">Note: {detail.decision.note}</p>}
      </Card>
    )
  }
  if (detail.status !== 'completed') {
    return (
      <Card title="Decision">
        <p className="text-sm text-stone-500">You can decide once the screening has ended.</p>
      </Card>
    )
  }
  return (
    <Card title="Decision">
      <div className="flex gap-2">
        {(['qualified', 'not_qualified'] as const).map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setOutcome(option)}
            aria-pressed={outcome === option}
            className={`rounded-lg border px-4 py-1.5 text-sm font-medium ${outcome === option ? 'border-brand-600 bg-brand-600 text-white' : 'border-stone-300 bg-white text-stone-700'}`}
          >
            {OUTCOME_LABEL[option]}
          </button>
        ))}
      </div>
      <label className="mt-3 block text-sm text-stone-700">
        Note for the team (the candidate doesn't see it)
        <textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={1000} rows={2} className={field} />
      </label>
      {decide.error && <p className="mt-2 text-sm text-red-700">{decide.error.message}</p>}
      <div className="mt-3 flex items-center gap-3">
        <button type="button" disabled={!outcome || decide.isPending} onClick={() => decide.mutate()} className={primaryButton}>
          Send decision
        </button>
        <p className="text-xs text-stone-500">Final. The candidate gets a message in the chat.</p>
      </div>
    </Card>
  )
}

function ChatLinkCard({ chatUrl }: { chatUrl: string }) {
  const [copied, setCopied] = useState(false)
  const link = `${window.location.origin}${chatUrl}`

  async function copy() {
    await navigator.clipboard.writeText(link)
    setCopied(true)
  }

  return (
    <Card title="Candidate's chat link">
      <div className="flex gap-2">
        <input readOnly value={link} aria-label="Chat link" className="flex-1 rounded-lg border border-stone-300 bg-stone-50 px-3 py-1.5 text-sm" />
        <button type="button" onClick={() => void copy()} className="rounded-lg border border-stone-300 bg-white px-3 text-sm">
          {copied ? 'Copied' : 'Copy'}
        </button>
        <a href={chatUrl} target="_blank" rel="noreferrer" className="rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-sm">
          Open
        </a>
      </div>
      <p className="mt-2 text-xs text-stone-500">Whoever has the link can chat as the candidate.</p>
    </Card>
  )
}

function Transcript({ detail }: { detail: CandidateDetail }) {
  return (
    <div className="max-h-[70vh] space-y-2 overflow-y-auto rounded-lg bg-brand-50 p-3">
      {detail.transcript.map((message) => (
        <div
          key={message.id}
          className={`w-fit max-w-[85%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap shadow-sm ${message.author === 'candidate' ? 'ml-auto rounded-br-sm bg-own' : 'rounded-bl-sm bg-white'}`}
        >
          {message.text}
          <span className="mt-0.5 block text-[10px] text-stone-400">
            {formatDate(message.created_at)}
            {message.template_key && ` · template: ${message.template_key.replaceAll('_', ' ')}`}
          </span>
        </div>
      ))}
    </div>
  )
}
