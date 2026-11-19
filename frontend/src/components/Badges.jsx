import { CHANNELS, LABELS } from '../utils.js'

export function LabelBadge({ label }) {
  if (!label) return <span className="badge b-none">Not assessed</span>
  return <span className={`badge b-${label}`}>{LABELS[label]}</span>
}

export function Confidence({ value }) {
  if (value == null) return <span className="b-none">–</span>
  const pct = Math.round(value * 100)
  const tone = value >= 0.8 ? 'hi' : value >= 0.55 ? 'mid' : 'lo'
  return (
    <div className="conf" title={`${pct}% confident`}>
      <div className="conf-bar"><div className={`conf-fill ${tone}`} style={{ width: `${pct}%` }} /></div>
      <span>{pct}%</span>
    </div>
  )
}

export function ChannelChip({ channel }) {
  if (!channel) return <span className="b-none">–</span>
  return <span className={`chip chip-${channel}`}>{CHANNELS[channel] || channel}</span>
}

export function StatusChip({ status }) {
  const map = { pending: ['b-warn', 'Waiting'], approved: ['b-ok', 'Approved'], skipped: ['b-insufficient', 'Skipped'] }
  const [cls, text] = map[status] || ['b-none', status]
  return <span className={`badge ${cls}`}>{text}</span>
}
