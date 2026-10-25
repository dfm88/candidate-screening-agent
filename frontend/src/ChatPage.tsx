import type { Status as CallStatus, VoiceConversation } from '@elevenlabs/client'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useParams } from 'react-router'
import {
  api,
  ApiError,
  type ChatMessage,
  type ChatStatus,
  type Corrected,
  type Language,
  type Status,
  type Typing,
} from './api'

const COPY = {
  es: {
    virtual: (client: string) => `Asistente virtual de ${client}`,
    placeholder: 'Escribe un mensaje',
    send: 'Enviar',
    typing: 'Escribiendo…',
    reconnecting: 'Reconectando…',
    closed: 'Este chat está cerrado.',
    notSent: 'No se envió. Toca para reintentar.',
    invalid: 'Este enlace de chat no es válido.',
    loadFailed: 'No se pudo abrir el chat. Recarga la página.',
    call: 'Llamar',
    connecting: 'Conectando…',
    hangUp: 'Colgar',
    callFailed: 'No se pudo llamar. Permite el micrófono e inténtalo de nuevo.',
    interrupted: 'interrumpido',
  },
  en: {
    virtual: (client: string) => `${client}'s virtual assistant`,
    placeholder: 'Type a message',
    send: 'Send',
    typing: 'Typing…',
    reconnecting: 'Reconnecting…',
    closed: 'This chat is closed.',
    notSent: 'Not sent. Tap to retry.',
    invalid: "This chat link isn't valid.",
    loadFailed: "Couldn't open the chat. Reload the page.",
    call: 'Call',
    connecting: 'Connecting…',
    hangUp: 'Hang up',
    callFailed: "Couldn't call. Allow the microphone and try again.",
    interrupted: 'cut off',
  },
} as const

// A message the candidate sent that the server hasn't echoed yet.
interface Outgoing {
  clientId: string
  text: string
  failed: boolean
}

/** The chat's events: every stored message (replayed on reconnect), the typing indicator and the status. */
function useChatEvents(token: string, enabled: boolean) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [typing, setTyping] = useState(false)
  const [status, setStatus] = useState<Status | null>(null)
  const [connected, setConnected] = useState(true)

  useEffect(() => {
    if (!enabled) return
    // EventSource reconnects by itself and sends Last-Event-ID, so the server replays only what was missed.
    const source = new EventSource(api.eventsUrl(token))
    source.addEventListener('message', (event) => {
      const message: ChatMessage = JSON.parse(event.data)
      setMessages((current) =>
        current.some((known) => known.id === message.id) ? current : [...current, message].sort((a, b) => a.id - b.id),
      )
    })
    // A message from a call, cut to what the candidate heard, or gone when they heard none of it.
    source.addEventListener('corrected', (event) => {
      const { id, text, interrupted }: Corrected = JSON.parse(event.data)
      setMessages((current) =>
        text === null
          ? current.filter((known) => known.id !== id)
          : current.map((known) => (known.id === id ? { ...known, text, interrupted } : known)),
      )
    })
    source.addEventListener('typing', (event) => setTyping((JSON.parse(event.data) as Typing).active))
    source.addEventListener('status', (event) => setStatus((JSON.parse(event.data) as ChatStatus).status))
    source.onopen = () => setConnected(true)
    source.onerror = () => setConnected(false)
    return () => source.close()
  }, [token, enabled])

  return { messages, typing, status, connected }
}

export default function ChatPage() {
  const { token = '' } = useParams()
  const session = useQuery({ queryKey: ['chat', token], queryFn: () => api.chatSession(token), retry: false })
  const { messages, typing, status, connected } = useChatEvents(token, session.isSuccess)
  const [outgoing, setOutgoing] = useState<Outgoing[]>([])
  const [draft, setDraft] = useState('')
  const bottom = useRef<HTMLDivElement>(null)

  const echoed = new Set(messages.map((message) => message.client_id))
  const waiting = outgoing.filter((message) => !echoed.has(message.clientId))
  useEffect(() => {
    // A block body: newer browsers return a Promise from scrollIntoView, which React would take for a cleanup.
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [messages.length, waiting.length, typing])

  if (session.isError) {
    const invalid = session.error instanceof ApiError && session.error.status === 404
    return <p className="p-6 text-center text-stone-700">{invalid ? COPY.es.invalid : COPY.es.loadFailed}</p>
  }
  if (!session.data) return null

  const copy = COPY[session.data.language satisfies Language]
  const closed = (status ?? session.data.status) === 'withdrawn'
  const last = messages.at(-1)
  // Suggested replies only while they're the latest thing in the chat; typing always works too.
  const quickReplies = !closed && last?.author === 'assistant' && waiting.length === 0 ? last.quick_replies : []

  async function send(text: string, clientId: string = crypto.randomUUID()) {
    setOutgoing((current) => [...current.filter((m) => m.clientId !== clientId), { clientId, text, failed: false }])
    try {
      await api.send(token, text, clientId)
    } catch {
      setOutgoing((current) => current.map((m) => (m.clientId === clientId ? { ...m, failed: true } : m)))
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const text = draft.trim()
    if (!text) return
    setDraft('')
    void send(text)
  }

  return (
    <div className="mx-auto flex h-dvh max-w-lg flex-col bg-brand-50">
      <title>{`${session.data.persona_name} · ${session.data.client_name}`}</title>
      <header className="flex items-center gap-3 bg-brand-600 px-4 py-3 text-white shadow">
        <div className="flex size-10 items-center justify-center rounded-full bg-white font-bold text-brand-700">
          {session.data.persona_name[0]}
        </div>
        <div>
          <p className="font-semibold">{session.data.persona_name}</p>
          <p className="text-xs text-brand-100">
            {!connected ? copy.reconnecting : typing ? copy.typing : copy.virtual(session.data.client_name)}
          </p>
        </div>
        {session.data.voice && !closed && <CallButton token={token} copy={copy} />}
      </header>

      <div role="log" aria-live="polite" className="flex-1 space-y-2 overflow-y-auto px-3 py-4">
        {messages.map((message) => (
          <Bubble key={message.id} own={message.author === 'candidate'} time={message.created_at}>
            {message.kind === 'voice' && <span aria-hidden>🎙️ </span>}
            {message.text}
            {message.interrupted && <span className="text-stone-400">… ({copy.interrupted})</span>}
          </Bubble>
        ))}
        {waiting.map((message) => (
          <Bubble key={message.clientId} own pending={!message.failed}>
            {message.text}
            {message.failed && (
              <button
                type="button"
                onClick={() => void send(message.text, message.clientId)}
                className="mt-1 block text-xs text-red-700 underline"
              >
                {copy.notSent}
              </button>
            )}
          </Bubble>
        ))}
        {typing && (
          <div className="w-fit rounded-2xl rounded-bl-sm bg-white px-4 py-2 text-stone-400 shadow-sm" aria-label={copy.typing}>
            •••
          </div>
        )}
        {quickReplies.length > 0 && (
          <div className="flex flex-wrap gap-2 pt-1">
            {quickReplies.map((reply) => (
              <button
                key={reply}
                type="button"
                onClick={() => void send(reply)}
                className="rounded-full border border-brand-600 bg-white px-4 py-1.5 text-sm font-medium text-brand-700 hover:bg-brand-100"
              >
                {reply}
              </button>
            ))}
          </div>
        )}
        <div ref={bottom} />
      </div>

      {closed ? (
        <p className="bg-white px-4 py-3 text-center text-sm text-stone-500">{copy.closed}</p>
      ) : (
        <form onSubmit={submit} className="flex gap-2 bg-white px-3 py-2 shadow-[0_-1px_2px_rgb(0_0_0/0.05)]">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={copy.placeholder}
            maxLength={2000}
            aria-label={copy.placeholder}
            className="flex-1 rounded-full border border-stone-300 px-4 py-2 focus:border-brand-500 focus:outline-none"
          />
          <button
            type="submit"
            disabled={!draft.trim()}
            className="rounded-full bg-brand-600 px-4 font-semibold text-white disabled:opacity-50"
          >
            {copy.send}
          </button>
        </form>
      )}
    </div>
  )
}

function Bubble({
  own,
  time,
  pending = false,
  children,
}: {
  own: boolean
  time?: string
  pending?: boolean
  children: React.ReactNode
}) {
  return (
    <div
      className={`w-fit max-w-[85%] rounded-2xl px-3 py-2 whitespace-pre-wrap shadow-sm ${own ? 'ml-auto rounded-br-sm bg-own' : 'rounded-bl-sm bg-white'} ${pending ? 'opacity-70' : ''}`}
    >
      {children}
      {time && (
        <span className="ml-2 align-bottom text-[10px] text-stone-400">
          {new Date(time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>
      )}
    </div>
  )
}

// A live voice call. ElevenLabs does the speech; our server answers each turn, and the turns show up in this chat through
// the same event stream. Its SDK loads on the first tap: most candidates type, and it's most of the bundle.
function CallButton({ token, copy }: { token: string; copy: (typeof COPY)[Language] }) {
  const call = useRef<VoiceConversation | null>(null)
  const [status, setStatus] = useState<CallStatus>('disconnected')
  const [failed, setFailed] = useState(false)
  const live = status === 'connected' || status === 'connecting'

  // Leaving the page hangs up.
  useEffect(() => () => void call.current?.endSession(), [])

  async function toggle() {
    if (live) {
      await call.current?.endSession()
      return
    }
    setFailed(false)
    setStatus('connecting')
    try {
      // Asked first, so a denied microphone fails here, with a message, rather than inside the call.
      const microphone = await navigator.mediaDevices.getUserMedia({ audio: true })
      microphone.getTracks().forEach((track) => track.stop())
      const [{ VoiceConversation }, { conversation_token, ticket }] = await Promise.all([
        import('@elevenlabs/client'),
        api.voiceSession(token),
      ])
      call.current = await VoiceConversation.startSession({
        conversationToken: conversation_token,
        dynamicVariables: { voice_ticket: ticket },
        onStatusChange: ({ status }) => setStatus(status),
      })
    } catch {
      setStatus('disconnected')
      setFailed(true)
    }
  }

  return (
    <div className="ml-auto text-right">
      <button
        type="button"
        onClick={() => void toggle()}
        className={`rounded-full px-4 py-1.5 text-sm font-semibold ${live ? 'bg-red-600 text-white' : 'bg-white text-brand-700'}`}
      >
        {status === 'connecting' ? copy.connecting : live ? `🔊 ${copy.hangUp}` : `🎙️ ${copy.call}`}
      </button>
      {failed && <p className="mt-1 max-w-48 text-xs text-brand-100">{copy.callFailed}</p>}
    </div>
  )
}
