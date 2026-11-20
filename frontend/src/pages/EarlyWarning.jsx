import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, notifyNudgesChanged } from '../api.js'
import { FlagStatus, NudgeStatus, RiskBar, SignalBadge, nudgeTypeText } from '../components/EarlyWarningBits.jsx'
import PageHeader from '../components/PageHeader.jsx'
import Pager from '../components/Pager.jsx'
import { SIGNALS, fmtDate } from '../utils.js'

const PAGE_SIZE = 10
const EMPTY = { signal: '', status: '', nudge_status: '', q: '', sort: 'newest' }
const SORTS = { newest: 'Newest flag', score: 'Risk score (high to low)', oldest: 'Oldest flag' }

// "Advance one day" / "go back": drives the replay of the daily event stream
function ReplayPanel({ replay, onChanged }) {
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState(null)
  const [backTo, setBackTo] = useState('')
  useEffect(() => { if (replay) setBackTo(String(Math.max(replay.first_scored_day, replay.replay_day - 10))) }, [replay?.replay_day])

  async function advance(days) {
    const last = Math.min(replay.replay_day + days, replay.stream_end_day)
    setBusy(`Agent is running day ${replay.replay_day + 1}${last > replay.replay_day + 1 ? ` to ${last}` : ''}: sensing events, scoring accounts, double-checking borderline cases. The first run after a server start loads all events and can take a minute…`)
    setMsg(null)
    try {
      const r = await api('/bi03/replay/advance', { method: 'POST', body: { days, confirm: true } })
      setMsg({ ok: true, text: `Ran day${r.ran_days.length > 1 ? 's' : ''} ${r.ran_days.join(', ')}: ${r.new_flags} new flag${r.new_flags === 1 ? '' : 's'}, ${r.new_nudges} nudge${r.new_nudges === 1 ? '' : 's'} drafted, ${r.flags_closed} flag${r.flags_closed === 1 ? '' : 's'} closed (account became late).` })
      onChanged()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy('')
  }

  async function reset() {
    const day = Number(backTo)
    if (!window.confirm(`Go back to day ${day}? Everything the agent produced after day ${day} (scores, flags, nudges) is removed so you can replay it. This is demo data only.`)) return
    setBusy(`Going back to day ${day}…`)
    setMsg(null)
    try {
      const r = await api('/bi03/replay/reset', { method: 'POST', body: { to_day: day } })
      setMsg({ ok: true, text: `Back at day ${r.replay_day} (${fmtDate(r.replay_date)}). Removed ${r.removed.flags} flags and ${r.removed.nudges} nudges produced later.` })
      onChanged()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy('')
  }

  if (!replay) return null
  return (
    <div className="panel replay">
      <div className="row-flex">
        <div>
          <div className="muted">Simulated date</div>
          <div className="big-date">{fmtDate(replay.replay_date)} <small>day {replay.replay_day} of {replay.stream_end_day}</small></div>
        </div>
        <span className="grow" />
        <button className="btn btn-search btn-small" disabled={!!busy || !replay.can_advance} onClick={() => advance(1)}>Advance 1 day</button>
        <button className="btn btn-reset btn-small" disabled={!!busy || !replay.can_advance} onClick={() => advance(7)}>Advance 7 days</button>
        <span className="sep" />
        <label className="muted">Go back to day
          <input className="field day-input" type="number" min={replay.first_scored_day} max={replay.replay_day} value={backTo} onChange={(e) => setBackTo(e.target.value)} />
        </label>
        <button className="btn btn-reset btn-small" disabled={!!busy || !backTo || Number(backTo) >= replay.replay_day || Number(backTo) < replay.first_scored_day} onClick={reset}>Go back</button>
      </div>
      {busy && <div className="progress">{busy}</div>}
      {msg && <div className={msg.ok ? 'progress ok' : 'alert-error'}>{msg.text}</div>}
      {!replay.can_advance && !busy && <p className="muted">The data stream ends at day {replay.stream_end_day}. Go back to an earlier day to replay it.</p>}
      {replay.past_public_period && <p className="muted">Days after {replay.public_end_day} are the evaluation period of the data set. Outcomes are not recorded from this screen.</p>}
      <p className="muted small">Each day the agent reads that day's new events (salary credits, EMI payments, bureau updates) and only those available on that day. Nothing from the future is ever visible.</p>
    </div>
  )
}

export default function EarlyWarning() {
  const nav = useNavigate()
  const [draft, setDraft] = useState(EMPTY)
  const [applied, setApplied] = useState(EMPTY)
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState('')

  const loadSummary = useCallback(() => api('/bi03/summary').then(setSummary).catch((e) => setError(e.message)), [])
  const loadList = useCallback(async () => {
    const p = new URLSearchParams({ page, page_size: PAGE_SIZE, sort: applied.sort })
    ;['signal', 'status', 'nudge_status', 'q'].forEach((k) => applied[k] && p.set(k, applied[k]))
    try { setData(await api(`/bi03/flags?${p}`)); setError('') } catch (e) { setError(e.message) }
  }, [page, applied])
  useEffect(() => { loadSummary() }, [loadSummary])
  useEffect(() => { loadList() }, [loadList])

  const changed = () => { loadSummary(); loadList(); notifyNudgesChanged() }
  const search = () => { setPage(1); setApplied(draft) }
  const reset = () => { setDraft(EMPTY); setApplied(EMPTY); setPage(1) }
  const pickSignal = (s) => { const d = { ...EMPTY, signal: s }; setDraft(d); setApplied(d); setPage(1) }

  const tau = summary?.active_threshold?.value
  const fs = summary?.flags_by_status || {}
  const openFlags = (fs.open || 0) + (fs.nudged || 0)
  const bySignal = summary?.open_flags_by_signal || {}

  return (
    <>
      <PageHeader title="Early warning" subtitle="Customers who pay today but may miss an upcoming EMI"
        info="The agent scores every current account daily from salary, EMI, bureau and balance signals and flags the riskiest ones with the main reason" />

      <ReplayPanel replay={summary?.replay} onChanged={changed} />

      {summary && (
        <div className="cards">
          <div className="card"><div className="big">{summary.current_accounts.toLocaleString('en-IN')}</div><div className="lbl">Accounts watched today</div></div>
          <div className="card"><div className="big">{summary.flags_today}</div><div className="lbl">New flags today</div></div>
          <div className="card"><div className="big">{openFlags}</div><div className="lbl">Open flags</div></div>
          <div className="card"><div className="big">{summary.nudges_by_status.pending_approval || 0}</div><div className="lbl">Nudges waiting for approval</div></div>
          <div className="card"><div className="big">{summary.refresh_settled_flags}</div><div className="lbl">Flags confirmed with fresh data</div></div>
        </div>
      )}

      {Object.keys(bySignal).length > 0 && (
        <div className="panel">
          <h3>Open flags by main signal <span className="muted small">(click to filter)</span></h3>
          <div className="row-flex">
            {Object.entries(bySignal).map(([s, n]) => (
              <button key={s} className={`sig-btn sig-${s}`} onClick={() => pickSignal(s)}>{SIGNALS[s] || 'No single signal'} <b>{n}</b></button>
            ))}
          </div>
        </div>
      )}

      <div className="filters">
        <select className="select" value={draft.signal} onChange={(e) => setDraft({ ...draft, signal: e.target.value })} aria-label="Main signal">
          <option value="">Signal: All</option>
          {Object.entries(SIGNALS).filter(([k]) => k !== 'bureau_score').map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select className="select" value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value })} aria-label="Flag status">
          <option value="">Flag: All</option>
          <option value="open">New</option><option value="nudged">Nudge drafted</option><option value="closed">Closed (became late)</option>
        </select>
        <select className="select" value={draft.nudge_status} onChange={(e) => setDraft({ ...draft, nudge_status: e.target.value })} aria-label="Nudge status">
          <option value="">Nudge: All</option>
          <option value="pending_approval">Waiting for approval</option><option value="sent">Sent</option><option value="skipped">Skipped</option><option value="blocked">Blocked</option>
        </select>
        <input className="field" placeholder="Account No / Customer Name" value={draft.q}
          onChange={(e) => setDraft({ ...draft, q: e.target.value })} onKeyDown={(e) => e.key === 'Enter' && search()} />
        <select className="select" value={draft.sort} onChange={(e) => setDraft({ ...draft, sort: e.target.value })} aria-label="Sorting">
          {Object.entries(SORTS).map(([k, v]) => <option key={k} value={k}>Sort: {v}</option>)}
        </select>
        <span className="grow" />
        <button className="btn btn-search" onClick={search}>Search</button>
        <button className="btn btn-reset" onClick={reset}>Reset</button>
      </div>

      {error && <div className="alert-error">{error}</div>}

      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr><th>Account</th><th>Customer</th><th>Flagged on</th><th>Risk score</th><th>Main signal</th><th>Nudge</th></tr>
          </thead>
          <tbody>
            {data?.items.map((f) => (
              <tr key={f.flag_id} className="click" onClick={() => nav(`/early-warning/${f.flag_id}`)}>
                <td><a href={`/early-warning/${f.flag_id}`} onClick={(e) => e.preventDefault()}>{f.account_id}</a><div className="sub">{f.product}</div></td>
                <td>{f.customer_name}<div className="sub">{f.branch}</div></td>
                <td>{fmtDate(f.flagged_on)}<div className="sub"><FlagStatus status={f.status} />{f.confirmed_by_refresh ? <span className="sub"> · double-checked</span> : null}</div></td>
                <td><RiskBar score={f.score} tau={tau} /></td>
                <td><SignalBadge signal={f.dominant_signal} /></td>
                <td>{f.nudge_type ? <>{nudgeTypeText(f.nudge_type)}<div className="sub"><NudgeStatus status={f.nudge_status} /></div></> : <NudgeStatus />}</td>
              </tr>
            ))}
            {data && !data.items.length && <tr><td colSpan="6" className="empty">No flags match.</td></tr>}
            {!data && !error && <tr><td colSpan="6" className="empty">Loading…</td></tr>}
          </tbody>
        </table>
      </div>
      {data && <Pager page={page} total={data.total} pageSize={PAGE_SIZE} onPage={setPage} />}
    </>
  )
}
