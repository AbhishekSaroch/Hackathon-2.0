import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, notifyNudgesChanged } from '../api.js'
import { GateChecks, NudgeStatus, SignalBadge, nudgeTypeText } from '../components/EarlyWarningBits.jsx'
import PageHeader from '../components/PageHeader.jsx'
import Pager from '../components/Pager.jsx'
import { useFetch } from '../hooks.js'
import { fmtDate } from '../utils.js'

const PAGE_SIZE = 10
const TABS = [['pending_approval', 'Waiting for you'], ['sent', 'Sent'], ['skipped', 'Skipped'], ['blocked', 'Blocked by the gate'], ['gate', 'Test the gate']]
const PUSHY = 'Asha, turant Rs 5000 bharo warna legal action hoga. Aapka salary late hai, aap default karne wale ho.'

// Type any message and see whether the compliance gate would let it out.
function GateTester() {
  const [message, setMessage] = useState('')
  const [hour, setHour] = useState(10)
  const [recent, setRecent] = useState(false)
  const [current, setCurrent] = useState(true)
  const [res, setRes] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const call = (body) => api('/bi03/gate/test', { method: 'POST', body })
  async function check() {
    setBusy(true); setError('')
    try { setRes(await call({ message, send_hour: Number(hour), recent_nudges: recent ? 1 : 0, account_is_current: current })) }
    catch (e) { setError(e.message) }
    setBusy(false)
  }
  async function loadApproved() {
    setError('')
    try { const r = await call({ message: '' }); setMessage(r.expected_message); setRes(null) } catch (e) { setError(e.message) }
  }

  return (
    <div className="panel">
      <h3>Try a message on the compliance gate</h3>
      <p className="muted">Every customer message must pass this gate before it can be sent. It also runs again at the moment of sending. Type anything, or use the two samples.</p>
      <div className="row-flex">
        <button className="btn btn-reset btn-small" onClick={loadApproved}>Use an approved message</button>
        <button className="btn btn-reset btn-small" onClick={() => { setMessage(PUSHY); setRes(null) }}>Try a pushy message</button>
      </div>
      <textarea className="field gate-text" rows="3" value={message} placeholder="Message to the customer…" onChange={(e) => setMessage(e.target.value)} />
      <div className="row-flex">
        <label className="muted">Send time
          <select className="select" value={hour} onChange={(e) => setHour(e.target.value)}>
            {Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{String(h).padStart(2, '0')}:00</option>)}
          </select>
        </label>
        <label className="muted"><input type="checkbox" checked={recent} onChange={(e) => setRecent(e.target.checked)} /> Customer already got a nudge in the last 7 days</label>
        <label className="muted"><input type="checkbox" checked={!current} onChange={(e) => setCurrent(!e.target.checked)} /> Account is already past due</label>
        <span className="grow" />
        <button className="btn btn-search btn-small" disabled={busy || !message.trim()} onClick={check}>Check message</button>
      </div>
      {error && <div className="alert-error">{error}</div>}
      {res && (
        <div className={'gate-result ' + (res.passed ? 'pass' : 'fail')}>
          <h3>{res.passed ? 'Passed: this message could be sent' : `Blocked: ${res.failed.length} check${res.failed.length === 1 ? '' : 's'} failed`}</h3>
          <GateChecks checks={res.checks} />
          {!res.passed && <p className="muted small">The nearest approved wording would be: "{res.expected_message}"</p>}
        </div>
      )}
    </div>
  )
}

export default function Nudges() {
  const [tab, setTab] = useState('pending_approval')
  const [page, setPage] = useState(1)
  const [actionError, setActionError] = useState('')
  const [busyId, setBusyId] = useState(null)
  const isGate = tab === 'gate'
  const { data, error, loading, reload } = useFetch(isGate ? '/bi03/summary' : `/bi03/nudges?status=${tab}&page=${page}&page_size=${PAGE_SIZE}`)

  async function act(id, kind) {
    setBusyId(id); setActionError('')
    try { await api(`/bi03/nudges/${id}/${kind}`, { method: 'POST' }); await reload(); notifyNudgesChanged() }
    catch (e) { setActionError(e.message) }
    setBusyId(null)
  }
  const pick = (k) => { setTab(k); setPage(1); setActionError('') }

  return (
    <>
      <PageHeader title="Nudges" subtitle="Helpful messages for customers the agent flagged"
        info="Messages come only from approved templates, in Hinglish. A person approves. Nothing reaches a real customer" />
      <div className="note">
        <b>Nothing here reaches a real customer.</b> Approving sends to a test number only (mock). The wording is never written freely by an AI:
        it is an approved template filled with the customer's own details, and it must pass the compliance gate first.
      </div>

      <div className="tabs">
        {TABS.map(([k, v]) => <button key={k} className={'tab' + (tab === k ? ' active' : '')} onClick={() => pick(k)}>{v}</button>)}
      </div>

      {isGate ? <GateTester /> : (
        <>
          {(error || actionError) && <div className="alert-error">{error || actionError}</div>}
          {!data?.items && !error && <div className="empty">Loading…</div>}
          {data?.items && !data.items.length && <div className="empty">Nothing here.</div>}

          <div className="rec-list">
            {data?.items?.map((n) => (
              <div key={n.nudge_id} className="panel rec">
                <div className="row-flex">
                  <span className="chip">{nudgeTypeText(n.nudge_type)}</span>
                  <b><Link to={`/early-warning/${n.flag_id}`}>{n.customer_name}</Link></b>
                  <span className="muted">{n.account_id}</span>
                  <span className="grow" />
                  <SignalBadge signal={n.dominant_signal} /> <NudgeStatus status={n.status} />
                </div>
                <blockquote className="msg">{n.message || '(no message: blocked by the gate)'}</blockquote>
                <div className="row-flex">
                  <span className={'badge ' + (n.gate.passed ? 'b-ok' : 'b-wont_pay')}>
                    Gate: {n.gate.passed ? `${n.gate.checks.length} of ${n.gate.checks.length} checks passed` : `failed ${n.gate.failed.join(', ')}`}
                  </span>
                  <span className="muted small">template {n.template_id || '–'}{n.sent_at ? ` · sent ${fmtDate(n.sent_at)} · ${n.dispatch_ref}` : ''}</span>
                  <span className="grow" />
                  {n.status === 'pending_approval' && (
                    <>
                      <button className="btn btn-search btn-small" disabled={busyId === n.nudge_id} onClick={() => act(n.nudge_id, 'approve')}>Approve and send</button>
                      <button className="btn btn-reset btn-small" disabled={busyId === n.nudge_id} onClick={() => act(n.nudge_id, 'skip')}>Skip</button>
                    </>
                  )}
                </div>
                {!n.gate.passed && <GateChecks checks={n.gate.checks.filter((c) => !c.passed)} />}
              </div>
            ))}
          </div>
          {data?.items && <Pager page={page} total={data.total} pageSize={PAGE_SIZE} onPage={setPage} />}
        </>
      )}
    </>
  )
}
