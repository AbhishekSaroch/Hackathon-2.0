// Chart for "Ask your portfolio" answers: horizontal bars (ranking) or lines (trend). Plain SVG / CSS, no chart library.
// The chart spec comes from the backend chart generator: { kind, title, unit, series: [{ name, points: [{ x, y }] }], highlight: [months] }.
export function fmtVal(v, unit) {
  if (v == null) return 'n/a'
  if (unit === 'pct') return (v * 100).toFixed(1) + '%'
  if (unit === 'rs') {
    const a = Math.abs(v)
    return a >= 1e7 ? `Rs ${(v / 1e7).toFixed(2)} crore` : a >= 1e5 ? `Rs ${(v / 1e5).toFixed(2)} lakh` : `Rs ${Math.round(v).toLocaleString('en-IN')}`
  }
  if (unit === 'days') return v.toFixed(1) + ' d'
  if (unit === 'sec') return Math.round(v) + ' s'
  return Math.round(v).toLocaleString('en-IN')
}

const COLORS = ['#034e63', '#e5484d', '#2e9e47', '#e0a100', '#7c4dff', '#0a6ca8']
const W = 720, H = 280, L = 62, R = 14, T = 14, B = 34

function Bars({ chart }) {
  const pts = chart.series[0]?.points || []
  const max = Math.max(...pts.map((p) => Math.abs(p.y || 0)), 1e-9)
  return (
    <div className="bar-list ask-bars">
      {pts.map((p) => (
        <div key={p.x} className="bar-row ask-bar-row">
          <span className="bar-name" title={p.x}>{p.x}</span>
          <span className="bar-track"><span className="bar-fill" style={{ width: `${(Math.abs(p.y || 0) / max) * 100}%` }} /></span>
          <span className="bar-val">{fmtVal(p.y, chart.unit)}</span>
        </div>
      ))}
    </div>
  )
}

function Lines({ chart }) {
  const series = chart.series.filter((s) => s.points.length)
  const months = [...new Set(series.flatMap((s) => s.points.map((p) => p.x)))].sort()
  const ys = series.flatMap((s) => s.points.map((p) => p.y).filter((v) => v != null))
  if (!months.length || !ys.length) return <div className="empty">Nothing to plot.</div>
  let lo = Math.min(...ys), hi = Math.max(...ys)
  if (hi === lo) { hi += Math.abs(hi) * 0.1 + 0.01; lo -= Math.abs(lo) * 0.1 }
  const pad = (hi - lo) * 0.12
  lo = chart.unit === 'pct' || chart.unit === 'count' ? Math.max(0, lo - pad) : lo - pad
  hi += pad
  const x = (m) => L + (months.indexOf(m) / Math.max(months.length - 1, 1)) * (W - L - R)
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B)
  const ticks = [lo, (lo + hi) / 2, hi]
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="tl-chart" role="img" aria-label={chart.title}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="#e3e3e3" />
            <text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="11" fill="#8a8a8a">{fmtVal(v, chart.unit)}</text>
          </g>
        ))}
        {(chart.highlight || []).filter((m) => months.includes(m)).map((m) => (
          <g key={m}><line x1={x(m)} x2={x(m)} y1={T} y2={H - B} stroke="#e5484d" strokeDasharray="4 3" /><text x={x(m)} y={T + 10} textAnchor="middle" fontSize="10" fill="#e5484d">compared</text></g>
        ))}
        {months.map((m, i) => (months.length <= 12 || i % 2 === 0) && <text key={m} x={x(m)} y={H - 12} textAnchor="middle" fontSize="10.5" fill="#8a8a8a">{m.slice(2)}</text>)}
        {series.map((s, si) => {
          const col = COLORS[si % COLORS.length]
          const d = s.points.filter((p) => p.y != null).map((p, i) => `${i ? 'L' : 'M'}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`).join(' ')
          return (
            <g key={s.name}>
              <path d={d} fill="none" stroke={col} strokeWidth="2.2" strokeLinejoin="round" />
              {s.points.filter((p) => p.y != null).map((p) => <circle key={p.x} cx={x(p.x)} cy={y(p.y)} r="3" fill={col}><title>{`${s.name} · ${p.x}: ${fmtVal(p.y, chart.unit)}`}</title></circle>)}
            </g>
          )
        })}
      </svg>
      {series.length > 1 && (
        <div className="ask-legend">{series.map((s, i) => <span key={s.name}><i style={{ background: COLORS[i % COLORS.length] }} />{s.name}</span>)}</div>
      )}
    </div>
  )
}

export default function AskChart({ chart }) {
  if (!chart) return null
  return (
    <div className="ask-chart">
      <div className="cmd-title">{chart.title}</div>
      {chart.kind === 'line' ? <Lines chart={chart} /> : <Bars chart={chart} />}
    </div>
  )
}
