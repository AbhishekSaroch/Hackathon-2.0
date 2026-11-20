import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { SegChips, fmtClock, hhmm, segmentText } from '../components/CommandBits.jsx'
import PageHeader from '../components/PageHeader.jsx'

const TOOLS = { detect: 'Detect', drill_down: 'Drill down', counterfactual: 'Counterfactual check', annotate: 'Annotate', alert: 'Alert' }

function traceText(t) {
  const o = t.output || {}
  if (t.tool === 'detect' && o.rule) return `Rule breached: ${o.count} legal notices sent outside the permitted ${o.window} window; logged at ${hhmm(o.detected_at)}. (A rule, not a statistical detector: the night-time baseline is zero.)`
  if (t.tool === 'drill_down' && o.z == null) return `Driver: ${segmentText(o.driver) || 'no single sender'}; it holds ${Math.round(Math.min(o.explained, 1) * 100)}% of the notices.`
  if (t.tool === 'counterfactual' && o.kind === 'rule') return o.note
  if (t.tool === 'detect') return `${o.direction === 'down' ? 'Fell' : 'Rose'} beyond seasonal expectations (score ${o.score}); ${o.alerts_in_incident} detections in this incident, first at ${hhmm(o.detected_at)}.`
  if (t.tool === 'drill_down') return `Driving segment: ${segmentText(o.driver)}; it explains ${Math.round(Math.min(o.explained, 1) * 100)}% of the deviation (z = ${o.z}). Looked at ${o.top_candidates?.length || 0}+ candidate segments.`
  if (t.tool === 'counterfactual') return o.kind === 'rest of the book' ? `Rest of the book is normal (z = ${o.z}).` : `Without it the rest of the segment is normal (z = ${o.z}, keeps ${Math.round((o.rest_share || 0) * 100)}% of the deviation).`
  if (t.tool === 'annotate') return `${o.confirmed ? 'Confirmed' : 'Not confirmed'}: ${o.headline}`
  if (t.tool === 'alert') return `Alert (${t.input.stage}) to ${o.rows} recipients at ${hhmm(o.sent_at)}: mock, nothing sent.`
  return ''
}

export default function IncidentDetail() {
  const { id } = useParams()
  const nav = useNavigate()
  const [d, setD] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  useEffect(() => { api(`/lc04/incidents/${id}`).then((r) => { setD(r); setError('') }).catch((e) => setError(e.message)) }, [id])

  async function rebuild() {
    setBusy('Building the view and rebuilding it around this incident…')
    try {
      await api('/lc04/dashboards/generate', { method: 'POST', body: { request: '', use_llm: false } }).then((g) =>
        api('/lc04/dashboards/rebuild', { method: 'POST', body: { dashboard_id: g.dashboard_id, incident_id: id } }))
      nav('/command')
    } catch (e) { setError(e.message) }
    setBusy('')
  }

  if (error) return <><PageHeader title={id} /><div className="alert-error">{error}</div><Link to="/command/incidents">Back to incidents</Link></>
  if (!d) return <div className="empty">Loading…</div>
  const f = d.finding
  const platform = !!d.trace
  return (
    <>
      <PageHeader title={`Incident ${d.incident_id}`} subtitle={`Detected ${fmtClock(d.detected_at)}${d.refined_at ? ` · refined with an hour more data at ${hhmm(d.refined_at)}` : ''}`} />
      <p><Link to="/command/incidents">← All incidents</Link></p>

      {d.alerts?.length > 0 && (
        <div className="panel">
          <div className="cmd-title">The alert (two lines and a link)</div>
          {d.alerts.slice(0, 3).map((a, i) => <div key={i} className="alert-card"><div className="alert-time">{fmtClock(a.at)}</div><div className="grow"><b>{a.line1}</b><div className="muted">{a.line2}</div></div></div>)}
          <button className="btn btn-search btn-small" disabled={!!busy} onClick={rebuild}>Rebuild my view around this</button>
          {busy && <div className="progress">{busy}</div>}
        </div>
      )}

      {f && (
        <div className="panel">
          <div className="cmd-title">The driving segment</div>
          <div className="driver"><SegChips seg={f.segment} />
            {f.parent && JSON.stringify(f.parent) !== JSON.stringify(f.segment) && <span className="muted"> inside <SegChips seg={f.parent} /></span>}</div>
          {f.headline && <p className="headline">{f.headline}</p>}
          {platform && f.rule && (
            <>
              <div className="cards">
                <div className="card"><div className="big">{f.rule.count}</div><div className="lbl">legal notices outside the window</div></div>
                <div className="card"><div className="big">{f.rule.window[0]}:00–{f.rule.window[1]}:00</div><div className="lbl">permitted window</div></div>
                <div className="card"><div className="big">{hhmm(f.rule.sent_from)}–{hhmm(f.rule.sent_to)}</div><div className="lbl">when they were sent</div></div>
                <div className="card"><div className="big">{hhmm(f.rule.logged_from)}–{hhmm(f.rule.logged_to)}</div><div className="lbl">when the platform learned of them</div></div>
              </div>
              <div className="muted small">Rule: {f.rule.name}. Lenders affected: {Object.entries(f.per_lender || {}).map(([l, v]) => `${l} (${v.count})`).join(', ')}.</div>
              <h4>Who sent them</h4>
              <table className="grid"><thead><tr><th>Dimension</th><th>Value</th><th className="num">Share of the notices</th></tr></thead>
                <tbody>{Object.entries(f.shares || {}).flatMap(([dim, vals]) => Object.entries(vals).slice(0, 3).map(([v, s]) => <tr key={dim + v}><td>{dim}</td><td>{v}</td><td className="num">{Math.round(s * 100)}%</td></tr>))}</tbody></table>
            </>
          )}
          {platform && !f.rule && (
            <>
              <div className="cards">
                <div className="card"><div className="big">{Math.round(Math.min(f.explained, 1) * 100)}%</div><div className="lbl">of the deviation it explains</div></div>
                <div className="card"><div className="big">{Math.abs(f.z).toFixed(1)}</div><div className="lbl">standard deviations from normal</div></div>
                <div className="card"><div className="big">{f.counterfactual_ok ? 'Yes' : 'No'}</div><div className="lbl">rest back to normal without it</div></div>
                <div className="card"><div className="big">{f.confirmed ? 'Confirmed' : 'Watched'}</div><div className="lbl">drill-down verdict</div></div>
              </div>
              <div className="muted small">Counterfactual: {f.counterfactual?.kind}; {f.counterfactual?.kind === 'rest of the segment'
                ? `the rest of ${segmentText(f.parent)} is at z = ${f.counterfactual.z} and keeps ${Math.round((f.counterfactual.rest_share || 0) * 100)}% of the deviation.`
                : `everything outside it is at z = ${f.counterfactual?.z}.`}</div>
              <h4>Segments the agent compared</h4>
              <table className="grid"><thead><tr><th>Segment</th><th className="num">Share of deviation</th><th className="num">z</th></tr></thead>
                <tbody>{f.candidates?.map((c, i) => <tr key={i}><td><SegChips seg={c.segment} /></td><td className="num">{Math.round(c.explained * 100)}%</td><td className="num">{c.z}</td></tr>)}</tbody></table>
            </>
          )}
        </div>
      )}

      {platform && d.detections?.length > 0 && (
        <div className="panel">
          <div className="cmd-title">Detections in this incident ({d.detections.length})</div>
          <table className="grid"><thead><tr><th>Detected</th><th>Metric</th><th>Segment</th><th>Direction</th><th className="num">Score</th></tr></thead>
            <tbody>{d.detections.map((a, i) => <tr key={i}><td>{hhmm(a.detected_at)}</td><td>{a.metric}</td><td><SegChips seg={a.segment} /></td><td>{a.direction}</td><td className="num">{a.score}</td></tr>)}</tbody></table>
        </div>
      )}

      {platform && d.trace?.length > 0 && (
        <div className="panel">
          <div className="cmd-title">What the agent did, step by step</div>
          <ol className="trace">{d.trace.map((t) => <li key={t.step}><b>{TOOLS[t.tool] || t.tool}</b> {traceText(t)}</li>)}</ol>
        </div>
      )}
      {!platform && <p className="muted small">You see your own alert and the driving segment. Platform-wide evidence and the agent's trace are for leadership and the heads.</p>}
    </>
  )
}
