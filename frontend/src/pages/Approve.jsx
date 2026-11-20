import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, notifyRecsChanged } from '../api.js'
import { ChannelChip, Confidence, LabelBadge } from '../components/Badges.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { useFetch } from '../hooks.js'

const TABS = [['pending', 'Waiting for you'], ['approved', 'Approved'], ['skipped', 'Skipped'], ['log', 'Send log']]
const EXAMPLES = ['Approve all who went to the dispute team', 'Approve the reminders to customers who forgot', 'Approve everything above 85% confidence']
const CHANNEL = { email: 'Email', whatsapp: 'WhatsApp', sms: 'SMS' }
const KIND = { customer_reminder: 'Reminder to the customer', team_handoff: 'Handoff to the team', pause_calls: 'Collection calls' }

const when = (s) => (s ? new Date(s.replace(' ', 'T') + 'Z').toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '')

// one line about what happened to a recommendation
function stepText(s) {
  const what = KIND[s.kind] || s.kind
  const st = { sent: `sent by ${CHANNEL[s.channel] || 'email'}`, mock_sent: 'mock only, nothing left the system', already_done: 'already done', blocked: `blocked${s.error ? ': ' + s.error : ''}`,
    failed: `failed${s.error ? ': ' + s.error : ''}`, paused: 'paused', already_paused: 'already paused' }[s.status] || s.status
  return `${what}${s.team ? ' (' + s.team + ')' : ''}: ${st}`
}

export default function Approve() {
  const [tab, setTab] = useState('pending')
  const recs = useFetch(`/bi02/recommendations?status=${tab === 'log' ? 'skipped' : tab}`)       // the send-log tab does not show this list
  const [actionError, setActionError] = useState('')
  const [out, setOut] = useState(null)               // /outreach/status, null = sending is not available (the page then only records decisions)
  const [summary, setSummary] = useState(null)
  const [logRows, setLogRows] = useState([])
  const [command, setCommand] = useState('')
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState('')
  const [rowMsg, setRowMsg] = useState({})

  useEffect(() => { api('/outreach/status').then(setOut).catch(() => setOut(null)) }, [])
  const loadSummary = useCallback(() => { if (out) api('/outreach/summary').then(setSummary).catch(() => {}) }, [out])
  const loadLog = useCallback(() => { if (out) api('/outreach/log?limit=500').then(setLogRows).catch(() => {}) }, [out])
  useEffect(() => { if (tab === 'pending') loadSummary() }, [tab, loadSummary])
  useEffect(() => { if (tab === 'log' || tab === 'approved') loadLog() }, [tab, loadLog])

  const refresh = async () => { await recs.reload?.(); loadSummary(); loadLog(); notifyRecsChanged() }

  async function decide(id, status) {                   // skip, or the old record-only approve when sending is not available
    setActionError('')
    try { await api(`/bi02/recommendations/${id}/status`, { method: 'POST', body: { status } }); await refresh() } catch (e) { setActionError(e.message) }
  }

  async function sendOne(id) {
    setActionError('')
    setBusy(`Sending #${id}…`)
    try {
      const r = await api(`/outreach/recommendations/${id}/send`, { method: 'POST' })
      setRowMsg((m) => ({ ...m, [id]: { ok: r.ok, text: r.steps.map(stepText).join(' · ') } }))
      await refresh()
    } catch (e) { setRowMsg((m) => ({ ...m, [id]: { ok: false, text: e.message } })) }
    setBusy('')
  }

  async function ask(text) {
    const c = (text ?? command).trim()
    if (!c) return
    setCommand(c); setPreview(null); setResult(null); setActionError(''); setBusy('The agent is working out which recommendations you mean…')
    try { setPreview(await api('/outreach/bulk/preview', { method: 'POST', body: { command: c } })) } catch (e) { setActionError(e.message) }
    setBusy('')
  }

  async function confirm() {
    setBusy('Sending… each message goes through the safety checks first.')
    try {
      const r = await api('/outreach/bulk/confirm', { method: 'POST', body: { plan_id: preview.plan_id } })
      setResult(r); setPreview(null); setCommand('')
      await refresh()
    } catch (e) { setActionError(e.message) }
    setBusy('')
  }

  const live = out?.mode === 'live'
  const sentFor = (id) => logRows.filter((l) => l.rec_id === id)

  return (
    <>
      <PageHeader title="To approve" subtitle="Recommended next actions" info="The agent recommends. A person approves. Approving sends the action, with safety checks first." />

      {out ? (
        <div className="note">
          {live
            ? <><b>Live: approving really sends.</b> In this demo every message goes only to the test recipient ({out.test_recipient.email}, {out.test_recipient.phone}), never to a customer.</>
            : <><b>Mock mode: nothing is sent.</b> Approving records the decision and the send log shows what would have gone out.</>}
          {' '}Channels: {Object.entries(out.channels).map(([c, v]) => `${CHANNEL[c]} ${v.configured ? 'ready' : 'not set up'}`).join(' · ')}.
        </div>
      ) : (
        <div className="note"><b>Nothing is sent until a person approves, and approving here only records the decision.</b> Sending is not connected.</div>
      )}

      <div className="tabs">
        {TABS.filter(([k]) => k !== 'log' || out).map(([k, v]) => <button key={k} className={'tab' + (tab === k ? ' active' : '')} onClick={() => setTab(k)}>{v}</button>)}
      </div>

      {(recs.error || actionError) && <div className="alert-error">{recs.error || actionError}</div>}
      {busy && <div className="progress">{busy}</div>}

      {tab === 'pending' && out && summary && (
        <div className="panel">
          <p style={{ margin: '0 0 8px' }}>{summary.text}</p>
          <div className="row-flex">
            <input className="field" style={{ flex: 1, minWidth: 280 }} placeholder="Tell the agent what to approve, e.g. approve all who went to the dispute team" value={command}
              onChange={(e) => setCommand(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && ask()} />
            <button className="btn btn-search btn-small" disabled={!!busy || !command.trim()} onClick={() => ask()}>Preview</button>
          </div>
          <div className="row-flex">
            {EXAMPLES.map((x) => <button key={x} className="link-btn small" disabled={!!busy} onClick={() => ask(x)}>{x}</button>)}
          </div>

          {preview && (
            <div style={{ marginTop: 10 }}>
              <div className="progress ok">{preview.text}</div>
              <div className="table-wrap">
                <table className="grid">
                  <thead><tr><th>Customer</th><th>What the agent will do</th><th>Confidence</th></tr></thead>
                  <tbody>
                    {preview.will_act.map((x) => <tr key={x.rec_id}><td>{x.customer}<div className="sub">{x.account_id}</div></td><td>{x.will_do}</td><td>{Math.round(x.confidence * 100)}%</td></tr>)}
                    {!preview.will_act.length && <tr><td colSpan="3" className="empty">Nothing to act on.</td></tr>}
                  </tbody>
                </table>
              </div>
              {preview.left_out.length > 0 && (
                <details className="muted small" style={{ marginTop: 6 }}>
                  <summary>{preview.left_out.length} left out</summary>
                  {preview.left_out.map((x) => <div key={x.rec_id}>{x.customer} ({x.account_id}): {x.reason}</div>)}
                </details>
              )}
              <div className="row-flex">
                <button className="btn btn-search btn-small" disabled={!!busy || !preview.will_act.length} onClick={confirm}>{live ? 'Confirm and send' : 'Confirm'}</button>
                <button className="btn btn-reset btn-small" disabled={!!busy} onClick={() => setPreview(null)}>Cancel</button>
              </div>
            </div>
          )}

          {result && (
            <div style={{ marginTop: 10 }}>
              <div className="progress ok">{result.text}</div>
              <div className="table-wrap">
                <table className="grid">
                  <thead><tr><th>Customer</th><th>Result</th></tr></thead>
                  <tbody>{result.results.map((r) => <tr key={r.rec_id}><td>{r.customer}<div className="sub">{r.account_id}</div></td><td>{r.steps.map(stepText).join(' · ') || r.error}</td></tr>)}</tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {tab === 'log' && (
        <div className="table-wrap">
          <table className="grid">
            <thead><tr><th>When</th><th>For</th><th>What</th><th>Channel</th><th>Result</th><th>Went to</th></tr></thead>
            <tbody>
              {logRows.map((l) => (
                <tr key={l.id}>
                  <td>{when(l.created_at)}</td>
                  <td>{l.intended_for || '–'}<div className="sub">{l.account_id}</div></td>
                  <td>{KIND[l.purpose] || l.purpose || '–'}</td>
                  <td>{CHANNEL[l.channel] || l.channel}</td>
                  <td>{{ sent: 'Sent', mock_sent: 'Mock only', blocked: 'Blocked', failed: 'Failed' }[l.status]}{l.error && <div className="sub">{l.error}</div>}</td>
                  <td>{l.recipient_hint || '–'}</td>
                </tr>
              ))}
              {!logRows.length && <tr><td colSpan="6" className="empty">Nothing has been sent yet.</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab !== 'log' && recs.loading && !recs.data && <div className="empty">Loading…</div>}
      {tab !== 'log' && recs.data && !recs.data.length && <div className="empty">Nothing here.</div>}

      {tab !== 'log' && (
        <div className="rec-list">
          {recs.data?.map((r) => {
            const done = sentFor(r.id)
            const reallySent = done.find((l) => l.status === 'sent')
            const mockOnly = !reallySent && done.find((l) => l.status === 'mock_sent')
            return (
              <div key={r.id} className="panel rec">
                <div className="row-flex">
                  <ChannelChip channel={r.channel} />
                  <b><Link to={`/accounts/${r.account_id}`}>{r.customer_name}</Link></b>
                  <span className="muted">{r.account_id}</span>
                  <span className="grow" />
                  <LabelBadge label={r.label} /> <Confidence value={r.confidence} />
                </div>
                <p className="reason">{r.action}</p>
                <p className="muted">{r.reason}</p>
                {r.reasoning_summary && <p className="why">{r.reasoning_summary}</p>}
                {tab === 'pending' && (
                  <div className="row-flex">
                    {out
                      ? <button className="btn btn-search btn-small" disabled={!!busy} onClick={() => sendOne(r.id)}>Approve and send</button>
                      : <button className="btn btn-search btn-small" onClick={() => decide(r.id, 'approved')}>Approve</button>}
                    <button className="btn btn-reset btn-small" disabled={!!busy} onClick={() => decide(r.id, 'skipped')}>Skip</button>
                  </div>
                )}
                {tab === 'approved' && out && (
                  <div className="row-flex">
                    <span className="muted small">{reallySent ? `Sent by ${CHANNEL[reallySent.channel]} on ${when(reallySent.created_at)}` : mockOnly ? 'Mock only: nothing left the system' : 'Approved, not sent yet'}</span>
                    {!reallySent && !mockOnly && <button className="btn btn-search btn-small" disabled={!!busy} onClick={() => sendOne(r.id)}>Send now</button>}
                  </div>
                )}
                {rowMsg[r.id] && <div className={rowMsg[r.id].ok ? 'progress ok' : 'alert-error'}>{rowMsg[r.id].text}</div>}
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
