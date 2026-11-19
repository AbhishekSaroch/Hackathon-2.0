import { useState } from 'react'
import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import { useFetch } from '../hooks.js'
import { fmtDate } from '../utils.js'

const pct = (v, d = 0) => (v == null ? '–' : `${(v * 100).toFixed(d)}%`)
const x1 = (v) => (v == null ? '–' : `${v.toFixed(1)}x`)
const days = (v) => (v == null ? '–' : `${Math.round(v)} d`)
const TARGET_LIFT = 3, TARGET_LEAD = 21

function Threshold() {
  const { data: th, reload } = useFetch('/bi03/thresholds')
  const { data: sum, reload: reloadSum } = useFetch('/bi03/summary')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState(null)
  const [tuned, setTuned] = useState(null)
  const rp = sum?.replay
  const day = rp ? Math.min(rp.replay_day, rp.public_end_day) : undefined   // never look past the simulated date or the public period

  async function run(label, fn) {
    setBusy(label); setMsg(null)
    try { await fn() } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy('')
  }
  const record = () => run('record', async () => {
    const r = await api('/bi03/outcomes/record', { method: 'POST', body: { as_of_day: day } })
    setMsg({ ok: true, text: `As of day ${r.as_of_day}: ${r.flags} flags, ${r.hit} missed an EMI, ${r.no_miss} did not, ${r.pending} not known yet. Precision is between ${pct(r.precision_range?.[0])} and ${pct(r.precision_range?.[1])} (the unknown ones can still go either way).` })
  })
  const tune = () => run('tune', async () => {
    const r = await api('/bi03/thresholds/tune', { method: 'POST', body: { cutoff_day: day } })
    setTuned(r); reload()
  })
  const activate = (v) => {
    if (!window.confirm(`Make ${v} the active alert threshold? From the next replayed day the agent flags with it.`)) return
    run('activate', async () => { await api(`/bi03/thresholds/${v}/activate`, { method: 'POST' }); setMsg({ ok: true, text: `${v} is now active.` }); reload(); reloadSum() })
  }

  return (
    <div className="panel">
      <h3>Alert threshold and learning from outcomes</h3>
      <p className="muted">The agent flags an account when its risk score crosses the threshold. Once we know what happened to past flags, we can check whether a different threshold would have worked better. A suggestion is saved as a candidate and changes nothing until a person activates it.</p>
      <div className="row-flex">
        <button className="btn btn-reset btn-small" disabled={!!busy} onClick={record}>Record outcomes so far</button>
        <button className="btn btn-search btn-small" disabled={!!busy} onClick={tune}>Suggest a better threshold</button>
        {busy && <span className="progress">Working…</span>}
        {rp && <span className="muted small">uses data up to day {day} only</span>}
      </div>
      {msg && <div className={msg.ok ? 'progress ok' : 'alert-error'}>{msg.text}</div>}

      {tuned && (
        <>
          <h3>What each threshold would have done (days {tuned.window[0]} to {tuned.window[1]})</h3>
          <table className="mini wide">
            <thead><tr><th>Threshold</th><th className="num">Flagged</th><th className="num">Precision</th><th className="num">Caught</th><th className="num">Median lead</th></tr></thead>
            <tbody>
              {tuned.table.map((r) => (
                <tr key={r.candidate} className={r.candidate === tuned.recommended.candidate ? 'rec-row' : ''}>
                  <td>{r.candidate}{r.candidate === tuned.recommended.candidate ? ' (suggested)' : ''}</td>
                  <td className="num">{r.flagged}</td><td className="num">{pct(r.precision)}</td>
                  <td className="num">{r.hits} of {r.missers}</td><td className="num">{days(r.median_lead_days)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">{tuned.reason}. The window is only {tuned.window_days} days, so precision is a lower bound and lead times are cut short.</p>
        </>
      )}

      {th && (
        <table className="mini wide">
          <thead><tr><th>Version</th><th className="num">Score cut-off</th><th className="num">Flags about</th><th></th></tr></thead>
          <tbody>
            {th.map((t) => (
              <tr key={t.threshold_version}>
                <td>{t.threshold_version} {t.is_active ? <span className="badge b-ok">Active</span> : <span className="badge b-insufficient">Candidate</span>}</td>
                <td className="num">{t.value.toFixed(3)}</td>
                <td className="num">{pct(t.top_pct, 1)} of accounts</td>
                <td className="num">{!t.is_active && <button className="btn btn-reset btn-small" disabled={!!busy} onClick={() => activate(t.threshold_version)}>Activate</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

export default function RiskEvaluation() {
  const { data: m, error, loading } = useFetch('/bi03/metrics')

  if (loading && !m) return <div className="empty">Loading…</div>
  if (error) return <><PageHeader title="Risk evaluation" /><div className="alert-error">{error}</div></>

  const h = m.backtest.hidden
  const mod = h?.model_result?.replay, rule = h?.rule_baseline?.replay, rnd = h?.random_baseline?.replay
  const ab = m.backtest.confirm_public_ab
  const looks = m.hidden_set_looks || []

  return (
    <>
      <PageHeader title="Risk evaluation" subtitle="How well does the early warning work" info="Measured on a held-out period the model never saw, with the same rules the daily agent follows" />
      <div className="note">
        <b>Read this first: the data is synthetic.</b> We generated it ourselves, and it is easier than real life, so these numbers show that the method works, not how accurate it would be on a real loan book.
        The held-out period was evaluated once (the log below), after a protocol fix made on public data only.
      </div>

      {mod ? (
        <>
          <div className="cards">
            <div className="card"><div className="big">{x1(mod.lift)}</div>
              <div className="lbl">Lift in the top 5% flagged <span className={`badge ${h.pass_lift ? 'b-ok' : 'b-warn'}`}>{h.pass_lift ? 'meets' : 'below'} {TARGET_LIFT}x</span></div>
              <div className="muted small">95% range {h.bootstrap.lift_ci95.map((v) => v.toFixed(1)).join(' to ')}x</div></div>
            <div className="card"><div className="big">{days(mod.median_lead_days)}</div>
              <div className="lbl">Median warning before a missed EMI <span className={`badge ${h.pass_lead ? 'b-ok' : 'b-warn'}`}>{h.pass_lead ? 'meets' : 'below'} {TARGET_LEAD} d</span></div>
              <div className="muted small">95% range {h.bootstrap.lead_ci95.join(' to ')} days</div></div>
            <div className="card"><div className="big">{pct(mod.precision)}</div><div className="lbl">Of flagged accounts really missed</div>
              <div className="muted small">base rate {pct(mod.base_rate, 1)}</div></div>
            <div className="card"><div className="big">{mod.caught} <small>/ {mod.missers}</small></div><div className="lbl">Missers caught early ({pct(mod.recall)})</div></div>
          </div>

          <div className="panel">
            <h3>Model vs simple alternatives (held-out period, same 5% flag budget)</h3>
            <table className="mini wide">
              <thead><tr><th>Method</th><th className="num">Flagged</th><th className="num">Caught</th><th className="num">Precision</th><th className="num">Lift</th><th className="num">Median lead</th></tr></thead>
              <tbody>
                {[['Risk model (what the agent uses)', mod, true], ['Hand-written rule, no learning', rule], ['Random flagging', rnd]].map(([name, r, bold]) => (
                  <tr key={name}><td>{bold ? <b>{name}</b> : name}</td><td className="num">{r.flagged}</td><td className="num">{r.caught} of {r.missers}</td>
                    <td className="num">{pct(r.precision)}</td><td className="num">{bold ? <b>{x1(r.lift)}</b> : x1(r.lift)}</td><td className="num">{days(r.median_lead_days)}</td></tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">Lift = how many times better than random at picking accounts that miss. About 12% of defaulters give no warning at all in this data, so no method can catch everyone.</p>
          </div>
        </>
      ) : <div className="panel">No evaluation report found. Run the backtest first.</div>}

      {ab?.with && (
        <div className="panel">
          <h3>Double-checking borderline accounts with fresh data</h3>
          <p className="muted">Accounts with a score close to the threshold get a fresh bureau report or bank statement before the decision. Tested on the public period only, so the held-out numbers above were measured <b>without</b> this step.</p>
          <table className="mini wide">
            <thead><tr><th></th><th className="num">Lift</th><th className="num">Caught</th><th className="num">Median lead</th></tr></thead>
            <tbody>
              <tr><td>Without the double-check</td><td className="num">{x1(ab.without.replay.lift)}</td><td className="num">{ab.without.replay.caught} of {ab.without.replay.missers}</td><td className="num">{days(ab.without.replay.median_lead_days)}</td></tr>
              <tr><td><b>With the double-check</b></td><td className="num"><b>{x1(ab.with.replay.lift)}</b></td><td className="num">{ab.with.replay.caught} of {ab.with.replay.missers}</td><td className="num">{days(ab.with.replay.median_lead_days)}</td></tr>
            </tbody>
          </table>
          <p className="muted small">
            The headline moves only a little because few accounts are borderline (about {Math.round(ab.avg_pulled_accounts_per_day)} per day). The decisions themselves were good:
            {' '}{ab.decisions.withdrawn} flags withdrawn as false alarms, {ab.decisions.promoted} accounts caught earlier than the plain score would have, {ab.decisions.confirmed} flags confirmed.
          </p>
        </div>
      )}

      <Threshold />

      <div className="panel">
        <h3>Outcomes recorded for the flags in this demo</h3>
        <p className="muted">
          {m.outcomes_recorded.length ? m.outcomes_recorded.map((o) => `${o.n} ${o.k === '1' ? 'missed an EMI' : o.k === '0' ? 'did not miss' : 'not known yet'}`).join(' · ') : 'No outcomes recorded yet. Use "Record outcomes so far" above.'}
        </p>
        <p className="muted small">"Not known yet" is never counted as a miss or as a success. We only know a flag was wrong after 60 days (plus the grace period) have passed.</p>
      </div>

      <div className="panel">
        <h3>How honest is this evaluation</h3>
        <ul className="honest">
          <li>Every number uses only information that was available on the day (a test checks this, and five deliberately planted leaks are all caught).</li>
          <li>The held-out period has been looked at {looks.length} time{looks.length === 1 ? '' : 's'}{looks.length ? ` (last: ${fmtDate(looks[looks.length - 1].at)}, model ${looks[looks.length - 1].model})` : ''}. Re-running it to tune would make it meaningless, so it is logged.</li>
          <li>Earlier, we loosened the data generator's difficulty after quick diagnostics on that period: the first data version gave only 0.7x lift. So treat the numbers as a method demo.</li>
          <li>{m.backtest.note}</li>
        </ul>
      </div>
    </>
  )
}
