import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'

const s = (n, w) => `${n.toLocaleString('en-IN')} ${w}${n === 1 ? '' : 's'}`

// Try new data: download the template -> load it into the database -> run the agent on those accounts. Nothing else on screen.
export default function DemoDataPanel({ onChanged, replayDay }) {
  const [info, setInfo] = useState(null)
  const [chosen, setChosen] = useState({})
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState(null)

  const refresh = useCallback(() => api('/bi03/demo').then(setInfo).catch((e) => setMsg({ ok: false, text: e.message })), [])
  useEffect(() => { setMsg(null) }, [replayDay])
  useEffect(() => { refresh() }, [refresh, replayDay])   // the flags of these accounts move with the date

  // the three files, matched by file name (bp_accounts.csv, events.csv, bureau_snapshots.csv)
  async function pick(e) {
    const got = {}
    const odd = []
    for (const f of [...e.target.files]) {
      const n = f.name.toLowerCase()
      const key = n.includes('account') ? 'accounts' : n.includes('event') ? 'events' : (n.includes('snapshot') || n.includes('bureau')) ? 'snapshots' : null
      if (key) got[key] = { name: f.name, text: await f.text() }
      else odd.push(f.name)
    }
    setChosen(got)
    setMsg(odd.length ? { ok: false, text: `Not recognised: ${odd.join(', ')}. Choose the three CSV files from the template.` } : null)
  }
  const haveAll = !!(chosen.accounts && chosen.events && chosen.snapshots)

  async function run(path, working, done, body) {
    setBusy(working)
    setMsg(null)
    try {
      const r = await api(path, { method: 'POST', body })
      setInfo((old) => ({ ...old, ...r }))
      setMsg({ ok: true, text: done(r), warnings: r.warnings })
      onChanged()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy('')
  }

  const loadedText = (r) => `Loaded ${s(r.rows_loaded.bp_accounts, 'account')}, ${s(r.rows_loaded.events, 'event')} and ${s(r.rows_loaded.bureau_snapshots, 'bureau snapshot')}.`
  // loading while accounts are already loaded replaces them (the old ones are removed first)
  const replaceFirst = async () => { if (info?.loaded) await api('/bi03/demo/remove', { method: 'POST' }) }
  const load = async () => {
    setBusy('Loading into the database…')
    try { await replaceFirst() } catch (e) { setMsg({ ok: false, text: e.message }); setBusy(''); return }
    run('/bi03/demo/load', 'Loading into the database…', loadedText, { accounts: chosen.accounts.text, events: chosen.events.text, snapshots: chosen.snapshots.text })
  }
  const loadSample = async () => {
    setBusy('Loading into the database…')
    try { await replaceFirst() } catch (e) { setMsg({ ok: false, text: e.message }); setBusy(''); return }
    run('/bi03/demo/load', 'Loading into the database…', loadedText)
  }
  const analyze = () => run('/bi03/demo/analyze', 'The agent is analysing the new accounts…', (r) => {
    const n = r.flagged_total
    return `Done: ${n} of ${r.accounts_total} account${r.accounts_total === 1 ? '' : 's'} flagged so far.${n ? '' : ' Advance the clock above to watch the flags appear.'}`
  })
  const remove = () => {
    if (!window.confirm('Remove the loaded accounts and everything the agent produced for them?')) return
    setChosen({})
    run('/bi03/demo/remove', 'Removing…', () => 'Removed.')
  }

  if (!info) return null
  const loaded = !!info.loaded
  const d = info.database?.demo
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Try new data</h3>
      <div className="row-flex">
        <a className="btn btn-reset btn-small" href="/bi03/demo/template" download>1. Download template</a>
        <input type="file" multiple accept=".csv" onChange={pick} disabled={!!busy} aria-label="Choose the CSV files" />
        <button className="btn btn-search btn-small" disabled={!!busy || !haveAll} onClick={load}>2. Load into database</button>
        <button className="btn btn-search btn-small" disabled={!!busy || !loaded} onClick={analyze}>3. Run analysis</button>
        {loaded && <button className="btn btn-reset btn-small" disabled={!!busy} onClick={remove}>Remove</button>}
      </div>
      <div className="muted small">
        Unzip the template and choose its three CSV files. No files at hand?{' '}
        <button className="link-btn" disabled={!!busy} onClick={loadSample}>Load the sample directly</button>
        {' · '}<a href="/bi03/demo/bulk-sample?n=200" download>Bulk sample (200 accounts)</a>
      </div>

      {busy && <div className="progress">{busy}</div>}
      {msg && <div className={msg.ok ? 'progress ok' : 'alert-error'}>{msg.text}</div>}
      {msg?.warnings?.map((w) => <div key={w} className="muted small">Note: {w}</div>)}

      {loaded && d && (
        <div className="muted small" style={{ marginTop: 6 }}>In the database: {s(d.accounts, 'account')} · {s(d.events, 'event')} · {s(d.snapshots, 'bureau snapshot')}. Flagged accounts show up in the list below.</div>
      )}
    </div>
  )
}
