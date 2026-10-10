import { StrictMode, Suspense, lazy, useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Badge, GATE, Icon, api, itemsText, money, useBusy, when } from './lib.jsx'
import './style.css'

const AuditLog = lazy(() => import('./AuditLog.jsx')) // AG Studio is megabytes: load it only for this tab
const EXAMPLE_RULES = 'Max $100 per order and $200 a month. Groceries and books only. Ask me before anything over $30.'
const SUGGESTIONS = ['Buy me a bag of coffee beans', 'Get the new cookbook set', 'Buy a BeanBox $40 gift card']
const TRACE = { pass: ['check', 'passed'], deny: ['x', 'stopped here'], approve: ['clock', 'asks you'], skip: ['minus', 'not checked'] }
const DONE = { paid: 'ok', charging: 'notice', rejected: 'notice' } // how an approve/reject ended; anything else is an error
const thisMonth = () => new Date().toLocaleString('en', { month: 'long', timeZone: 'UTC' })

function App() {
  const [page, setPage] = useState(location.hash.slice(2) || 'shop')
  const [wallet, setWallet] = useState(null)
  const [purchases, setPurchases] = useState([])
  const [logOpened, setLogOpened] = useState(false)
  if (page === 'log' && !logOpened) setLogOpened(true)

  const reload = useCallback(() => Promise.all([
    api('/wallet').then(setWallet).catch(() => {}),
    api('/purchases').then(next => setPurchases(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next)).catch(() => {}),
  ]), [])

  useEffect(() => {
    const onHash = () => { setPage(location.hash.slice(2) || 'shop'); scrollTo(0, 0) } // a new tab starts at its top
    addEventListener('hashchange', onHash)
    reload()
    const timer = setInterval(reload, 3000) // inbox and audit log pick up webhook/phone changes
    return () => { removeEventListener('hashchange', onHash); clearInterval(timer) }
  }, [reload])

  const pending = purchases.filter(p => p.status === 'pending').length
  const tabs = [['shop', 'Shop'], ['inbox', 'Approvals'], ['log', 'Audit log'], ['wallet', 'Wallet & rules']]

  return (
    <>
      <header className="top">
        <div className="bar">
          <a className="logo" href="#/shop"><span className="mark"><Icon name="gate" size={18} /></span>LeashPay</a>
          <nav aria-label="Main">
            {tabs.map(([id, label]) => (
              <a key={id} href={`#/${id}`} aria-current={page === id ? 'page' : undefined}>
                {label}{id === 'inbox' && pending > 0 && <span className="count">{pending}</span>}
              </a>
            ))}
          </nav>
          {wallet && (
            <a className={`pill ${wallet.connected ? 'paid' : 'pending'}`} href="#/wallet">
              <i className="dot" />PayPal {wallet.connected ? 'connected' : 'not connected'}
            </a>
          )}
        </div>
      </header>
      <main>
        {!wallet ? <p className="muted">Loading…</p> : <>
          {/* Shop stays mounted so the chat, and a reply still on its way, survive a tab switch */}
          <div hidden={['wallet', 'inbox', 'log'].includes(page)}><Shop wallet={wallet} purchases={purchases} reload={reload} /></div>
          {page === 'wallet' && <Wallet wallet={wallet} reload={reload} />}
          {page === 'inbox' && <Inbox wallet={wallet} purchases={purchases} reload={reload} />}
          {/* Once opened, the audit log stays mounted too, so the dashboard agents' chat and widgets survive a tab switch */}
          {logOpened && (
            <div hidden={page !== 'log'}>
              <Suspense fallback={<p className="muted">Loading the dashboard…</p>}>
                <AuditLog purchases={purchases} reload={reload} />
              </Suspense>
            </div>
          )}
        </>}
      </main>
    </>
  )
}

function PolicyView({ policy, tiles }) {
  if (!policy) return <p className="muted">No rules yet, so every purchase is denied.</p>
  const limit = v => (v == null ? 'no limit' : money(v))
  const categories = policy.allowed_categories
  return (
    <dl className={tiles ? 'policy tiles' : 'policy'}>
      <div><dt>Per order</dt><dd>{limit(policy.max_per_order)}</dd></div>
      <div><dt>Per month</dt><dd>{limit(policy.max_per_month)}</dd></div>
      <div><dt>Ask me above</dt><dd>{policy.approve_above == null ? 'never ask' : money(policy.approve_above)}</dd></div>
      <div>
        <dt>Categories</dt>
        <dd className="categories">{categories == null ? 'any' : categories.length ? categories.map(c => <span key={c}>{c}</span>) : 'none'}</dd>
      </div>
    </dl>
  )
}

// Budget bar: what already counts (ink), plus an amount on top of it (amber).
function Meter({ limit, spent, extra = 0 }) {
  const width = v => `${Math.min(100, (100 * v) / limit)}%`
  return (
    <div className="meter" aria-hidden="true">
      <i style={{ width: width(spent) }} />{extra > 0 && <i className="extra" style={{ width: width(extra) }} />}
    </div>
  )
}

function Wallet({ wallet, reload }) {
  const [text, setText] = useState(EXAMPLE_RULES)
  const [draft, setDraft] = useState(null)
  const [run, busy, error] = useBusy()

  const connect = run(async () => { location.href = (await api('/wallet/connect', { method: 'POST' })).approve_url })
  const parse = run(async () => setDraft(await api('/policy/parse', { method: 'POST', body: { text } })))
  const confirm = run(async () => { await api('/policy', { method: 'PUT', body: draft }); setDraft(null); reload() })
  const rulesDone = wallet.policy && !draft

  return (
    <div className="page">
      <div className="head">
        <h1>Wallet &amp; rules</h1>
        <p>Connect PayPal once, then tell the gate what your agent may spend.</p>
      </div>
      <div className="split">
        <div className="stack">
          <section className="card">
            <div className="step-head">
              <span className={wallet.connected ? 'num done' : 'num'}>{wallet.connected ? <Icon name="check" size={14} /> : 1}</span>
              <h2>Connect PayPal</h2><span className="mono muted small">Step 1</span>
            </div>
            {wallet.connected
              ? <p className="ok">PayPal connected{wallet.payer_email && <> as <b>{wallet.payer_email}</b></>}</p>
              : <button className="primary" onClick={connect} disabled={busy}>Connect PayPal</button>}
            <p className="muted">Your PayPal account is saved with PayPal Vault, with no purchase. The agent never sees it: it can only ask, and your rules decide.</p>
          </section>

          <section className="card">
            <div className="step-head">
              <span className={rulesDone ? 'num done' : 'num'}>{rulesDone ? <Icon name="check" size={14} /> : 2}</span>
              <h2>Set your rules in plain English</h2><span className="mono muted small">Step 2</span>
            </div>
            <label className="field">Your rules<textarea rows={3} value={text} onChange={e => { setText(e.target.value); setDraft(null) }} /></label>
            <div className="row">
              <button onClick={parse} disabled={busy || !text.trim()}>{busy && !draft ? 'Reading…' : 'Read my rules'}</button>
              <span className="muted small">A model reads your words. Plain code enforces the result.</span>
            </div>
            {draft && (
              <div className="draft">
                <h3>Does this look right?</h3>
                <PolicyView policy={draft} tiles />
                <p className="muted small">Nothing changes until you confirm.</p>
                <div className="row">
                  <button className="primary" onClick={confirm} disabled={busy}>Confirm policy</button>
                  <button className="ghost" onClick={() => setDraft(null)}>Discard</button>
                </div>
              </div>
            )}
          </section>
          {error && <p className="error">{error}</p>}
        </div>

        <aside>
          <section className="card">
            <h2>Active policy</h2>
            <PolicyView policy={wallet.policy} />
          </section>
          <section className="card">
            <h2>How the gate decides</h2>
            <ol className="steps">
              {GATE.map((step, i) => (
                <li key={step.name}><span className="num">{i + 1}</span><span><b>{step.name}</b><span className="muted small">{step.about}</span></span></li>
              ))}
            </ol>
            <p className="callout"><Icon name="shield" />No per-order or monthly limit, an unknown item or a price that doesn't match the catalog: the gate denies.</p>
          </section>
        </aside>
      </div>
    </div>
  )
}

function GateTrace({ p }) {
  const fired = p.decision === 'allow' ? GATE.length : GATE.findIndex(step => step.stops.test(p.rule))
  if (fired < 0) return null // stopped before the rules ran: no policy, unknown item, wrong total
  return (
    <div className="trace">
      <span className="eyebrow">Gate</span>
      <ol>
        {GATE.map((step, i) => {
          const state = i < fired ? 'pass' : i === fired ? step.outcome : 'skip'
          const [icon, label] = TRACE[state]
          return <li key={step.name} className={state}><span className="step"><Icon name={icon} size={11} /></span>{step.name}<span className="sr">: {label}</span></li>
        })}
      </ol>
    </div>
  )
}

function PurchaseCard({ p }) {
  return (
    <div className="purchase">
      <div className="body">
        <div className="row">
          <Badge value={p.decision} /><span className="muted">{p.merchant}</span>
          <span className="end"><b className="mono">{money(p.amount)}</b><Badge value={p.status} /></span>
        </div>
        <div className="row between">
          <span>{itemsText(p.items)}</span>
          {p.status === 'pending' && <a className="row" href="#/inbox">Review in Approvals<Icon name="chevron" size={14} /></a>}
        </div>
      </div>
      <div className="foot">
        <GateTrace p={p} />
        <p className="mono muted">Rule: {p.rule}{p.paypal_order_id && ` · PayPal order ${p.paypal_order_id}`}</p>
      </div>
    </div>
  )
}

function Message({ m, live }) {
  if (m.role === 'user') return <p className="bubble">{m.text}</p>
  if (m.role === 'error') return <p className="error">{m.text}</p>
  return (
    <div className="reply">
      <span className="avatar"><Icon name="gate" /></span>
      <div className="stack">
        <p>{m.text}</p>
        {m.purchases?.map(p => <PurchaseCard key={p.id} p={live[p.id] ?? p} />)}
      </div>
    </div>
  )
}

function Budget({ wallet }) {
  const limit = wallet.policy?.max_per_month
  return (
    <section className="card">
      <div className="row between"><h2>{thisMonth()} budget</h2><span className="mono muted small">UTC month</span></div>
      <p className="figure"><b>{money(wallet.spent)}</b>{limit != null && `of ${money(limit)}`}</p>
      {limit != null && <Meter limit={limit} spent={wallet.spent} />}
      <p className="muted small">
        {limit == null ? 'No monthly limit. ' : `${money(Math.max(0, limit - wallet.spent))} left. `}
        Paid orders and charges still in flight both count.
      </p>
    </section>
  )
}

function Merchants() {
  const [merchants, setMerchants] = useState([])
  useEffect(() => { api('/catalog').then(c => setMerchants(c.merchants)).catch(() => {}) }, [])
  return (
    <section className="card">
      <h2>Where it can shop</h2>
      <ul className="merchants">
        {merchants.map(m => <li key={m.id}><b>{m.name}</b><span className="muted small">{m.items.length} items</span></li>)}
      </ul>
      <p className="muted small">Prices and categories come from the catalog, never from the agent.</p>
    </section>
  )
}

function Shop({ wallet, purchases, reload }) {
  const [chat, setChat] = useState([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const live = Object.fromEntries(purchases.map(p => [p.id, p])) // chat cards follow later approvals and refunds
  // The model gets past turns as text only; without its gate results it decides those purchases never happened.
  const results = ps => (ps?.length ? `\n[gate results: ${ps.map(p => live[p.id] ?? p)
    .map(p => `#${p.id} ${itemsText(p.items)}, ${money(p.amount)}: ${p.status} (${p.rule})`).join('; ')}]` : '')

  const send = async message => {
    if (!message.trim()) return
    setText(''); setBusy(true)
    setChat(c => [...c, { role: 'user', text: message }])
    try {
      // Chat is user/reply pairs, so an even slice starts with a user turn. Failed replies aren't sent.
      const history = chat.slice(-20).filter(m => m.role !== 'error')
        .map(m => ({ role: m.role === 'agent' ? 'assistant' : 'user', content: m.text + results(m.purchases) }))
      const r = await api('/agent', { method: 'POST', body: { text: message, history } })
      setChat(c => [...c, { role: 'agent', text: r.reply, purchases: r.purchases }])
      reload()
    } catch (e) {
      setChat(c => [...c, { role: 'error', text: e.message }])
    } finally { setBusy(false) }
  }

  return (
    <div className="page">
      <div className="head">
        <h1>Shop</h1>
        <p>Your agent can only ask to buy. Your rules decide, and PayPal is charged only when they say yes.</p>
      </div>
      {!wallet.policy && <p className="notice">No rules yet, so every purchase will be denied. <a href="#/wallet">Set your rules</a></p>}
      {!wallet.connected && <p className="notice">PayPal isn't connected, so allowed purchases can't be paid. <a href="#/wallet">Connect</a></p>}
      <div className="split">
        <section className="card flush" aria-label="Chat with your agent">
          <div className="messages">
            {chat.length === 0 && <p className="muted">Ask the agent to buy something from BeanBox Coffee or PageTurn Books.</p>}
            {chat.map((m, i) => <Message key={i} m={m} live={live} />)}
            {busy && <p className="typing"><span className="dots"><i /><i /><i /></span>Agent is shopping…</p>}
          </div>
          <div className="composer">
            <div className="row">
              {SUGGESTIONS.map(s => <button key={s} className="chip" onClick={() => send(s)} disabled={busy}>{s}</button>)}
            </div>
            <form onSubmit={e => { e.preventDefault(); send(text) }}>
              <label className="sr" htmlFor="ask">Tell your agent what to buy</label>
              <input id="ask" value={text} onChange={e => setText(e.target.value)} placeholder="Tell your agent what to buy…" disabled={busy} />
              <button className="primary" disabled={busy || !text.trim()}>Send<Icon name="send" /></button>
            </form>
          </div>
        </section>
        <aside>
          <Budget wallet={wallet} />
          <section className="card">
            <div className="row between"><h2>Your rules</h2><a href="#/wallet">Edit</a></div>
            <PolicyView policy={wallet.policy} />
          </section>
          <Merchants />
        </aside>
      </div>
    </div>
  )
}

function Inbox({ wallet, purchases, reload }) {
  const [run, busy, error] = useBusy()
  const pending = purchases.filter(p => p.status === 'pending')
  const limit = wallet.policy?.max_per_month
  const [done, setDone] = useState(null) // the last approve/reject result: the card vanishes either way, so say how it ended
  const act = run(async (id, action) => {
    setDone(null)
    try { setDone(await api(`/purchases/${id}/${action}`, { method: 'POST' })) } finally { await reload() }
  })
  return (
    <div className="page narrow">
      <div className="head">
        <h1>Waiting for you</h1>
        <p>Purchases above your ask-me limit land here. Approving re-checks every rule before PayPal is charged, so a change since the request can still deny it.</p>
      </div>
      {error && <p className="error">{error}</p>}
      {done && (
        <p className={DONE[done.status] ?? 'error'}>
          #{done.id} · {money(done.amount)} at {done.merchant}: <b>{done.status}</b>{done.status === 'denied' && ` (${done.rule})`}
        </p>
      )}
      {pending.length === 0 && (
        <div className="empty"><b>Nothing to approve.</b><span className="muted">Purchases above your "ask me" limit land here.</span></div>
      )}
      {pending.map(p => (
        <section key={p.id} className="card flush approval" aria-label={`Approve ${money(p.amount)} at ${p.merchant}`}>
          <div className="body">
            <div className="row between">
              <span className="row"><Badge value={p.decision} /><b>{p.merchant}</b></span>
              <span className="mono muted small">{when(p.at)}</span>
            </div>
            <div>
              <p className="amount">{money(p.amount)}</p>
              <p>{itemsText(p.items)}</p>
            </div>
            <div className="why">
              <div><span className="eyebrow">Agent says</span>{p.agent_reason}</div>
              <div className="asked"><span className="eyebrow">Why you're asked</span><span className="mono small">{p.rule}</span></div>
            </div>
            {limit != null && (
              <div className="stack">
                <div className="row between small">
                  <span className="muted">If approved, {thisMonth()} spend becomes</span>
                  <span className="mono"><b>{money(wallet.spent + p.amount)}</b> <span className="muted">of {money(limit)}</span></span>
                </div>
                <Meter limit={limit} spent={wallet.spent} extra={p.amount} />
                {Math.round((wallet.spent + p.amount) * 100) > Math.round(limit * 100) && (
                  <p className="error small">That's over your monthly budget: if you approve, the gate will deny it.</p>
                )}
              </div>
            )}
          </div>
          <div className="actions">
            <button className="danger" onClick={() => act(p.id, 'reject')} disabled={busy}>Reject</button>
            <button className="primary" onClick={() => act(p.id, 'approve')} disabled={busy}><Icon name="check" />Approve {money(p.amount)}</button>
          </div>
        </section>
      ))}
    </div>
  )
}

createRoot(document.getElementById('root')).render(<StrictMode><App /></StrictMode>)
