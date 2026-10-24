import { useEffect, useState } from 'react'

// Walking-skeleton page: proves the API, the database and SSE work end to end (replaced in step 5).
export default function App() {
  const [health, setHealth] = useState('checking…')
  const [ticks, setTicks] = useState<string[]>([])

  useEffect(() => {
    fetch('/api/health')
      .then((r) => r.json())
      .then((body) => setHealth(JSON.stringify(body)))
      .catch(() => setHealth('unreachable'))

    const source = new EventSource('/api/health/stream')
    source.addEventListener('tick', (e) => setTicks((prev) => [...prev, (e as MessageEvent).data]))
    return () => source.close()
  }, [])

  return (
    <main>
      <h1>Candidate screening agent</h1>
      <p>Health: {health}</p>
      <p>SSE ticks received: {ticks.length}</p>
      <ul>
        {ticks.slice(-5).map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
    </main>
  )
}
