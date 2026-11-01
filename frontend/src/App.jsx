import { useEffect, useRef, useState } from 'react'

const API_KEY = import.meta.env.VITE_API_KEY || ''
const apiHeaders = (extra = {}) => ({ ...(API_KEY ? { 'X-Api-Key': API_KEY } : {}), ...extra })

export default function App() {
  const [models, setModels] = useState([])
  const [question, setQuestion] = useState('')
  const [model, setModel] = useState('')
  const [loading, setLoading] = useState(false)
  const [turns, setTurns] = useState([])
  const [stage, setStage] = useState(null)
  const [liveSql, setLiveSql] = useState(null)
  const [fullSchema, setFullSchema] = useState(null)
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns, stage, liveSql])

  useEffect(() => {
    fetch('/api/schema/full', { headers: apiHeaders() })
      .then((r) => r.json())
      .then(setFullSchema)
      .catch(() => {})
  }, [])

  useEffect(() => {
    fetch('/api/models', { headers: apiHeaders() })
      .then((r) => r.json())
      .then((d) => {
        const list = Array.isArray(d) ? d : d.fallback || []
        setModels(list)
        if (list.length) setModel(list[0])
      })
      .catch(() => {})
  }, [])

  async function ask(e) {
    e.preventDefault()
    const q = question.trim()
    if (!q || loading) return
    const history = turns.slice(-5)
      .filter((t) => t.sql)
      .map((t) => ({ question: t.question, sql: t.sql }))
    setQuestion('')
    setLoading(true)
    setStage('Starting…')
    setLiveSql(null)
    setTurns((prev) => [...prev, { question: q, model, pending: true }])
    const finish = (turn) => {
      setTurns((prev) => {
        const next = [...prev]
        next[next.length - 1] = { ...next[next.length - 1], ...turn, pending: false }
        return next
      })
      setLoading(false)
      setStage(null)
    }
    try {
      const res = await fetch('/api/ask/stream', {
        method: 'POST',
        headers: apiHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ question: q, model, history }),
      })
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let done = false
      while (!done) {
        const { value, done: d } = await reader.read()
        done = d
        buf += decoder.decode(value || new Uint8Array(), { stream: !d })
        const parts = buf.split('\n\n')
        buf = parts.pop()
        for (const part of parts) {
          let event = null, data = null
          for (const line of part.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim()
            else if (line.startsWith('data:')) data = line.slice(5).trim()
            else if (line.startsWith(':')) continue
          }
          if (!event || data == null) continue
          const d = JSON.parse(data)
          if (event === 'stage') setStage(d)
          else if (event === 'sql') setLiveSql(d)
          else if (event === 'result') { finish({ question: q, model, ...d }); done = true; }
          else if (event === 'error') {
            finish({ question: q, model, error: d.error || 'failed', sql: d.sql || null, rows: [] })
            done = true
          }
        }
      }
    } catch (err) {
      finish({ question: q, model, error: String(err), sql: null, rows: [] })
    }
  }

  return (
    <div className="flex flex-col h-screen max-w-2xl mx-auto bg-white text-gray-900">
      <header className="border-b border-gray-200 px-4 py-3 flex items-center justify-between gap-2">
        <div>
          <h1 className="font-bold text-lg leading-tight">Football Oracle</h1>
          <p className="text-xs text-gray-500">88k games, 790 clubs, 50k players, 1.9M appearances, 175k transfers, frozen July 2026</p>
        </div>
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          className="text-sm border border-gray-300 rounded-full px-2 py-1 text-gray-700 bg-white"
        >
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      </header>

      <details className="px-4 py-1 text-xs text-gray-500">
        <summary className="cursor-pointer">
          What can I ask?{fullSchema ? ` ${Object.keys(fullSchema).length} tables` : '…'}
        </summary>
        {fullSchema && Object.entries(fullSchema).map(([t, info]) => (
          <div key={t} className="mt-2">
            <p><strong>{t}</strong> <span className="text-gray-400">{info.count.toLocaleString()} rows</span></p>
            <p className="text-gray-500">{info.columns.join(', ')}</p>
          </div>
        ))}
      </details>

      <main className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
        {turns.length === 0 && !loading && (
          <div className="text-center text-gray-400 text-sm mt-10">
            <p className="text-2xl mb-2">⚽</p>
            <p>Ask anything — try "who scored the most goals in a single season?"</p>
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className="space-y-1">
            <div className="flex justify-end">
              <div className="bg-blue-500 text-white rounded-3xl rounded-br-lg px-4 py-2 max-w-[80%] text-[15px]">
                {t.question}
              </div>
            </div>
            {t.pending ? (
              <div className="flex justify-start">
                <div className="bg-gray-100 rounded-3xl rounded-bl-lg px-4 py-2 text-[15px]">
                  <p className="flex items-center gap-1">
                    <span className="inline-block w-2 h-2 bg-gray-400 rounded-full animate-bounce" />
                    <span className="inline-block w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                    <span className="inline-block w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                    <span className="ml-1 text-gray-600 text-sm">{stage || 'Thinking…'}</span>
                  </p>
                  {liveSql && (
                    <details open className="text-xs mt-1">
                      <summary>Generated SQL</summary>
                      <pre className="text-xs">{liveSql}</pre>
                    </details>
                  )}
                </div>
              </div>
            ) : (
            <div className="flex justify-start">
              <div className="bg-gray-100 rounded-3xl rounded-bl-lg px-4 py-2 max-w-[90%] text-[15px]">
                {t.error ? (
                  <>
                    <p className="text-red-600">{t.error}</p>
                    {t.sql && <details className="text-xs mt-1"><summary>SQL</summary><pre>{t.sql}</pre></details>}
                  </>
                ) : (
                  <>
                    <p>{t.answer}</p>
                    <p className="text-[11px] text-gray-500 mt-1">
                      {t.model}{t.cached ? ' · cached' : ''} · {t.latency_ms} ms · {t.rows.length} rows
                    </p>
                    {t.timings && (
                      <p className="text-[11px] text-gray-400">
                        {Object.entries(t.timings).map(([k, v]) => `${k.replace('_ms', '')} ${v >= 1000 ? (v / 1000).toFixed(1) + 's' : v + 'ms'}`).join(' · ')}
                      </p>
                    )}
                    {t.rows.length > 0 && (
                      <div className="mt-2 overflow-x-auto rounded-xl bg-white px-3 py-2 shadow-sm">
                        <table className="w-full text-xs">
                          <thead>
                            <tr>{Object.keys(t.rows[0]).filter((c) => !c.toLowerCase().endsWith('_id')).map((c) => <th key={c} className="text-left pr-3 pb-1 font-medium uppercase tracking-wide text-[10px] text-gray-400">{c.replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())}</th>)}</tr>
                          </thead>
                          <tbody>
                            {t.rows.map((r, j) => (
                              <tr key={j} className="border-t border-gray-100">
                                {Object.entries(r).filter(([c]) => !c.toLowerCase().endsWith('_id')).map(([c, v], k) => <td key={k} className="pr-3 py-1 tabular-nums text-gray-700">{v ?? ''}</td>)}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    <details className="text-xs mt-1 text-gray-400">
                      <summary>SQL</summary>
                      <pre className="text-xs mt-1 text-gray-500">{t.sql}</pre>
                    </details>
                  </>
                )}
              </div>
            </div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </main>

      <form onSubmit={ask} className="border-t border-gray-200 p-3 flex gap-2 items-center">
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask a question about football…"
          autoFocus
          className="flex-1 border border-gray-300 rounded-full px-4 py-2 text-[15px] outline-none focus:border-blue-500"
        />
        <button
          type="submit"
          disabled={loading || !question.trim()}
          className="bg-blue-500 text-white rounded-full w-10 h-10 shrink-0 disabled:opacity-40 text-lg leading-none"
          aria-label="Send"
        >
          ↑
        </button>
      </form>
      <p className="text-center text-[11px] text-gray-400 pb-2">
        Data: Transfermarkt scrape by David Cariboo (
        <a href="https://www.kaggle.com/datasets/davidcariboo/player-scores" className="underline">davidcariboo/player-scores</a>
        ), CC0, frozen July 2026.
      </p>
    </div>
  )
}
