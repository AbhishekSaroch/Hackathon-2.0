import { useFetch } from '../hooks.js'
import { SegChips } from '../components/CommandBits.jsx'
import PageHeader from '../components/PageHeader.jsx'

function Anoms({ title, set }) {
  if (!set?.anomalies) return null
  return (
    <div className="panel">
      <div className="cmd-title">{title}</div>
      <table className="grid">
        <thead><tr><th>#</th><th>What was seeded</th><th>Where</th><th className="num">Alert after</th><th>Named driver</th><th>Right?</th></tr></thead>
        <tbody>
          {set.anomalies.map((a) => (
            <tr key={a.id}>
              <td><b>{a.id}</b></td><td>{a.kind.replace(/_/g, ' ')}</td><td><SegChips seg={a.segment} /></td>
              <td className="num">{a.alert_minutes == null ? 'not alerted' : `${Math.round(a.alert_minutes)} min`}{a.within_hour ? '' : a.alert_minutes != null ? ' (later than an hour)' : ''}</td>
              <td>{a.driver ? <SegChips seg={a.driver} /> : '–'}</td>
              <td><span className={'cbadge ' + (a.driver_correct ? 'ai' : 'bad')}>{a.driver_correct ? 'yes' : 'no'}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      {set.false_alerts && <p className="muted">False alerts: {set.false_alerts.total} in {set.false_alerts.days} days, worst day {set.false_alerts.per_day_max}, mean {set.false_alerts.per_day_mean} a day.</p>}
    </div>
  )
}

export default function ScoreCard() {
  const { data, error, loading } = useFetch('/lc04/evaluation')
  const r = data?.report
  return (
    <>
      <PageHeader title="Scorecard" subtitle="Acceptance criteria, measured honestly"
        info="All data is synthetic and the anomalies were seeded by us. The public days were used to tune; the hidden days were run once and logged." />
      {error && <div className="alert-error">{error}</div>}
      {loading && <div className="empty">Loading…</div>}
      {data && !r && <div className="empty">The scorecard has not been written yet: run <code>python backend/agents/lc04/lc_evaluate.py</code>.</div>}
      {r && (
        <>
          <div className="panel">
            <div className="cmd-title">Acceptance criteria (brief)</div>
            <table className="grid">
              <thead><tr><th>Criterion</th><th>Target</th><th>Public days (tuned on)</th><th>Hidden days: the official run (frozen code, once)</th>{r.hidden_after_fix && <th>Hidden days: re-scored after a bug fix (not a clean hold-out)</th>}</tr></thead>
              <tbody>
                {r.acceptance.map((a) => (
                  <tr key={a.criterion}>
                    <td>{a.criterion}</td><td>{a.target}</td>
                    <td>{a.public}</td>
                    <td>{a.hidden == null ? <span className="muted">not run yet</span> : <><span className={'cbadge ' + (a.status === 'met' ? 'ai' : a.status === 'partly' ? 'fb' : 'bad')}>{a.status}</span> {a.hidden}</>}</td>
                    {r.hidden_after_fix && <td>{a.hidden_after_fix || <span className="muted">unchanged</span>}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">{r.data_note}</p>
          </div>
          <Anoms title="Public days: practice anomalies (used to tune)" set={r.public} />
          <Anoms title="Hidden days: the 6 seeded anomalies, the official run (frozen code, run once)" set={r.hidden} />
          {r.hidden_after_fix && (
            <>
              <div className="panel honest"><div className="cmd-title">What happened after the official hidden run</div><p>{r.hidden_after_fix.note}</p></div>
              <Anoms title="Hidden days: the same data re-scored after the drill-down fix (not a clean hold-out)" set={r.hidden_after_fix} />
            </>
          )}
          {r.limits?.length > 0 && <div className="panel honest"><div className="cmd-title">Honest limits</div><ul>{r.limits.map((l, i) => <li key={i}>{l}</li>)}</ul></div>}
          {data.ledger?.length > 0 && <div className="panel"><div className="cmd-title">Hidden-data ledger</div><ul>{data.ledger.map((l, i) => <li key={i}><code>{l.at}</code> {l.event}{l.note ? `: ${l.note}` : ''}</li>)}</ul></div>}
        </>
      )}
    </>
  )
}
