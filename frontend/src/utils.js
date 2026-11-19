export const inr = (n) => (n == null ? '–' : '₹' + Math.round(n).toLocaleString('en-IN'))

export const fmtDate = (iso) =>
  iso ? new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) : '–'

export const LABELS = {
  forgot: 'Forgot',
  cant_pay: "Can't pay",
  wont_pay: "Won't pay",
  disputed: 'Disputed',
  insufficient: 'Not sure',
}
export const LABEL_KEYS = Object.keys(LABELS)

export const CHANNELS = {
  whatsapp: 'WhatsApp',
  telecall: 'Telecall',
  field_visit: 'Field visit',
  dispute_team: 'Dispute team',
  human: 'Human review',
}

// ---- BI-03 (early warning) ----
export const SIGNALS = {
  salary_delay: 'Salary arriving late',
  late_emi_payments: 'EMIs paid late',
  other_loan_bounce: 'Other loan bounced',
  new_enquiries: 'New credit enquiries',
  card_utilisation: 'Card use rising',
  bureau_score: 'Bureau score falling',
  low_balance: 'Low balance',
  emi_burden: 'EMI burden high',
}
export const NUDGE_TYPES = {
  emi_date_change: 'EMI date change offer',
  autopay_setup: 'Auto-pay setup',
  reminder: 'Friendly reminder',
}
export const signalText = (s) => (s ? SIGNALS[s] || s : 'No single signal')

// Friendly one-line description of each agent step (for the timeline)
export function stepText(s) {
  const o = s.output || {}
  const i = s.input || {}
  switch (s.tool) {
    case 'get_evidence_pack':
      return `Collected data: ${o.n_calls} calls, ${o.n_missed} missed EMIs${o.statement_thin ? ', bank statement is thin' : ''}`
    case 'extract_stated_reasons':
      return `Read the calls: customer says "${o.overall}"`
    case 'decide':
      return `${i.re_assess ? 'Re-checked with fresh statement' : 'Tested the claim against the statement'}: ${o.label} (${Math.round((o.confidence || 0) * 100)}%), claim is ${o.claim_vs_evidence}`
    case 'request_fresh_statement':
      return o.new_data
        ? `Asked for a fresh bank statement: got ${o.txn_count} transactions (had ${o.previous_txn_count})`
        : 'Asked for a fresh bank statement: nothing new came back'
    case 'finalize':
      return o.abstained ? 'Not confident enough, so the agent abstained' : `Final label: ${o.label}`
    default:
      return s.tool
  }
}
