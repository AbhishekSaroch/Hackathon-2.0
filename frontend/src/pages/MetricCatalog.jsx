import { useMemo, useState } from 'react'
import { useFetch } from '../hooks.js'
import PageHeader from '../components/PageHeader.jsx'

export default function MetricCatalog() {
  const { data, error, loading } = useFetch('/lc04/metrics')
  const [fam, setFam] = useState('')
  const [q, setQ] = useState('')
  const families = useMemo(() => [...new Set((data || []).map((m) => m.family))], [data])
  const rows = (data || []).filter((m) => (!fam || m.family === fam) && (!q || (m.metric_id + m.name + m.description).toLowerCase().includes(q.toLowerCase())))
  return (
    <>
      <PageHeader title="Metric catalog" subtitle="The only definitions allowed on any view"
        info="Every chart, alert and number in the command center is computed from one of these governed metrics. The list is cut to what your role may see." />
      {error && <div className="alert-error">{error}</div>}
      <div className="filters">
        <input className="field" placeholder="Search metrics" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className={'tab' + (!fam ? ' active' : '')} onClick={() => setFam('')}>All ({data?.length || 0})</button>
        {families.map((f) => <button key={f} className={'tab' + (fam === f ? ' active' : '')} onClick={() => setFam(f)}>{f}</button>)}
      </div>
      {loading ? <div className="empty">Loading…</div> : (
        <div className="table-wrap">
          <table className="grid">
            <thead><tr><th>Metric</th><th>Definition</th><th>Unit</th><th>Can be sliced by</th><th>Owner</th></tr></thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.metric_id}>
                  <td><b>{m.name}</b>{m.is_key && <span className="cbadge ai" style={{ marginLeft: 6 }}>monitored</span>}<div className="sub">{m.metric_id}</div></td>
                  <td>{m.description}<div className="sub"><code>{m.formula}</code></div></td>
                  <td>{m.unit}</td>
                  <td>{m.allowed_dims.join(', ')}</td>
                  <td>{m.owner}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
