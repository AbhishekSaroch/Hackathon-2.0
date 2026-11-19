import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, notifyRecsChanged } from '../api.js'
import { ChannelChip, Confidence, LabelBadge, StatusChip } from '../components/Badges.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { useFetch } from '../hooks.js'
import { fmtDate, inr, stepText } from '../utils.js'

const yn = (v) => (v === true ? 'Yes' : v === false ? 'No' : '–')
const num = (v, suffix = '') => (v == null ? '–' : `${v}${suffix}`)

function Fact({ k, v }) {
  return <div className="fact"><span>{k}</span><b>{v}</b></div>
}

export default function AccountDetail() {
  const { id } = useParams()
  const { data, error, loading, reload } = useFetch(`/bi02/accounts/${id}`)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')

  async function runAgent() {
    setBusy(true); setActionError('')
    try { await api(`/bi02/assess/${id}`, { method: 'POST' }); await reload(); notifyRecsChanged() }
    catch (e) { setActionError(e.message) }
    setBusy(false)
  }
  async function decide(status) {
    try {
      await api(`/bi02/recommendations/${data.recommendation.id}/status`, { method: 'POST', body: { status } })
      await reload(); notifyRecsChanged()
    } catch (e) { setActionError(e.message) }
  }

  if (loading && !data) return <div className="empty">Loading…</div>
  if (error) return <><PageHeader title="Account" /><div className="alert-error">{error}</div><Link to="/">Back to accounts</Link></>

  const { account: a, assessment: s, evidence, agent_steps: steps, recommendation: rec, facts, repayments, calls } = data
  const f = facts.statement

  return (
    <>
      <PageHeader title={`${a.customer_name}`} subtitle={`${a.account_id} · ${a.product} · ${a.branch}`} info="Why this customer is not paying, and what to do next" />
      <p><Link to="/">← Back to accounts</Link></p>

      <div className="toolbar">
        <span className="muted">{a.bucket} · {a.dpd} days late · EMI {inr(a.emi_amount)} · Outstanding {inr(a.outstanding)}</span>
        <span className="grow" />
        {busy && <span className="progress">Agent is working, this can take up to a minute…</span>}
        <button className="btn btn-search btn-small" disabled={busy} onClick={runAgent}>{s ? 'Re-run agent' : 'Run agent'}</button>
      </div>
      {actionError && <div className="alert-error">{actionError}</div>}

      {!s && <div className="panel">This account has not been assessed yet. Press <b>Run agent</b>.</div>}

      {s && (
        <div className="detail-grid">
          <div>
            <div className="panel">
              <div className="row-flex">
                <LabelBadge label={s.label} /> <Confidence value={s.confidence} />
                <span className="grow" /><span className="muted">As of {fmtDate(s.as_of_date)}</span>
              </div>
              <p className="reason">{s.reasoning_summary}</p>
            </div>

            {rec && (
              <div className="panel next-action">
                <h3>Recommended next action</h3>
                <div className="row-flex"><ChannelChip channel={rec.channel} /> <StatusChip status={rec.status} /></div>
                <p className="reason">{rec.action}</p>
                <p className="muted">{rec.reason}</p>
                {rec.status === 'pending' ? (
                  <div className="row-flex">
                    <button className="btn btn-search btn-small" onClick={() => decide('approved')}>Approve</button>
                    <button className="btn btn-reset btn-small" onClick={() => decide('skipped')}>Skip</button>
                    <span className="muted">Nothing is sent to the customer from here.</span>
                  </div>
                ) : <p className="muted">Decision recorded.</p>}
              </div>
            )}

            <div className="panel">
              <h3>Evidence the agent used</h3>
              {evidence.map((e, i) => (
                <div key={i} className="evidence">
                  <span className={`src src-${e.source}`}>{e.source}</span>
                  <div>{e.text}<div className="sub">ref: {e.source_ref}{e.supports ? ` · points to: ${e.supports}` : ''}</div></div>
                </div>
              ))}
              {!evidence.length && <span className="muted">No evidence recorded.</span>}
            </div>

            <div className="panel">
              <h3>What the agent did</h3>
              <ol className="timeline">
                {steps.map((st) => (
                  <li key={st.step_no} className={st.tool === 'request_fresh_statement' ? 'hl' : ''}>{stepText(st)}</li>
                ))}
              </ol>
            </div>

            <div className="panel">
              <h3>Customer calls</h3>
              {calls.map((c) => (
                <details key={c.call_id} className="call">
                  <summary>{fmtDate(c.call_date)} · {c.call_id}</summary>
                  <p className="transcript">{c.transcript}</p>
                </details>
              ))}
              {!calls.length && <span className="muted">No calls on record.</span>}
            </div>
          </div>

          <div>
            <div className="panel">
              <h3>Key numbers</h3>
              <Fact k="EMIs paid on time / late / missed" v={`${facts.repayment.n_paid_on_time} / ${facts.repayment.n_paid_late} / ${facts.repayment.n_missed}`} />
              <Fact k="Clean record before this miss" v={yn(facts.repayment.perfect_history_before_miss)} />
              <Fact k="Promises kept" v={`${facts.promises.n_kept} of ${facts.promises.n_promises}`} />
              {f.available ? (
                <>
                  <Fact k="Statement shown" v={`pull ${data.statement_pull_shown}${f.is_thin ? ' (thin)' : ''}`} />
                  <Fact k="Salary drop" v={num(f.salary_drop_pct, '%')} />
                  <Fact k="Months since last salary" v={num(f.months_since_last_salary)} />
                  <Fact k="Income variability" v={num(f.income_variability)} />
                  <Fact k="Balance on first missed due date" v={inr(f.balance_on_first_missed_due)} />
                  <Fact k="Current balance covers EMI" v={yn(f.current_balance_covers_emi)} />
                  <Fact k="Current balance" v={inr(f.current_balance)} />
                  <Fact k="Discretionary spend (90 days)" v={inr(f.discretionary_spend_90d)} />
                  <Fact k="Double debit found" v={f.double_debit?.length ? f.double_debit.map((d) => fmtDate(d.date)).join(', ') : 'No'} />
                  <Fact k="Reversal credits" v={f.reversal_credits?.length ?? 0} />
                </>
              ) : <Fact k="Bank statement" v="Not available" />}
            </div>

            <div className="panel">
              <h3>EMI history</h3>
              <table className="mini">
                <thead><tr><th>Due</th><th>Paid</th><th>Status</th></tr></thead>
                <tbody>
                  {repayments.map((r) => (
                    <tr key={r.repay_id}><td>{fmtDate(r.due_date)}</td><td>{fmtDate(r.paid_date)}</td>
                      <td><span className={`badge ${r.status === 'paid' ? 'b-ok' : r.status === 'late' ? 'b-warn' : 'b-wont_pay'}`}>{r.status}</span></td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
