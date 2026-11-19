import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, notifyRecsChanged } from '../api.js'
import { ChannelChip, Confidence, LabelBadge } from '../components/Badges.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { useFetch } from '../hooks.js'

const TABS = [['pending', 'Waiting for you'], ['approved', 'Approved'], ['skipped', 'Skipped']]

export default function Approve() {
  const [tab, setTab] = useState('pending')
  const { data, error, loading, reload } = useFetch(`/bi02/recommendations?status=${tab}`)
  const [actionError, setActionError] = useState('')

  async function decide(id, status) {
    setActionError('')
    try {
      await api(`/bi02/recommendations/${id}/status`, { method: 'POST', body: { status } })
      await reload(); notifyRecsChanged()
    } catch (e) { setActionError(e.message) }
  }

  return (
    <>
      <PageHeader title="To approve" subtitle="Recommended next actions" info="The agent recommends. A person approves. Nothing is sent to customers." />
      <div className="note">
        <b>Nothing is sent until a person approves, and approving here only records the decision.</b> Any message to a
        customer would also have to pass the compliance check first. In this demo, sending is not connected.
      </div>

      <div className="tabs">
        {TABS.map(([k, v]) => <button key={k} className={'tab' + (tab === k ? ' active' : '')} onClick={() => setTab(k)}>{v}</button>)}
      </div>

      {(error || actionError) && <div className="alert-error">{error || actionError}</div>}
      {loading && !data && <div className="empty">Loading…</div>}
      {data && !data.length && <div className="empty">Nothing here.</div>}

      <div className="rec-list">
        {data?.map((r) => (
          <div key={r.id} className="panel rec">
            <div className="row-flex">
              <ChannelChip channel={r.channel} />
              <b><Link to={`/accounts/${r.account_id}`}>{r.customer_name}</Link></b>
              <span className="muted">{r.account_id}</span>
              <span className="grow" />
              <LabelBadge label={r.label} /> <Confidence value={r.confidence} />
            </div>
            <p className="reason">{r.action}</p>
            <p className="muted">{r.reason}</p>
            {r.reasoning_summary && <p className="why">{r.reasoning_summary}</p>}
            {tab === 'pending' && (
              <div className="row-flex">
                <button className="btn btn-search btn-small" onClick={() => decide(r.id, 'approved')}>Approve</button>
                <button className="btn btn-reset btn-small" onClick={() => decide(r.id, 'skipped')}>Skip</button>
              </div>
            )}
          </div>
        ))}
      </div>
    </>
  )
}
