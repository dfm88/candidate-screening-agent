import { useMutation, useQuery } from '@tanstack/react-query'
import { QRCodeSVG } from 'qrcode.react'
import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { api, ApiError, type ApplicationCreated, type Language } from './api'

// Neutral Spanish: these words are the same in Spain and Mexico ("teléfono", not "móvil" or "celular").
const COPY = {
  es: {
    title: 'Reparte con nosotros: envía tu solicitud',
    intro: 'Después te hacemos unas preguntas rápidas por chat. Son unos 3 minutos.',
    name: 'Nombre y apellidos',
    phone: 'Teléfono',
    phoneHint: 'Demo: usa un número inventado.',
    vacancy: 'Dónde quieres repartir',
    choose: 'Elige un restaurante',
    language: 'Idioma del chat',
    whatsapp: 'Acepto recibir mensajes por WhatsApp sobre mi solicitud.',
    accessCode: 'Código de acceso',
    submit: 'Enviar solicitud',
    sending: 'Enviando…',
    wrongCode: 'Ese código de acceso no es correcto. Revisa el enlace que te dieron.',
    failed: 'No se pudo enviar. Inténtalo de nuevo.',
    loadFailed: 'No se pudo cargar el formulario. Recarga la página.',
    countries: { ES: 'España', MX: 'México' },
    thanks: (name: string) => `¡Gracias, ${name}!`,
    sent: 'Tu solicitud está enviada. Seguimos por chat.',
    open: 'Continuar en el chat',
    scan: 'O escanea el código para seguir en el teléfono.',
  },
  en: {
    title: 'Deliver with us: send your application',
    intro: "Then we'll ask you a few quick questions by chat. It takes about 3 minutes.",
    name: 'Full name',
    phone: 'Phone',
    phoneHint: 'Demo: use a made-up number.',
    vacancy: 'Where you want to deliver',
    choose: 'Choose a restaurant',
    language: 'Chat language',
    whatsapp: 'I agree to receive WhatsApp messages about my application.',
    accessCode: 'Access code',
    submit: 'Send application',
    sending: 'Sending…',
    wrongCode: "That access code isn't right. Check the link you were given.",
    failed: "Couldn't send it. Please try again.",
    loadFailed: "Couldn't load the form. Reload the page.",
    countries: { ES: 'Spain', MX: 'Mexico' },
    thanks: (name: string) => `Thanks, ${name}!`,
    sent: "Your application is in. Let's continue by chat.",
    open: 'Continue in the chat',
    scan: 'Or scan the code to continue on your phone.',
  },
} as const

const field = 'mt-1 w-full rounded-lg border border-stone-300 bg-white px-3 py-2 focus:border-brand-500 focus:outline-none'

export default function ApplyPage() {
  const [searchParams] = useSearchParams()
  const [language, setLanguage] = useState<Language>(searchParams.get('lang') === 'en' ? 'en' : 'es')
  // One id per form: a retried submit (a double tap, a flaky network) returns the same chat.
  const [submissionId] = useState(() => crypto.randomUUID())
  const form = useQuery({ queryKey: ['apply-form'], queryFn: api.applyForm })
  const apply = useMutation({ mutationFn: api.apply })
  const copy = COPY[language]

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    apply.mutate({
      submission_id: submissionId,
      full_name: String(data.get('full_name')),
      phone: String(data.get('phone')),
      vacancy_location_id: String(data.get('vacancy')),
      language,
      whatsapp_opt_in: data.get('whatsapp') === 'on',
      access_code: data.has('access_code') ? String(data.get('access_code')) : null,
    })
  }

  if (apply.data) {
    const firstName = apply.variables.full_name.trim().split(/\s+/)[0]
    return <Confirmation created={apply.data} copy={copy} firstName={firstName} />
  }

  const vacancies = form.data?.vacancies ?? []
  const error =
    apply.error instanceof ApiError && apply.error.status === 403 ? copy.wrongCode : apply.error && copy.failed

  return (
    <main className="mx-auto max-w-md px-4 py-8">
      {form.data && <title>{form.data.client_name}</title>}
      <p className="text-sm font-semibold tracking-wide text-brand-600 uppercase">{form.data?.client_name}</p>
      <h1 className="mt-1 text-2xl font-bold text-stone-900">{copy.title}</h1>
      <p className="mt-2 text-stone-600">{copy.intro}</p>
      {form.isError && <p className="mt-6 text-red-700">{copy.loadFailed}</p>}

      <form onSubmit={submit} className="mt-6 space-y-4">
        <fieldset>
          <legend className="text-sm font-medium text-stone-700">{copy.language}</legend>
          <div className="mt-1 flex gap-2">
            {(['es', 'en'] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setLanguage(option)}
                aria-pressed={language === option}
                className={`rounded-full border px-4 py-1 ${language === option ? 'border-brand-600 bg-brand-600 text-white' : 'border-stone-300 bg-white text-stone-700'}`}
              >
                {option === 'es' ? 'Español' : 'English'}
              </button>
            ))}
          </div>
        </fieldset>

        <label className="block text-sm font-medium text-stone-700">
          {copy.name}
          <input name="full_name" required minLength={2} maxLength={120} autoComplete="name" className={field} />
        </label>

        <label className="block text-sm font-medium text-stone-700">
          {copy.phone}
          <input
            name="phone"
            type="tel"
            required
            // The v flag browsers use for `pattern` wants ( ) - escaped inside a class.
            pattern="\+?[0-9][0-9 \(\)\-]{6,19}"
            autoComplete="tel"
            className={field}
          />
          <span className="mt-1 block text-xs font-normal text-stone-500">{copy.phoneHint}</span>
        </label>

        <label className="block text-sm font-medium text-stone-700">
          {copy.vacancy}
          <select name="vacancy" required defaultValue="" className={field}>
            <option value="" disabled>
              {copy.choose}
            </option>
            {(['ES', 'MX'] as const).map((country) => (
              <optgroup key={country} label={copy.countries[country]}>
                {vacancies
                  .filter((vacancy) => vacancy.country === country)
                  .map((vacancy) => (
                    <option key={vacancy.id} value={vacancy.id}>
                      {vacancy.name}
                    </option>
                  ))}
              </optgroup>
            ))}
          </select>
        </label>

        {form.data?.access_code_required && (
          <label className="block text-sm font-medium text-stone-700">
            {copy.accessCode}
            <input name="access_code" required defaultValue={searchParams.get('code') ?? ''} className={field} />
          </label>
        )}

        <label className="flex items-start gap-2 text-sm text-stone-700">
          <input name="whatsapp" type="checkbox" className="mt-1 accent-brand-600" />
          {copy.whatsapp}
        </label>

        {error && <p className="text-sm text-red-700">{error}</p>}
        <button
          type="submit"
          disabled={!form.data || apply.isPending}
          className="w-full rounded-lg bg-brand-600 py-3 font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
        >
          {apply.isPending ? copy.sending : copy.submit}
        </button>
      </form>
    </main>
  )
}

function Confirmation({
  created,
  copy,
  firstName,
}: {
  created: ApplicationCreated
  copy: (typeof COPY)[Language]
  firstName: string
}) {
  const chatLink = `${window.location.origin}${created.chat_url}`
  return (
    <main className="mx-auto max-w-md px-4 py-10 text-center">
      <h1 className="text-2xl font-bold text-stone-900">{copy.thanks(firstName)}</h1>
      <p className="mt-2 text-stone-600">{copy.sent}</p>
      <Link
        to={created.chat_url}
        className="mt-6 block w-full rounded-lg bg-brand-600 py-3 font-semibold text-white hover:bg-brand-700"
      >
        {copy.open}
      </Link>
      <p className="mt-8 text-sm text-stone-600">{copy.scan}</p>
      <QRCodeSVG value={chatLink} size={176} marginSize={4} title={copy.open} className="mx-auto mt-3" />
    </main>
  )
}
