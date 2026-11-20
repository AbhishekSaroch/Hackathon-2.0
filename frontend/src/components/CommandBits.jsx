// Small building blocks for the LC-04 command center (plain SVG, no chart library).
import { Link } from 'react-router-dom'

export const SEG_LABEL = { lender: 'lender', agency: 'agency', dialer: 'dialer', gateway: 'gateway', dpd_bucket: 'DPD' }
export const segmentText = (seg = {}) =>
  ['lender', 'city', 'agency', 'dialer', 'gateway', 'product', 'dpd_bucket'].filter((k) => k in seg)
    .map((k) => (SEG_LABEL[k] ? `${SEG_LABEL[k]} ${seg[k]}` : seg[k])).join(' / ')

export function fmtVal(v, unit) {
  if (v == null || Number.isNaN(v)) return '–'
  if (unit === 'pct') return `${v.toFixed(1)}%`
  if (unit === 'rs') return '₹' + Math.round(v).toLocaleString('en-IN')
  if (unit === 'ratio') return v.toFixed(2)
  return Math.round(v).toLocaleString('en-IN')
}
export const fmtClock = (s) => (s ? new Date(s.replace(' ', 'T')).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '–')
export const hhmm = (s) => (s ? s.slice(11, 16) : '')

export function SegChips({ seg }) {
  return <span>{Object.entries(seg || {}).map(([k, v]) => <span className="seg-chip" key={k}><small>{SEG_LABEL[k] || k}</small> {v}</span>)}</span>
}

const COLORS = ['#034e63', '#e0a100', '#2e9e47', '#8b5cf6', '#e5484d', '#0088d1']

export function LineChart({ series = [], markers = [], unit }) {
  const pts = series.flatMap((s) => s.points.filter((p) => p[1] != null))
  if (!pts.length) return <div className="empty">No data in this window yet.</div>
  const W = 560, H = 190, L = 66, R = 10, T = 12, B = 24
  const tt = (t) => new Date(t.replace(' ', 'T')).getTime()
  const xs = pts.map((p) => tt(p[0]))
  const x0 = Math.min(...xs), x1 = Math.max(...xs, x0 + 1)
  const yMax = Math.max(...pts.map((p) => p[1]), 1e-9) * 1.1
  const X = (t) => L + ((tt(t) - x0) / (x1 - x0)) * (W - L - R)
  const Y = (v) => T + (1 - v / yMax) * (H - T - B)
  const mk = markers.filter((m) => { const t = tt(m.at); return t >= x0 && t <= x1 })
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="tl-chart" role="img">
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line x1={L} x2={W - R} y1={Y(yMax * f)} y2={Y(yMax * f)} stroke="#e3e3e3" />
          <text x={L - 5} y={Y(yMax * f) + 4} textAnchor="end" fontSize="10" fill="#8a8a8a">{fmtVal(yMax * f / 1.1 * 1.1, unit)}</text>
        </g>
      ))}
      {mk.map((m) => (
        <g key={m.incident_id}>
          <line x1={X(m.at)} x2={X(m.at)} y1={T} y2={H - B} stroke="#e5484d" strokeDasharray="4 3" />
          <polygon points={`${X(m.at) - 5},${T} ${X(m.at) + 5},${T} ${X(m.at)},${T + 8}`} fill="#e5484d"><title>{`${m.incident_id}: ${m.line1}`}</title></polygon>
        </g>
      ))}
      {series.map((s, i) => (
        <path key={s.name} fill="none" stroke={COLORS[i % COLORS.length]} strokeWidth="1.8" strokeLinejoin="round"
          d={s.points.filter((p) => p[1] != null).map((p, j) => `${j ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' ')} />
      ))}
      {[x0, (x0 + x1) / 2, x1].map((t, i) => (
        <text key={i} x={L + i * (W - L - R) / 2} y={H - 6} textAnchor={i === 0 ? 'start' : i === 2 ? 'end' : 'middle'} fontSize="10" fill="#8a8a8a">
          {new Date(t).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}
        </text>
      ))}
      {series.length > 1 && series.slice(0, 6).map((s, i) => (
        <text key={s.name} x={L + 6 + i * 84} y={T + 4} fontSize="10" fill={COLORS[i % COLORS.length]}>{s.name}</text>
      ))}
    </svg>
  )
}

export function BarList({ rows = [], unit, good }) {
  if (!rows.length) return <div className="empty">No data in this window yet.</div>
  const vals = rows.map((r) => Math.abs(r.value ?? 0))
  const max = Math.max(...vals, 1e-9)
  return (
    <div className="bar-list">
      {rows.map((r, i) => (
        <div className="bar-row" key={i}>
          <span className="bar-name">{segmentText(r.segment) || '–'}</span>
          <span className="bar-track"><span className="bar-fill" style={{ width: `${Math.abs(r.value ?? 0) / max * 100}%` }} /></span>
          <span className="bar-val">{fmtVal(r.value, unit)}</span>
        </div>
      ))}
    </div>
  )
}

export function Panel({ p, markers }) {
  if (p.error) return <div className="cmd-card"><div className="cmd-title">{p.title}</div><div className="alert-error">{p.error}</div></div>
  const chg = p.change_pct
  const better = chg == null || Math.abs(chg) < 1 ? null : (p.good_direction === 'down' ? chg < 0 : p.good_direction === 'up' ? chg > 0 : null)
  return (
    <div className={'cmd-card' + (p.type === 'kpi' ? ' kpi' : ' wide')}>
      <div className="cmd-title" title={p.name}>{p.title}</div>
      {p.empty && <div className="empty">Nothing yet at this time of day.</div>}
      {p.type === 'kpi' && !p.empty && (
        <>
          <div className="kpi-val">{fmtVal(p.value, p.unit)}</div>
          <div className={'kpi-chg' + (better === true ? ' good' : better === false ? ' bad' : '')}>
            {chg == null ? 'no comparison yet' : `${chg > 0 ? '▲' : '▼'} ${Math.abs(chg).toFixed(1)}% vs ${p.baseline_label}`}
          </div>
        </>
      )}
      {p.type === 'timeseries' && !p.empty && <LineChart series={p.series} markers={p.annotate ? markers : []} unit={p.unit} />}
      {(p.type === 'breakdown' || p.type === 'table') && !p.empty && <BarList rows={p.rows} unit={p.unit} good={p.good_direction} />}
    </div>
  )
}

export const IncidentLink = ({ id, children }) => <Link to={`/command/incidents/${id}`}>{children || id}</Link>
