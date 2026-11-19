import { NUDGE_TYPES, signalText } from '../utils.js'

export function SignalBadge({ signal }) {
  return <span className={`badge sig sig-${signal || 'none'}`}>{signalText(signal)}</span>
}

const FLAG = {
  open: ['b-warn', 'New'],
  nudged: ['b-ok', 'Nudge drafted'],
  closed: ['b-insufficient', 'Closed (became late)'],
}
export function FlagStatus({ status }) {
  const [cls, text] = FLAG[status] || ['b-none', status]
  return <span className={`badge ${cls}`}>{text}</span>
}

const NUDGE = {
  pending_approval: ['b-warn', 'Waiting for approval'],
  sent: ['b-ok', 'Sent (test number)'],
  skipped: ['b-insufficient', 'Skipped'],
  blocked: ['b-wont_pay', 'Blocked by the gate'],
}
export function NudgeStatus({ status }) {
  if (!status) return <span className="b-none">No nudge</span>
  const [cls, text] = NUDGE[status] || ['b-none', status]
  return <span className={`badge ${cls}`}>{text}</span>
}

export const nudgeTypeText = (t) => NUDGE_TYPES[t] || t || '–'

// risk score as a small bar; the tick marks the alert threshold
export function RiskBar({ score, tau }) {
  if (score == null) return <span className="b-none">–</span>
  const w = Math.min(100, Math.round(score * 100))
  return (
    <div className="conf" title={`Risk score ${score.toFixed(2)}${tau ? ` (alert threshold ${tau.toFixed(2)})` : ''}`}>
      <div className="conf-bar risk-bar">
        <div className="conf-fill risk" style={{ width: `${w}%` }} />
        {tau != null && <i className="tau-tick" style={{ left: `${Math.min(100, tau * 100)}%` }} />}
      </div>
      <span>{score.toFixed(2)}</span>
    </div>
  )
}

export function OutcomeText({ went, lead }) {
  if (went === 1) return <span className="badge b-wont_pay">Missed an EMI{lead != null ? ` ${lead} days later` : ''}</span>
  if (went === 0) return <span className="badge b-ok">No missed EMI</span>
  return <span className="b-none">Not known yet</span>
}

// compliance-gate checks, one line each
export function GateChecks({ checks }) {
  if (!checks?.length) return null
  return (
    <ul className="gate-list">
      {checks.map((c) => (
        <li key={c.check} className={c.passed ? 'ok' : 'bad'}>
          <span className="mark">{c.passed ? '✓' : '✗'}</span>
          <b>{c.check.replace(/_/g, ' ')}</b>
          <span className="muted">{c.detail}</span>
        </li>
      ))}
    </ul>
  )
}
