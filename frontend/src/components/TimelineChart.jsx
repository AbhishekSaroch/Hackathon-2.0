// Risk score over time + what happened in the account (plain SVG, no chart library).
// Top: risk line, dashed alert threshold, red lines where a flag was raised.
// Lanes below: salary credits, EMI payments, bureau events. Everything is cut at the replay date by the API.
const W = 900, H = 330, L = 100, R = 16, T = 14
const PLOT_H = 200
const LANES = [
  { key: 'salary', label: 'Salary credited', y: 248, color: '#2e9e47' },
  { key: 'emi', label: 'EMI paid', y: 270, color: '#6b7280' },
  { key: 'bureau', label: 'Bureau event', y: 292, color: '#e0a100' },
]
const DAY = 86400000
const t = (d) => new Date(d).getTime()
const fmt = (ms) => new Date(ms).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })

export default function TimelineChart({ risk = [], events = [], flags = [], tau }) {
  if (!risk.length) return <div className="empty">No risk scores yet for this account.</div>

  const last = t(risk[risk.length - 1].date)
  const first = t(risk[0].date) - 45 * DAY               // a little history before scoring starts
  const x = (ms) => L + ((ms - first) / (last - first || 1)) * (W - L - R)
  const yMax = Math.max(0.5, Math.ceil(Math.max(...risk.map((r) => r.score), tau || 0) * 10) / 10)
  const y = (s) => T + PLOT_H - (s / yMax) * PLOT_H

  const line = risk.map((r, i) => `${i ? 'L' : 'M'}${x(t(r.date)).toFixed(1)},${y(r.score).toFixed(1)}`).join(' ')
  const laneOf = (e) =>
    e.source === 'statement' && /^salary/i.test(e.narration || '') ? 'salary'
    : e.source === 'repayment' ? 'emi'
    : e.source === 'bureau' ? 'bureau' : null
  const dots = events.map((e) => ({ ...e, lane: laneOf(e), ms: t(e.date) })).filter((e) => e.lane && e.ms >= first && e.ms <= last)
  const ticks = []
  for (let ms = first; ms <= last; ms += 30 * DAY) ticks.push(ms)
  const yTicks = [0, yMax / 2, yMax]

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="tl-chart" role="img" aria-label="Risk score over time with salary, EMI and bureau events">
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="#e3e3e3" />
            <text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="11" fill="#8a8a8a">{v.toFixed(2)}</text>
          </g>
        ))}
        {tau != null && tau <= yMax && (
          <g>
            <line x1={L} x2={W - R} y1={y(tau)} y2={y(tau)} stroke="#e5484d" strokeDasharray="5 4" />
            <text x={W - R} y={y(tau) - 5} textAnchor="end" fontSize="11" fill="#e5484d">Alert threshold {tau.toFixed(2)}</text>
          </g>
        )}
        {flags.map((f) => (
          <g key={f.flag_id}>
            <line x1={x(t(f.flagged_on))} x2={x(t(f.flagged_on))} y1={T} y2={T + PLOT_H} stroke="#e5484d" strokeWidth="1.5" />
            <polygon points={`${x(t(f.flagged_on)) - 5},${T} ${x(t(f.flagged_on)) + 5},${T} ${x(t(f.flagged_on))},${T + 9}`} fill="#e5484d">
              <title>{`Flag raised on ${fmt(t(f.flagged_on))}`}</title>
            </polygon>
          </g>
        ))}
        <path d={line} fill="none" stroke="#034e63" strokeWidth="2.2" strokeLinejoin="round" />
        {risk.length <= 40 && risk.map((r) => (
          <circle key={r.date} cx={x(t(r.date))} cy={y(r.score)} r="2.5" fill="#034e63"><title>{`${fmt(t(r.date))}: risk ${r.score.toFixed(2)}`}</title></circle>
        ))}

        {LANES.map((l) => (
          <g key={l.key}>
            <line x1={L} x2={W - R} y1={l.y} y2={l.y} stroke="#eee" />
            <text x={L - 6} y={l.y + 4} textAnchor="end" fontSize="10" fill="#8a8a8a">{l.label}</text>
            {dots.filter((d) => d.lane === l.key).map((d, i) => (
              <circle key={i} cx={x(d.ms)} cy={l.y} r="4" fill={l.color}>
                <title>{`${fmt(d.ms)}: ${d.narration || d.event_type}${d.amount ? ` (₹${Math.round(d.amount).toLocaleString('en-IN')})` : ''}`}</title>
              </circle>
            ))}
          </g>
        ))}

        {ticks.map((ms) => (
          <text key={ms} x={x(ms)} y={H - 6} textAnchor="middle" fontSize="11" fill="#8a8a8a">{fmt(ms)}</text>
        ))}
        <text x={12} y={T + PLOT_H / 2} fontSize="11" fill="#8a8a8a" transform={`rotate(-90 12 ${T + PLOT_H / 2})`} textAnchor="middle">Risk score</text>
      </svg>
      <p className="muted chart-note">
        Dark line = risk score each day. Red marks = days the account was flagged. Dots below show salary credits, EMI payments and bureau events;
        a late salary shows up as a gap between green dots. Hover a dot for details.
      </p>
    </div>
  )
}
