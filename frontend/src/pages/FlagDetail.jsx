import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, notifyNudgesChanged } from '../api.js'
import { FlagStatus, GateChecks, NudgeStatus, OutcomeText, SignalBadge, nudgeTypeText } from '../components/EarlyWarningBits.jsx'
import PageHeader from '../components/PageHeader.jsx'
import TimelineChart from '../components/TimelineChart.jsx'
import { useFetch } from '../hooks.js'
import { fmtDate, inr, signalText } from '../utils.js'

const REFRESH_TEXT = {
  confirmed: 'The score was close to the alert threshold, so the agent pulled fresh data first. The flag was confirmed.',
  promoted: 'The score was just below the alert threshold. Fresh data pushed it over, so the account was caught early.',
}
const TOOL_TEXT = { bureau_refresh: 'a fresh bureau report', aa_statement: 'a fresh bank statement (Account Aggregator, mock)' }

function Fact({ k, v }) {
  return <div className="fact"><span>{k}</span><b>{v}</b></div>
}

export default function FlagDetail() {
  const { id } = useParams()
  const { data, error, loading, reload } = useFetch(`/bi03/flags/${id}`)
  const { data: summary } = useFetch('/bi03/summary')
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState(false)

  async function act(kind) {
    setBusy(true); setActionError('')
    try {
      await api(`/bi03/nudges/${data.nudge.nudge_id}/${kind}`, { method: 'POST' })
      await reload(); notifyNudgesChanged()
    } catch (e) { setActionError(e.message) }
    setBusy(false)
  }

  if (loading && !data) return <div className="empty">Loading…</div>
  if (error) return <><PageHeader title="Flag" /><div className="alert-error">{error}</div><Link to="/early-warning">Back to early warning</Link></>

  const { flag: f, signals, nudge, outcome, timeline } = data
  const tau = summary?.active_threshold?.value
  const drops = Object.entries(signals || {})
    .filter(([k, v]) => k.startsWith('drop_') && k !== 'drop_bureau_score' && typeof v === 'number' && v > 0.001)   // bureau score is a summary of the others, never shown as a driver
    .map(([k, v]) => [k.replace('drop_', ''), v]).sort((a, b) => b[1] - a[1])
  const maxDrop = Math.max(0.0001, ...drops.map((d) => d[1]))
  const refresh = signals?.refresh
  const g = nudge?.gate_result
  const passedN = g?.checks?.filter((c) => c.passed).length

  return (
    <>
      <PageHeader title={f.customer_name} subtitle={`${f.account_id} · ${f.product} · ${f.branch}`} info="Why this customer was flagged and what the agent did about it" />
      <p><Link to="/early-warning">← Back to early warning</Link></p>

      <div className="toolbar">
        <FlagStatus status={f.status} />
        <span className="muted">Flagged on {fmtDate(f.flagged_on)} · risk score {f.score.toFixed(2)}{tau ? ` (alert threshold ${tau.toFixed(2)})` : ''} · EMI {inr(f.emi_amount)} · loan {inr(f.loan_amount)}</span>
      </div>

      <div className="detail-grid">
        <div>
          <div className="panel">
            <h3>Risk over time</h3>
            <TimelineChart risk={timeline.risk} events={timeline.events} flags={timeline.flags} tau={tau} />
          </div>

          <div className="panel next-action">
            <h3>Nudge to the customer</h3>
            {!nudge && <p className="muted">No nudge was drafted for this flag.</p>}
            {nudge && (
              <>
                <div className="row-flex">
                  <span className="chip">{nudgeTypeText(nudge.nudge_type)}</span> <NudgeStatus status={nudge.status} />
                  <span className="muted small">template {nudge.template_id}{g?.language ? ` · ${g.language}` : ''}</span>
                </div>
                <blockquote className="msg">{nudge.message || '(no message: blocked by the gate)'}</blockquote>
                {g && (
                  <details className="gate">
                    <summary>Compliance gate: {passedN} of {g.checks.length} checks passed{g.send_at ? ` · scheduled ${fmtDate(g.send_at)}, ${new Date(g.send_at).getHours()}:00` : ''}</summary>
                    <GateChecks checks={g.checks} />
                    {g.attempts?.length > 1 && <p className="muted small">The agent tried {g.attempts.length} wordings before one passed.</p>}
                  </details>
                )}
                {actionError && <div className="alert-error">{actionError}</div>}
                {nudge.status === 'pending_approval' && (
                  <div className="row-flex">
                    <button className="btn btn-search btn-small" disabled={busy} onClick={() => act('approve')}>Approve and send</button>
                    <button className="btn btn-reset btn-small" disabled={busy} onClick={() => act('skip')}>Skip</button>
                    <span className="muted">Sending goes to a test number only (mock). A message the gate has not passed can never be sent.</span>
                  </div>
                )}
                {nudge.status === 'sent' && <p className="muted">Sent on {fmtDate(nudge.sent_at)} · reference {nudge.dispatch_ref} (mock, test number).</p>}
              </>
            )}
          </div>
        </div>

        <div>
          <div className="panel">
            <h3>Why flagged</h3>
            <div className="row-flex"><SignalBadge signal={f.dominant_signal} /></div>
            <p className="reason">Main reason: <b>{signalText(f.dominant_signal)}</b>.</p>
            {drops.length > 0 ? (
              <>
                {drops.map(([k, v]) => (
                  <div key={k} className="sigbar">
                    <span>{signalText(k)}</span>
                    <div className="conf-bar"><div className="conf-fill risk" style={{ width: `${(v / maxDrop) * 100}%` }} /></div>
                  </div>
                ))}
                <p className="muted small">How much each signal pushed the risk score up. The bureau score is left out: it only summarises the other signals, so it is never given as the reason.</p>
              </>
            ) : <p className="muted">No signal breakdown stored for this flag.</p>}
            {refresh && (
              <div className="why">
                <b>Double-checked.</b> {REFRESH_TEXT[refresh.status] || `Fresh data was pulled (${refresh.status}).`}{' '}
                Pulled {TOOL_TEXT[refresh.tools] || refresh.tools}; the score before was {Number(refresh.score_before).toFixed(2)}.
              </div>
            )}
          </div>

          <div className="panel">
            <h3>What happened next</h3>
            <div className="row-flex"><OutcomeText went={outcome?.went_dpd1} lead={outcome?.lead_time_days} /></div>
            <p className="muted small">
              {outcome?.went_dpd1 == null
                ? 'Not known yet. We record the result once 60 days (plus the grace period) have passed or an EMI is missed, never earlier.'
                : outcome.went_dpd1 === 1
                  ? `The first missed EMI was on ${fmtDate(outcome.dpd1_date)}, so the flag gave ${outcome.lead_time_days} days of warning.`
                  : 'No EMI was missed in the 60 days after the flag.'}
            </p>
          </div>

          <div className="panel">
            <h3>Account</h3>
            <Fact k="Account" v={f.account_id} />
            <Fact k="Product" v={f.product} />
            <Fact k="Branch" v={f.branch} />
            <Fact k="EMI" v={inr(f.emi_amount)} />
            <Fact k="Loan amount" v={inr(f.loan_amount)} />
            <Fact k="Alert threshold version" v={f.threshold_version || '–'} />
            <Fact k="Other flags on this account" v={timeline.flags.length - 1} />
          </div>
        </div>
      </div>
    </>
  )
}
