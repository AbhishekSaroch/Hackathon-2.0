import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, getLcUser, notifyLcClockChanged } from '../api.js'
import { Panel, fmtClock, hhmm } from '../components/CommandBits.jsx'
import PageHeader from '../components/PageHeader.jsx'

const PRESETS = [
  { label: 'Oct 15, 10:45: just before a dialer outage', to: '2026-10-15 10:45:00' },
  { label: 'Oct 19, 14:45: before a gateway failure', to: '2026-10-19 14:45:00' },
  { label: 'Oct 23, 10:15: before duplicated events', to: '2026-10-23 10:15:00' },
  { label: 'Oct 14, 09:45: before wrong dispositions', to: '2026-10-14 09:45:00' },
]

const PRESETS_LEGAL = [    // the demo day: legal notices sent at 3 AM, outside the permitted 08:00-20:00 window, logged at 06:05-06:14
  { label: 'Nov 20, 06:00: before the sender’s log arrives', to: '2026-11-20 06:00:00' },
  { label: 'Nov 20, 06:17: legal notices sent at 3 AM (outside 08:00-20:00)', to: '2026-11-20 06:17:00' },
]
const PRESETS_HIDDEN = [   // only offered after the single final run has loaded the hidden days
  { label: 'Oct 28, 10:15: before a city dialer outage (hidden set)', to: '2026-10-28 10:15:00' },
  { label: 'Oct 29, 09:15: before wrong dispositions (hidden set)', to: '2026-10-29 09:15:00' },
  { label: 'Oct 30, 13:45: before a gateway failure (hidden set)', to: '2026-10-30 13:45:00' },
  { label: 'Nov 2, 10:45: before a feed stall (hidden set)', to: '2026-11-02 10:45:00' },
]

function ReplayPanel({ clock, me, onChanged }) {
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState(null)
  const [to, setTo] = useState('')
  const admin = me && ['cn_leadership', 'ops_head'].includes(me.role_id)

  async function run(label, fn) {
    setBusy(label); setMsg(null)
    try { const r = await fn(); setMsg({ ok: true, r }); onChanged() } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy('')
  }
  const prepare = () => run('Preparing the replay: detecting anomalies and drilling down over the whole stream. This takes about a minute the first time…',
    () => api('/lc04/replay/prepare', { method: 'POST' }))
  const advance = (minutes) => run(`Moving the clock ${minutes} minutes: detecting, drilling down, writing alerts…`,
    () => api('/lc04/replay/advance', { method: 'POST', body: { minutes } }))
  const reset = (t) => run('Going back…', () => api('/lc04/replay/reset', { method: 'POST', body: { to: t } }))

  if (!clock) return null
  const r = msg?.r
  return (
    <div className="panel replay">
      <div className="row-flex">
        <div>
          <div className="muted">Simulated time</div>
          <div className="big-date">{fmtClock(clock.clock)} <small>stream ends {fmtClock(clock.end)}</small></div>
        </div>
        <span className="grow" />
        {admin && !clock.prepared && <button className="btn btn-search btn-small" disabled={!!busy} onClick={prepare}>Prepare replay</button>}
        {admin && clock.prepared && <>
          <button className="btn btn-search btn-small" disabled={!!busy} onClick={() => advance(15)}>Advance 15 min</button>
          <button className="btn btn-reset btn-small" disabled={!!busy} onClick={() => advance(60)}>Advance 1 hour</button>
        </>}
      </div>
      {admin && clock.prepared && (
        <div className="row-flex" style={{ marginTop: 10 }}>
          <span className="muted">Jump to:</span>
          {[...(clock.demo_legal_day ? PRESETS_LEGAL : []), ...PRESETS, ...(clock.hidden_loaded ? PRESETS_HIDDEN : [])].map((p) => <button key={p.to} className="chip-btn" disabled={!!busy} onClick={() => reset(p.to)}>{p.label}</button>)}
          <span className="grow" />
          <input className="field" style={{ width: 170 }} placeholder="2026-10-15 11:00:00" value={to} onChange={(e) => setTo(e.target.value)} />
          <button className="btn btn-reset btn-small" disabled={!!busy || !to} onClick={() => reset(to)}>Go to time</button>
        </div>
      )}
      {busy && <div className="progress">{busy}</div>}
      {msg && !msg.ok && <div className="alert-error">{msg.text}</div>}
      {r?.new_alerts?.length > 0 && r.new_alerts.map((a) => (
        <div key={a.incident_id + a.created_at} className="progress ok"><b>New alert {hhmm(a.created_at)}:</b> {a.line1}</div>
      ))}
      {r && !r.new_alerts && r.clock && <div className="progress ok">Clock is now {fmtClock(r.clock)}: {r.alerts} alert rows, {r.incidents} incidents written so far.</div>}
      {!admin && <p className="muted small">The clock is moved by leadership or the operations head. You see what the agent has found up to this time.</p>}
      <p className="muted small">The agent only ever sees data up to the simulated time. Every 15 minutes it re-checks the key metrics, accounts for Sundays, salary days and festivals, drills down to the driving segment and writes an alert. Alerts are mock: nothing is sent.</p>
    </div>
  )
}

export default function CommandCenter() {
  const [me, setMe] = useState(null)
  const [clock, setClock] = useState(null)
  const [alerts, setAlerts] = useState([])
  const [examples, setExamples] = useState([])
  const [view, setView] = useState(null)         // { dashboard_id, spec, source, notes }
  const [data, setData] = useState(null)
  const [request, setRequest] = useState('')
  const [useAi, setUseAi] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [refreshed, setRefreshed] = useState(null)
  const [user, setUser] = useState(getLcUser())

  useEffect(() => { const h = () => { setUser(getLcUser()); setView(null); setData(null) }; window.addEventListener('lc04-user-changed', h); return () => window.removeEventListener('lc04-user-changed', h) }, [])

  const loadMeta = useCallback(async () => {
    try {
      const [m, c, a, ex] = await Promise.all([api('/lc04/me'), api('/lc04/clock'), api('/lc04/alerts'), api('/lc04/requests')])
      setMe(m); setClock(c); setAlerts(a); setExamples(ex); setError('')
    } catch (e) { setError(e.message) }
  }, [user])
  useEffect(() => { loadMeta() }, [loadMeta])

  const build = useCallback(async (text, ai = false) => {
    setBusy(ai ? 'Building your view: the AI drafts it, the system checks every metric against the governed catalog and your role…' : 'Building your view…')
    try {
      const v = await api('/lc04/dashboards/generate', { method: 'POST', body: { request: text, use_llm: ai } })
      setView(v); setError('')
    } catch (e) { setError(e.message) }
    setBusy('')
  }, [])
  useEffect(() => { if (me && !view) build('', false) }, [me])          // every role starts with a view of its own

  const refresh = useCallback(async () => {
    if (!view) return
    try { setData(await api(`/lc04/dashboards/${view.dashboard_id}/data`)); setRefreshed(new Date()); setError('') } catch (e) { setError(e.message) }
  }, [view])
  useEffect(() => { refresh() }, [refresh, clock?.clock])
  useEffect(() => { const t = setInterval(() => { loadMeta(); refresh() }, 900000); return () => clearInterval(t) }, [refresh, loadMeta])   // every 15 minutes

  const clockChanged = () => { loadMeta(); notifyLcClockChanged() }

  async function rebuild(incident_id) {
    setBusy('Rebuilding the view around the finding…')
    try {
      const v = await api('/lc04/dashboards/rebuild', { method: 'POST', body: { dashboard_id: view.dashboard_id, incident_id } })
      setView(v)
    } catch (e) { setError(e.message) }
    setBusy('')
  }

  return (
    <>
      <PageHeader title="Command center" subtitle="A live view that builds itself around what you need to act on"
        info="Every number is a governed metric, limited to your role and your lenders. The view refreshes every 15 minutes." />
      {error && <div className="alert-error">{error}</div>}
      <ReplayPanel clock={clock} me={me} onChanged={clockChanged} />

      {alerts.length > 0 && (
        <div className="panel">
          <div className="cmd-title">Alerts for you ({alerts.length})</div>
          {alerts.slice(0, 4).map((a) => (
            <div className="alert-card" key={a.incident_id + a.at + a.line1}>
              <div className="alert-time">{fmtClock(a.at)}</div>
              <div className="grow"><b>{a.line1}</b><div className="muted">{a.line2}</div></div>
              <div className="alert-actions">
                <Link className="btn btn-reset btn-small" to={`/command/incidents/${a.incident_id}`}>Annotated view</Link>
                {view && me?.role_id !== undefined && <button className="btn btn-search btn-small" disabled={!!busy} onClick={() => rebuild(a.incident_id)}>Rebuild my view around it</button>}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="panel">
        <div className="cmd-title">Build a view</div>
        <textarea className="field req-box" rows={2} value={request} placeholder="Ask in plain words, e.g. “Which payment gateway is failing and how much money is affected?”"
          onChange={(e) => setRequest(e.target.value)} />
        <div className="row-flex" style={{ marginTop: 8 }}>
          <button className="btn btn-search" disabled={!!busy || !request.trim()} onClick={() => build(request, useAi)}>Build view</button>
          <label className="muted"><input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} /> Use the AI (without it, a default view for your role)</label>
          <span className="grow" />
          {examples.map((x) => <button key={x.id} className="chip-btn" title={x.text} onClick={() => setRequest(x.text)}>{x.text.length > 46 ? x.text.slice(0, 44) + '…' : x.text}</button>)}
        </div>
        {busy && <div className="progress">{busy}</div>}
        {view && (
          <div className="view-meta">
            <span className={'cbadge ' + (view.source === 'llm' ? 'ai' : view.source === 'rebuilt' ? 'rb' : 'fb')}>
              {view.source === 'llm' ? 'AI draft, checked' : view.source === 'rebuilt' ? 'Rebuilt around a finding' : 'Default view'}
            </span>
            <span className="muted"> {view.spec.title} · {view.spec.panels.length} panels · {view.spec.window.replace('_', ' ')} · refreshes every {view.spec.refresh_seconds / 60} min{view.ms != null ? ` · built in ${view.ms} ms` : ''}</span>
            {view.notes?.length > 0 && <ul className="notes">{view.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
          </div>
        )}
      </div>

      {data ? (
        <>
          <div className="muted small" style={{ marginBottom: 6 }}>
            Showing {data.window.kind.replace('_', ' ')} up to {fmtClock(data.clock)} · refreshed {refreshed?.toLocaleTimeString('en-GB')} ·{' '}
            <button className="link-btn" onClick={refresh}>refresh now</button>
          </div>
          <div className="cmd-grid">
            {data.panels.map((p, i) => <Panel key={i} p={p} markers={data.markers} />)}
          </div>
        </>
      ) : <div className="empty">{busy || 'Loading your view…'}</div>}
    </>
  )
}
