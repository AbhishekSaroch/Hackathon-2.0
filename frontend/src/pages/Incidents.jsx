import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { fmtClock } from '../components/CommandBits.jsx'
import PageHeader from '../components/PageHeader.jsx'

export default function Incidents() {
  const nav = useNavigate()
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')
  const [user, setUser] = useState(0)
  const load = () => api('/lc04/incidents').then((r) => { setRows(r); setError('') }).catch((e) => setError(e.message))
  useEffect(() => {
    load()
    const h = () => load()
    window.addEventListener('lc04-clock-changed', h)
    window.addEventListener('lc04-user-changed', h)
    return () => { window.removeEventListener('lc04-clock-changed', h); window.removeEventListener('lc04-user-changed', h) }
  }, [])

  return (
    <>
      <PageHeader title="Incidents" subtitle="What the agent found, one row per root cause"
        info="Alerts that cover the same part of the book and fire together are one incident. Incidents the drill-down could not confirm stay 'watched': no alert is sent for them." />
      {error && <div className="alert-error">{error}</div>}
      {!rows ? <div className="empty">Loading…</div> : rows.length === 0 ? (
        <div className="empty">Nothing has been found yet at this simulated time. Advance the clock on the Command center page.</div>
      ) : (
        <div className="table-wrap">
          <table className="grid">
            <thead><tr><th>Incident</th><th>First alert / detection</th><th>What happened</th><th>Status</th></tr></thead>
            <tbody>
              {rows.map((i) => (
                <tr key={i.incident_id} className="click" onClick={() => nav(`/command/incidents/${i.incident_id}`)}>
                  <td><b>{i.incident_id}</b></td>
                  <td>{fmtClock(i.first_alert_at || i.detected_at)}</td>
                  <td>{i.line1 ? <>{i.line1}<div className="sub">{i.line2}</div></> : <span className="muted">Detected, not confirmed by the drill-down: watched, no alert sent</span>}</td>
                  <td><span className={'cbadge ' + (i.alerted ? 'bad' : 'fb')}>{i.alerted ? 'Alerted' : 'Watched'}</span> {i.status && <small className="muted">{i.status}</small>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
