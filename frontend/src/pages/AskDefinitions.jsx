import { useMemo, useState } from 'react'
import { getAskUser } from '../api.js'
import { useFetch } from '../hooks.js'
import PageHeader from '../components/PageHeader.jsx'

export default function AskDefinitions() {
  const { data, error, loading } = useFetch(`/lc01/metrics?u=${getAskUser()}`)
  const [fam, setFam] = useState('')
  const [q, setQ] = useState('')
  const [onlyOverlap, setOnlyOverlap] = useState(false)
  const families = useMemo(() => [...new Set((data || []).map((m) => m.family))], [data])
  const names = useMemo(() => Object.fromEntries((data || []).map((m) => [m.metric_id, m.name])), [data])
  const groups = useMemo(() => {
    const g = {}
    ;(data || []).forEach((m) => { if (m.overlap) (g[m.overlap] = g[m.overlap] || []).push(m) })
    return g
  }, [data])
  const rows = (data || []).filter((m) => (!fam || m.family === fam) && (!onlyOverlap || m.overlap) && (!q || (m.metric_id + m.name + m.definition).toLowerCase().includes(q.toLowerCase())))
  return (
    <>
      <PageHeader title="Metric definitions" subtitle="The only definitions the agent may use"
        info="Every number in an answer is one of these metrics, computed by one SELECT over one table. Some everyday words fit several definitions on purpose; the agent asks which one you mean instead of guessing." />
      {error && <div className="alert-error">{error}</div>}
      <div className="panel">
        <h3>Overlapping definitions (the agent asks before answering)</h3>
        {Object.entries(groups).map(([g, ms]) => (
          <div key={g} className="ask-group"><span className="seg-chip">{g.replace('_', ' ')}</span>{ms.map((m) => m.name).join(' · ')}</div>
        ))}
      </div>
      <div className="filters">
        <input className="field" placeholder="Search metrics" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className={'tab' + (!fam ? ' active' : '')} onClick={() => setFam('')}>All ({data?.length || 0})</button>
        {families.map((f) => <button key={f} className={'tab' + (fam === f ? ' active' : '')} onClick={() => setFam(f)}>{f}</button>)}
        <label><input type="checkbox" checked={onlyOverlap} onChange={(e) => setOnlyOverlap(e.target.checked)} /> overlapping only</label>
      </div>
      {loading ? <div className="empty">Loading…</div> : (
        <div className="table-wrap">
          <table className="grid">
            <thead><tr><th>Metric</th><th>Definition</th><th>Unit</th><th>Can be split by</th><th>Table</th></tr></thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.metric_id}>
                  <td><b>{m.name}</b>{m.overlap && <span className="cbadge ai" style={{ marginLeft: 6 }}>{m.overlap.replace('_', ' ')}</span>}<div className="sub">{m.metric_id}</div></td>
                  <td>{m.definition}<div className="sub"><code>{m.formula}</code></div>
                    {m.overlaps_with.length > 0 && <div className="sub">Overlaps with: {m.overlaps_with.map((x) => names[x]).join(', ')}</div>}</td>
                  <td>{m.unit}</td>
                  <td>{m.dims.join(', ')}</td>
                  <td>{m.table}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
