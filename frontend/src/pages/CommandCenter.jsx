import { useCallback, useEffect, useRef, useState } from 'react'
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

const MAX_ALERTS = 3       // section 1 shows the newest few; the rest sit behind "show all"
const MAX_PANELS = 6       // section 2 shows the first few panels; the rest sit behind "show all"

// The thin strip under the title: what time the simulation is at, and (for leadership / ops head) the buttons that move it.
function ClockStrip({ clock, me, onChanged }) {
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
  const presets = [...(clock.demo_legal_day ? PRESETS_LEGAL : []), ...PRESETS, ...(clock.hidden_loaded ? PRESETS_HIDDEN : [])]
  return (
    <div className="clock-strip">
      <div className="row-flex">
        <span className="clock-label">Simulated time</span>
        <b className="clock-now">{fmtClock(clock.clock)}</b>
        <span className="muted small">of data to {fmtClock(clock.end)}</span>
        <span className="help-dot" tabIndex={0} title="The agent only ever sees data up to this time. Every 15 minutes it re-checks the key metrics, allows for Sundays, salary days and festivals, drills down to the driving segment and writes an alert. Alerts are mock: nothing is sent.">?</span>
        <span className="grow" />
        {admin && !clock.prepared && <button className="btn btn-search btn-small" disabled={!!busy} onClick={prepare}>Prepare replay</button>}
        {admin && clock.prepared && <>
          <button className="btn btn-search btn-small" disabled={!!busy} onClick={() => advance(15)}>+15 min</button>
          <button className="btn btn-reset btn-small" disabled={!!busy} onClick={() => advance(60)}>+1 hour</button>
          <details className="jump">
            <summary className="btn btn-reset btn-small">Jump to ▾</summary>
            <div className="jump-menu">
              {presets.map((p) => <button key={p.to} className="jump-item" disabled={!!busy} onClick={() => reset(p.to)}>{p.label}</button>)}
              <div className="jump-custom">
                <input className="field" placeholder="2026-10-15 11:00:00" value={to} onChange={(e) => setTo(e.target.value)} />
                <button className="btn btn-reset btn-small" disabled={!!busy || !to} onClick={() => reset(to)}>Go</button>
              </div>
            </div>
          </details>
        </>}
      </div>
      {busy && <div className="progress">{busy}</div>}
      {msg && !msg.ok && <div className="alert-error">{msg.text}</div>}
      {r?.new_alerts?.length > 0 && r.new_alerts.map((a) => (
        <div key={a.incident_id + a.created_at} className="progress ok"><b>New alert {hhmm(a.created_at)}:</b> {a.line1}</div>
      ))}
      {r && !r.new_alerts && r.clock && <div className="progress ok">Clock is now {fmtClock(r.clock)}: {r.alerts} alert rows, {r.incidents} incidents written so far.</div>}
      {!admin && <p className="muted small" style={{ margin: '6px 0 0' }}>The clock is moved by leadership or the operations head. You see what the agent has found up to this time.</p>}
    </div>
  )
}

function Section({ n, title, hint, children, className = '' }) {
  return (
    <section className={'cc-sec ' + className}>
      <div className="cc-sec-head"><span className="cc-num">{n}</span><h2>{title}</h2>{hint && <span className="muted small">{hint}</span>}</div>
      {children}
    </section>
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
  const [allAlerts, setAllAlerts] = useState(false)
  const [allPanels, setAllPanels] = useState(false)
  const liveRef = useRef(null)

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
      setView(v); setError(''); setAllPanels(false)
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
  const showView = () => liveRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  async function rebuild(incident_id) {
    setBusy('Rebuilding the view around the finding…')
    try {
      const v = await api('/lc04/dashboards/rebuild', { method: 'POST', body: { dashboard_id: view.dashboard_id, incident_id } })
      setView(v); setAllPanels(false); setTimeout(showView, 150)
    } catch (e) { setError(e.message) }
    setBusy('')
  }

  const shownAlerts = allAlerts ? alerts : alerts.slice(0, MAX_ALERTS)
  const panels = data?.panels || []
  const shownPanels = allPanels || panels.length <= MAX_PANELS + 2 ? panels : panels.slice(0, MAX_PANELS)

  return (
    <>
      <PageHeader title="Command center" subtitle="What is wrong, how the business looks, and a view built for you"
        info="Every number is a governed metric, limited to your role and your lenders. The view refreshes every 15 minutes." />
      {error && <div className="alert-error">{error}</div>}
      <ClockStrip clock={clock} me={me} onChanged={clockChanged} />

      <Section n="1" title="Needs attention" hint={alerts.length ? `${alerts.length} alert${alerts.length > 1 ? 's' : ''} for you` : ''} className="sec-alerts">
        {alerts.length === 0 ? (
          <div className="all-clear">All clear{clock ? ` as of ${fmtClock(clock.clock)}` : ''}. Nothing has moved outside its normal range for you.</div>
        ) : (
          <>
            {shownAlerts.map((a) => (
              <div className="alert-card" key={a.incident_id + a.at + a.line1}>
                <div className="alert-time">{fmtClock(a.at)}</div>
                <div className="grow"><b>{a.line1}</b><div className="muted">{a.line2}</div></div>
                <div className="alert-actions">
                  <Link className="btn btn-reset btn-small" to={`/command/incidents/${a.incident_id}`}>Annotated view</Link>
                  {view && <button className="btn btn-search btn-small" disabled={!!busy} onClick={() => rebuild(a.incident_id)}>Rebuild my view around it</button>}
                </div>
              </div>
            ))}
            {alerts.length > MAX_ALERTS && (
              <button className="link-btn" onClick={() => setAllAlerts(!allAlerts)}>{allAlerts ? 'Show fewer' : `Show all ${alerts.length} alerts`}</button>
            )}
          </>
        )}
        {busy && busy.startsWith('Rebuilding') && <div className="progress">{busy}</div>}
      </Section>

      <div ref={liveRef} />
      <Section n="2" title="How the business looks" className="sec-live"
        hint={view ? `${view.spec.title} · ${view.spec.window.replace('_', ' ')}` : ''}>
        {data ? (
          <>
            <div className="muted small live-line">
              Up to {fmtClock(data.clock)} · refreshed {refreshed?.toLocaleTimeString('en-GB')} · <button className="link-btn" onClick={refresh}>refresh now</button>
              {view && <span className={'cbadge ' + (view.source === 'llm' ? 'ai' : view.source === 'rebuilt' ? 'rb' : 'fb')} style={{ marginLeft: 8 }}>
                {view.source === 'llm' ? 'AI draft, checked' : view.source === 'rebuilt' ? 'Rebuilt around a finding' : 'Default view'}
              </span>}
            </div>
            {view?.notes?.length > 0 && (
              <details className="view-notes"><summary>{view.notes.length} note{view.notes.length > 1 ? 's' : ''} on this view</summary>
                <ul className="notes">{view.notes.map((n, i) => <li key={i}>{n}</li>)}</ul></details>
            )}
            <div className="cmd-grid">
              {shownPanels.map((p, i) => <Panel key={i} p={p} markers={data.markers} />)}
            </div>
            {panels.length > MAX_PANELS + 2 && (
              <button className="link-btn" onClick={() => setAllPanels(!allPanels)}>{allPanels ? 'Show fewer panels' : `Show all ${panels.length} panels`}</button>
            )}
          </>
        ) : <div className="empty">{busy || 'Loading your view…'}</div>}
      </Section>

      <Section n="3" title="Ask for a different view" className="sec-build">
        <details className="build-box" open={!!request || undefined}>
          <summary>Describe what you want to see in plain words</summary>
          <textarea className="field req-box" rows={2} value={request} placeholder="e.g. “Which payment gateway is failing and how much money is affected?”"
            onChange={(e) => setRequest(e.target.value)} />
          <div className="row-flex" style={{ marginTop: 8 }}>
            <button className="btn btn-search" disabled={!!busy || !request.trim()} onClick={async () => { await build(request, useAi); setTimeout(showView, 150) }}>Build view</button>
            {examples.slice(0, 3).map((x) => <button key={x.id} className="chip-btn" title={x.text} onClick={() => setRequest(x.text)}>{x.text.length > 46 ? x.text.slice(0, 44) + '…' : x.text}</button>)}
          </div>
          <details className="adv">
            <summary>Advanced</summary>
            <label className="muted"><input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} /> Use the AI (without it, you get the default view for your role)</label>
          </details>
          {busy && !busy.startsWith('Rebuilding') && <div className="progress">{busy}</div>}
        </details>
      </Section>
    </>
  )
}
