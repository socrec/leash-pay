import { useCallback, useRef, useState } from 'react'

export async function api(path, { method = 'GET', body, signal } = {}) {
  const r = await fetch('/api' + path, {
    method,
    // The backend refuses writes without X-LeashPay: another site can't add it without a CORS preflight (CSRF).
    headers: { 'X-LeashPay': '1', ...(body && { 'Content-Type': 'application/json' }) },
    body: body && JSON.stringify(body),
    signal,
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? r.statusText))
  return data
}

export const money = n => `$${Number(n).toFixed(2)}`
export const itemsText = items => items.map(i => `${i.qty ?? 1}× ${i.name ?? i.sku}`).join(', ')
// SQLite returns naive UTC timestamps.
export const utc = at => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(at) ? at : at + 'Z')
const TIME = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' }
export const when = at => utc(at).toLocaleString([], TIME)

// policy.check() runs these in order. `stops` matches the rule text a step writes when it fires (test_policy pins it).
export const GATE = [
  { name: 'Category', outcome: 'deny', stops: /not allowed/, about: 'Only the categories you allow can be bought.' },
  { name: 'Per order', outcome: 'deny', stops: /per-order limit/, about: 'Any single order above the limit is refused.' },
  { name: 'Monthly', outcome: 'deny', stops: /monthly budget/,
    about: 'Refused if it would push this month past the budget. Paid and in-flight charges count.' },
  { name: 'Approval', outcome: 'approve', stops: /auto-approve limit/, about: 'Above your ask-me limit, it waits for you.' },
]

const ICONS = {
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  x: <path d="M7 7l10 10M17 7L7 17" />,
  minus: <path d="M7 12h10" />,
  gate: <path d="M6 4v16M18 4v16M6 9h12M6 15h12" />,
  chevron: <path d="M9 6l6 6-6 6" />,
  send: <path d="M5 12h13M13 6l6 6-6 6" />,
  refund: <><path d="M4 4v6h6" /><path d="M4.5 10A8 8 0 1 1 6 16.5" /></>,
  shield: <path d="M12 3l7 3v5.5c0 4.4-3 7.7-7 9.5-4-1.8-7-5.1-7-9.5V6l7-3z" />,
}
export const Icon = ({ name, size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{ICONS[name]}</svg>
)

const DECISION_ICON = { allow: 'check', approve: 'clock', deny: 'x' }
// A decision (what the gate said) is filled, with an icon. A status (where the money is) is outlined, with a dot.
export const Badge = ({ value }) => (
  <span className={`tag ${value}`}>
    {DECISION_ICON[value] ? <Icon name={DECISION_ICON[value]} size={12} /> : <i className="dot" />}{value}
  </span>
)

// One request at a time: `busy` while it runs, `error` if it throws. Taps while busy are ignored (no double submits).
export function useBusy() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const running = useRef(false)
  const run = useCallback(fn => async (...args) => {
    if (running.current) return
    running.current = true; setBusy(true); setError('')
    try { await fn(...args) } catch (e) { setError(e.message) } finally { running.current = false; setBusy(false) }
  }, [])
  return [run, busy, error]
}
