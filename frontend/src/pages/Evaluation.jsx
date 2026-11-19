import PageHeader from '../components/PageHeader.jsx'
import { useFetch } from '../hooks.js'
import { LABELS, LABEL_KEYS } from '../utils.js'

const pct = (v) => (v == null ? '–' : `${Math.round(v * 100)}%`)
const TARGET_F1 = 0.75

export default function Evaluation() {
  const { data: m, error, loading, reload } = useFetch('/bi02/metrics')

  if (loading && !m) return <div className="empty">Loading…</div>
  if (error) return <><PageHeader title="Evaluation" /><div className="alert-error">{error}</div></>

  const pass = m.macro_f1 != null && m.macro_f1 >= TARGET_F1
  const ab = m.abstention

  return (
    <>
      <PageHeader title="Evaluation" subtitle="How accurate is the agent" info="Scored against expert labels. The agent never sees these labels." />
      <div className="toolbar">
        <span className="muted">Scored on accounts that have both an agent result and an expert label.</span>
        <span className="grow" />
        <button className="btn btn-reset btn-small" onClick={reload}>Refresh</button>
      </div>

      {!m.n_assessed ? (
        <div className="panel">Nothing to score yet. Run the agent on some accounts first.</div>
      ) : (
        <>
          <div className="cards">
            <div className="card"><div className="big">{m.n_assessed} <small>/ {m.n_gold}</small></div><div className="lbl">Accounts scored</div></div>
            <div className="card">
              <div className="big">{m.macro_f1}</div>
              <div className="lbl">Macro-F1 <span className={`badge ${pass ? 'b-ok' : 'b-warn'}`}>{pass ? 'meets' : 'below'} target {TARGET_F1}</span></div>
            </div>
            <div className="card"><div className="big">{pct(m.accuracy)}</div><div className="lbl">Accuracy</div></div>
            <div className="card">
              <div className="big">{ab.correctly_abstained} <small>/ {ab.ambiguous_cases}</small></div>
              <div className="lbl">Correct "not sure" on ambiguous cases</div>
            </div>
          </div>

          <div className="panel">
            <h3>Per reason</h3>
            <table className="mini wide">
              <thead><tr><th>Reason</th><th className="num">Accounts</th><th className="num">Precision</th><th className="num">Recall</th><th className="num">F1</th></tr></thead>
              <tbody>
                {m.per_label.map((p) => (
                  <tr key={p.label}><td>{LABELS[p.label]}</td><td className="num">{p.support}</td>
                    <td className="num">{pct(p.precision)}</td><td className="num">{pct(p.recall)}</td><td className="num"><b>{p.f1}</b></td></tr>
                ))}
              </tbody>
            </table>
            <p className="muted">"Not sure" on a clear case (wrongly abstained): {ab.wrongly_abstained} of {ab.clear_cases}.</p>
          </div>

          <div className="panel">
            <h3>Expert label (rows) vs agent label (columns)</h3>
            <div className="table-scroll">
              <table className="matrix">
                <thead><tr><th></th>{LABEL_KEYS.map((k) => <th key={k}>{LABELS[k]}</th>)}</tr></thead>
                <tbody>
                  {LABEL_KEYS.map((g) => (
                    <tr key={g}><th>{LABELS[g]}</th>
                      {LABEL_KEYS.map((p) => {
                        const n = m.confusion[g]?.[p] || 0
                        return <td key={p} className={n ? (g === p ? 'diag' : 'off') : 'zero'}>{n}</td>
                      })}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel">
            <h3>Is the confidence trustworthy?</h3>
            <table className="mini wide">
              <thead><tr><th>Confidence range</th><th className="num">Accounts</th><th className="num">Average confidence</th><th className="num">Actually correct</th></tr></thead>
              <tbody>
                {m.calibration.map((b) => (
                  <tr key={b.range}><td>{b.range}</td><td className="num">{b.n}</td><td className="num">{pct(b.avg_confidence)}</td><td className="num"><b>{pct(b.accuracy)}</b></td></tr>
                ))}
              </tbody>
            </table>
            <p className="muted">If "average confidence" and "actually correct" are close, the confidence can be trusted.</p>
          </div>

          <div className="panel">
            <h3>AI usage (this server session)</h3>
            <p className="muted">
              {m.llm_stats.calls} model calls · {m.llm_stats.cache_hits} served from cache · {m.llm_stats.retries} rate-limit retries ·{' '}
              {(m.llm_stats.prompt_tokens + m.llm_stats.completion_tokens).toLocaleString('en-IN')} tokens
            </p>
          </div>
        </>
      )}
    </>
  )
}
