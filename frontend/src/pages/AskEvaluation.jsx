import { useState } from 'react'
import { getAskUser } from '../api.js'
import { useFetch } from '../hooks.js'
import PageHeader from '../components/PageHeader.jsx'

const pct = (x) => `${Math.round(x * 100)}%`

function Block({ title, res, note }) {
  const [open, setOpen] = useState(false)
  if (!res) return null
  return (
    <div className="panel">
      <h3>{title}</h3>
      <div className="cards">
        <div className="card"><div className="big">{res.correct}/{res.n}</div><div className="lbl">correct ({pct(res.accuracy)})</div></div>
        <div className="card"><div className="big">{res.clarified_ambiguous}</div><div className="lbl">ambiguous questions that got a clarifying question</div></div>
        <div className="card"><div className="big">{res.cross_tenant_leaks}</div><div className="lbl">cross-lender leaks</div></div>
        <div className="card"><div className="big">{res.median_seconds}s</div><div className="lbl">median time to answer (max {res.max_seconds}s)</div></div>
      </div>
      <div className="row-flex">{Object.entries(res.by_kind).map(([k, v]) => <span key={k} className="seg-chip">{k.replace('_', ' ')} {v.correct}/{v.n}</span>)}</div>
      {note && <p className="sub">{note}</p>}
      <button className="link-btn" onClick={() => setOpen(!open)}>{open ? 'Hide' : 'Show'} every question</button>
      {open && (
        <div className="table-wrap" style={{ marginTop: 8 }}>
          <table className="grid"><thead><tr><th>#</th><th>Question</th><th>Kind</th><th>Result</th><th>What happened</th></tr></thead>
            <tbody>{res.questions.map((q) => (
              <tr key={q.id}><td>{q.id}</td><td>{q.text}</td><td>{q.kind}</td>
                <td><span className={'badge ' + (q.correct ? 'b-ok' : 'b-wont_pay')}>{q.correct ? 'correct' : 'wrong'}</span></td>
                <td className="sub">{q.why}</td></tr>))}</tbody></table>
        </div>
      )}
    </div>
  )
}

export default function AskEvaluation() {
  const { data, error, loading } = useFetch(`/lc01/evaluation?u=${getAskUser()}`)
  return (
    <>
      <PageHeader title="Ask evaluation" subtitle="Scored against answer keys computed by a separate implementation"
        info="Correct means the number matches an independent pandas calculation, the root cause names the planted driver, an ambiguous question gets a clarifying question, and a forbidden request is refused with the right reason." />
      {error && <div className="alert-error">{error}</div>}
      {loading ? <div className="empty">Loading…</div> : (
        <>
          <Block title="Public questions (we tuned on these): language model" res={data?.public?.llm} note="Not a clean number: the prompt and rules were tuned while looking at these." />
          <Block title="Public questions: rule-based parser only (no AI)" res={data?.public?.rules} note="The deterministic fallback used when the model is unavailable." />
          {data?.hidden ? (
            <>
              <Block title="Hidden questions (run once on frozen code): language model" res={data.hidden.official} note={data.hidden.note} />
              <Block title="Hidden questions: rule-based parser only (free baseline)" res={data.hidden.rules_baseline} />
            </>
          ) : <div className="note">The hidden set has not been run yet.</div>}
          <div className="panel">
            <h3>Honest notes</h3>
            <ul className="honest">
              <li>The warehouse, the questions and the answer keys are all ours, and the planted problems (a weak agency, a bounce spike, a creeping city) were written by us. The numbers show the method, not real-world accuracy.</li>
              <li>The hidden questions were written after the public tuning and run once, but by the same team that built the agent, so this is self-graded.</li>
              <li>Trimmed warehouse: 11 tables and 38 governed metrics instead of about 40 tables and 60 metrics.</li>
              <li>The model never writes SQL or any sentence of the answer. It only picks a metric, dimensions and months; code does the rest and enforces the overlap and refusal rules.</li>
            </ul>
          </div>
        </>
      )}
    </>
  )
}
