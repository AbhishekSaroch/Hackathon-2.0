import { useEffect, useRef, useState } from 'react'
import { api, getAskUser } from '../api.js'
import { useFetch } from '../hooks.js'
import AskChart from '../components/AskChart.jsx'
import PageHeader from '../components/PageHeader.jsx'

const STATUS = {
  answered: ['Answered', 'b-ok'], clarify: ['Needs one detail', 'b-warn'], refused: ['Refused and logged', 'b-wont_pay'],
  no_data: ['No data', 'b-none'], unsupported: ['Not possible', 'b-none'], error: ['Could not answer', 'b-wont_pay'],
}

function Answer({ r, onChoose, busy }) {
  const [label, cls] = STATUS[r.status] || ['', '']
  const ex = r.explanation
  return (
    <div className={'panel ask-answer ask-' + r.status}>
      <div className="row-flex" style={{ margin: 0 }}>
        <span className={'badge ' + cls}>{label}</span>
        {r.metric?.name && <span className="sub">Metric: <b>{r.metric.name}</b>{r.metric.overlap && <span className="cbadge ai" style={{ marginLeft: 6 }}>one of several definitions</span>}</span>}
        <span className="grow" />
        {r.latency_ms != null && <span className="sub">{(r.latency_ms / 1000).toFixed(1)} s · {r.source === 'llm' ? 'language model + governed query' : r.source === 'rules' ? 'rule-based parser (model unavailable)' : r.source === 'xsource' ? 'routed by code to another module’s data' : 'safety check'}</span>}
      </div>
      {r.data_source && (
        <div className="row-flex" style={{ margin: '6px 0 0' }}>
          <span className="cbadge ai">Data from {r.data_source.module}</span>
          <span className="sub">{r.data_source.name} · copy made {r.data_source.synced_at || 'at an unknown time'}</span>
        </div>
      )}
      <p className="headline ask-headline">{r.headline}</p>

      {r.status === 'clarify' && r.clarification && (
        <div className="ask-options">
          {r.clarification.options.map((o) => (
            <button key={o.metric_id} className="ask-option" disabled={busy} onClick={() => onChoose(r.question, o.metric_id)}>
              <b>{o.name}</b><span>{o.definition}</span>
            </button>
          ))}
        </div>
      )}

      {r.status === 'refused' && r.refusal && (
        <p className="sub">Category: <b>{r.refusal.category.replace('_', ' ')}</b>. {['cross_tenant', 'write_attempt', 'raw_sql', 'injection'].includes(r.refusal.category) ? 'This request was written to the audit log.' : ''}</p>
      )}

      {r.chart && <AskChart chart={r.chart} />}

      {ex && (
        <div className="ask-explain">
          <div className="cmd-title">Why</div>
          {ex.summary.map((s, i) => <p key={i} className="ask-line">{s}</p>)}
          {ex.evidence?.length > 0 && <ul className="honest">{ex.evidence.map((e) => <li key={e.metric_id}>{e.text}</li>)}</ul>}
          {ex.segments?.length > 0 && ex.driver && (
            <div className="sub" style={{ marginTop: 6 }}>Ranked by effect on the {ex.dimension}: {ex.segments.map((s) => `${s.label} (${Math.round(s.share * 100)}%)`).join(' · ')}</div>
          )}
        </div>
      )}

      {r.table && r.table.rows.length > 0 && (
        <details className="ask-details" open={!r.chart && r.table.rows.length > 1}>
          <summary>Table ({r.table.rows.length} row{r.table.rows.length === 1 ? '' : 's'})</summary>
          <div className="table-wrap"><table className="grid"><thead><tr>{r.table.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>{r.table.rows.slice(0, 60).map((row, i) => <tr key={i}>{row.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody></table></div>
        </details>
      )}

      {r.notes?.length > 0 && <ul className="ask-notes">{r.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}

      {r.sql?.length > 0 && (
        <details className="ask-details">
          <summary>The query behind this answer ({r.sql.length} statement{r.sql.length === 1 ? '' : 's'})</summary>
          {r.sql.map((s, i) => (
            <div key={i} className="ask-sql">
              <div className="sub">{s.label} · lender{s.lenders.length > 1 ? 's' : ''} {s.lenders.join(', ')} · {s.rows} row(s)</div>
              <pre>{s.sql}{'\n'}-- parameters: {JSON.stringify(s.params)}</pre>
            </div>
          ))}
          <div className="sub">Every statement is read-only, filters on the tenant id (bound by the data layer, not by the question), and was checked before it ran.</div>
        </details>
      )}

      {r.steps?.length > 0 && (
        <details className="ask-details">
          <summary>How the agent got there ({r.steps.length} steps)</summary>
          <ol className="trace">{r.steps.map((s, i) => <li key={i}><b>{s.tool.replace('_', ' ')}</b>: {s.detail}{s.rows != null ? ` (${s.rows} rows)` : ''}</li>)}</ol>
        </details>
      )}
    </div>
  )
}

export default function AskPortfolio() {
  const [user, setUser] = useState(getAskUser())
  useEffect(() => {
    const h = () => { setUser(getAskUser()); setTurns([]) }
    window.addEventListener('lc01-user-changed', h)
    return () => window.removeEventListener('lc01-user-changed', h)
  }, [])
  const me = useFetch(`/lc01/me?u=${user}`)
  const ex = useFetch(`/lc01/examples?u=${user}`)
  const hist = useFetch(`/lc01/history?u=${user}`)
  const src = useFetch(`/lc01/sources?u=${user}`)
  const [syncMsg, setSyncMsg] = useState('')
  const [q, setQ] = useState('')
  const [turns, setTurns] = useState([])
  const [busy, setBusy] = useState(false)
  const end = useRef(null)

  async function ask(question, choice) {
    if (!question.trim() || busy) return
    setBusy(true)
    setTurns((t) => [...t, { question, pending: true }])
    setQ('')
    try {
      const r = await api('/lc01/ask', { method: 'POST', body: { question, choice } })
      setTurns((t) => [...t.slice(0, -1), { question, resp: r }])
    } catch (e) {
      setTurns((t) => [...t.slice(0, -1), { question, error: e.message }])
    } finally {
      setBusy(false)
      hist.reload()
      setTimeout(() => end.current?.scrollIntoView({ behavior: 'smooth' }), 50)
    }
  }
  async function syncCopy() {
    setSyncMsg('Copying BI-02, BI-03 and LC-04 data (read-only on them)…')
    try {
      const r = await api('/lc01/sync', { method: 'POST' })
      setSyncMsg(`Copy refreshed: ${r.tables} tables, ${Number(r.rows).toLocaleString()} rows in ${r.seconds}s.`)
      src.reload()
    } catch (e) { setSyncMsg(`Could not refresh: ${e.message}`) }
  }
  async function openHistory(id, question) {
    try {
      const resp = await api(`/lc01/history/${id}`)
      setTurns((t) => [...t, { question, resp: { ...resp, question } }])
    } catch (e) { /* ignore */ }
  }

  const lenders = me.data?.lender_names ? Object.values(me.data.lender_names).join(', ') : '…'
  return (
    <>
      <PageHeader title="Ask your portfolio" subtitle={`Plain-language questions about ${lenders}, answered from governed metrics`}
        info="The model only decides what you are asking. Numbers, SQL, charts and explanations are produced by code from governed metric definitions, scoped to your lender at the data layer." />
      <div className="detail-grid">
        <div>
          {turns.length === 0 && (
            <div className="note">
              {'Ask about collection efficiency, delinquency, roll rates, agencies, bounces, promises to pay or disputes across your lenders. If a word has several definitions I will ask which you mean. '}
              {(src.data?.sources || []).some((s) => s.id === 'bi02') && 'You can also ask about ABC Bank accounts that are not paying and why (forgot, cannot pay, will not pay, insufficient balance, disputed), recommended actions, and BI-03 early-warning flags, hit rate and lead time, in English or Hinglish. '}
              {(src.data?.sources || []).some((s) => s.id === 'lc04') && 'And the command center: anomalies, alerts, accounts and dashboards. '}
              If a request reaches beyond your access I will refuse and log it. All data is synthetic.
            </div>
          )}
          {turns.map((t, i) => (
            <div key={i} className="ask-turn">
              <div className="ask-q">{t.question}</div>
              {t.pending ? <div className="empty">Working on it…</div> : t.error ? <div className="alert-error">{t.error}</div> : <Answer r={t.resp} onChoose={ask} busy={busy} />}
            </div>
          ))}
          <div ref={end} />
          <form className="ask-form" onSubmit={(e) => { e.preventDefault(); ask(q) }}>
            <textarea className="field gate-text" rows={2} placeholder="e.g. Why did total collection efficiency drop in September?" value={q}
              onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(q) } }} />
            <button className="btn btn-search" disabled={busy || !q.trim()}>{busy ? 'Asking…' : 'Ask'}</button>
          </form>
        </div>

        <div>
          <div className="panel">
            <h3>Try asking</h3>
            {(ex.data || []).map((e) => <div key={e.id}><button className="link-btn ask-example" disabled={busy} onClick={() => ask(e.text)}>{e.text}</button></div>)}
            <div className="sub" style={{ marginTop: 8 }}>Safety checks to try: ask about another lender, type SQL, or ask it to delete something.</div>
          </div>
          {(src.data?.sources || []).length > 0 && (
            <div className="panel">
              <h3>Other modules’ data</h3>
              <div className="sub">You can also ask about: {src.data.sources.map((s) => s.name).join('; ')}.</div>
              <div className="sub" style={{ marginTop: 6 }}>{src.data.copy_exists ? `Read from a copy made ${src.data.synced_at || 'at an unknown time'}. The modules’ own databases are never changed.` : 'The data copy has not been built yet.'}</div>
              <button className="btn btn-reset btn-small" style={{ marginTop: 8 }} onClick={syncCopy}>Refresh data copy</button>
              {syncMsg && <div className="sub" style={{ marginTop: 6 }}>{syncMsg}</div>}
            </div>
          )}
          <div className="panel">
            <h3>Your recent questions</h3>
            {(hist.data || []).length === 0 && <div className="sub">Nothing yet.</div>}
            {(hist.data || []).slice(0, 8).map((h) => (
              <div key={h.id} className="ask-hist"><button className="link-btn" onClick={() => openHistory(h.id, h.question)}>{h.question}</button>
                <span className="sub"> · {h.status}</span></div>
            ))}
          </div>
        </div>
      </div>
    </>
  )
}
