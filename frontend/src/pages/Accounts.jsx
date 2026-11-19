import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, notifyRecsChanged } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import Pager from '../components/Pager.jsx'
import { ChannelChip, Confidence, LabelBadge, StatusChip } from '../components/Badges.jsx'
import { LABELS, inr } from '../utils.js'

const PAGE_SIZE = 10
const EMPTY = { label: '', q: '', sort: 'account' }
const SORTS = {
  account: 'Account no',
  dpd_desc: 'DPD (high to low)',
  outstanding_desc: 'Outstanding (high to low)',
  confidence_asc: 'Confidence (low to high)',
  confidence_desc: 'Confidence (high to low)',
}

export default function Accounts() {
  const nav = useNavigate()
  const [draft, setDraft] = useState(EMPTY)
  const [applied, setApplied] = useState(EMPTY)
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [job, setJob] = useState(null)

  const load = useCallback(async () => {
    const p = new URLSearchParams({ page, page_size: PAGE_SIZE, sort: applied.sort })
    if (applied.label) p.set('label', applied.label)
    if (applied.q) p.set('q', applied.q)
    try {
      setData(await api(`/bi02/accounts?${p}`))
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }, [page, applied])
  useEffect(() => { load() }, [load])

  // follow a running batch job
  useEffect(() => {
    if (!job || job.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const s = await api(`/bi02/batch/${job.job_id}`)
        setJob(s)
        if (s.status === 'done') { load(); notifyRecsChanged() }
      } catch { /* keep polling */ }
    }, 2000)
    return () => clearInterval(t)
  }, [job, load])

  async function runBatch(limit) {
    try {
      const j = await api('/bi02/batch', { method: 'POST', body: { only_unassessed: true, limit, workers: 3 } })
      if (!j.total) { setError('Every account is already assessed.'); return }
      setError('')
      setJob({ job_id: j.job_id, status: 'running', total: j.total, done: 0, failed: 0 })
    } catch (e) { setError(e.message) }
  }

  const search = () => { setPage(1); setApplied(draft) }
  const reset = () => { setDraft(EMPTY); setApplied(EMPTY); setPage(1) }
  const running = job?.status === 'running'

  return (
    <>
      <PageHeader title="Accounts" subtitle="Why late customers are not paying" info="One reason label per late account, with confidence and a recommended next step" />

      <div className="filters">
        <select className="select" value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} aria-label="Reason">
          <option value="">Reason: All</option>
          {Object.entries(LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          <option value="unassessed">Not assessed yet</option>
        </select>
        <input className="field" placeholder="Account No / Customer Name" value={draft.q}
          onChange={(e) => setDraft({ ...draft, q: e.target.value })} onKeyDown={(e) => e.key === 'Enter' && search()} />
        <select className="select" value={draft.sort} onChange={(e) => setDraft({ ...draft, sort: e.target.value })} aria-label="Sorting">
          {Object.entries(SORTS).map(([k, v]) => <option key={k} value={k}>Sort: {v}</option>)}
        </select>
        <span className="grow" />
        <button className="btn btn-search" onClick={search}>Search</button>
        <button className="btn btn-reset" onClick={reset}>Reset</button>
      </div>

      <div className="toolbar">
        <span className="muted">The AI agent reads calls and bank statements and labels each late account.</span>
        <span className="grow" />
        {running && <span className="progress">Agent running: {job.done + job.failed} / {job.total} accounts</span>}
        {job?.status === 'done' && <span className="progress ok">Done: {job.done} assessed{job.failed ? `, ${job.failed} failed` : ''}</span>}
        <button className="btn btn-reset btn-small" disabled={running} onClick={() => runBatch(20)}>Run agent on next 20</button>
        <button className="btn btn-search btn-small" disabled={running}
          onClick={() => window.confirm('Run the agent on ALL remaining accounts? This takes a while.') && runBatch(500)}>Run on all remaining</button>
      </div>

      {error && <div className="alert-error">{error}</div>}

      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>Account</th><th>Customer</th><th>Bucket / DPD</th><th className="num">Outstanding</th>
              <th className="num">EMI</th><th>Reason</th><th>Confidence</th><th>Next action</th>
            </tr>
          </thead>
          <tbody>
            {data?.items.map((a) => (
              <tr key={a.account_id} className="click" onClick={() => nav(`/accounts/${a.account_id}`)}>
                <td><a href={`/accounts/${a.account_id}`} onClick={(e) => e.preventDefault()}>{a.account_id}</a><div className="sub">{a.product}</div></td>
                <td>{a.customer_name}<div className="sub">{a.branch}</div></td>
                <td>{a.bucket}<div className="sub">{a.dpd} days</div></td>
                <td className="num">{inr(a.outstanding)}</td>
                <td className="num">{inr(a.emi_amount)}</td>
                <td><LabelBadge label={a.label} /></td>
                <td><Confidence value={a.confidence} /></td>
                <td><ChannelChip channel={a.channel} />{a.rec_status && a.rec_status !== 'pending' && <> <StatusChip status={a.rec_status} /></>}</td>
              </tr>
            ))}
            {data && !data.items.length && <tr><td colSpan="8" className="empty">No accounts match.</td></tr>}
            {!data && !error && <tr><td colSpan="8" className="empty">Loading…</td></tr>}
          </tbody>
        </table>
      </div>
      {data && <Pager page={page} total={data.total} pageSize={PAGE_SIZE} onPage={setPage} />}
    </>
  )
}
