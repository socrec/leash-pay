import { useEffect, useMemo, useState } from 'react'
import { AllCommunityModule } from 'ag-grid-community'
import { AgGridReact } from 'ag-grid-react'
import {
  AgStudioAiModule, createAiHarness, directLlmRunner, enableStudioDevValidations, studioGridTheme,
} from 'ag-studio'
import { AgStudio, AgStudioProvider, createWidgets } from 'ag-studio-react'
import { Badge, GATE, Icon, api, itemsText, useBusy, utc } from './lib.jsx'

if (import.meta.env.DEV) enableStudioDevValidations()

const DECISIONS = ['allow', 'approve', 'deny']
// Where the gate stopped each attempt, in check() order. "Before rules": no policy, unknown item or a wrong total.
const STEPS = ['Before rules', ...GATE.map(g => g.name), 'Passed']
const OUTCOME = { Approval: 'approve', Passed: 'allow' }
const step = p => p.decision === 'allow' ? 'Passed' : p.decision === 'approve' ? 'Approval'
  : GATE.find(g => g.stops.test(p.rule))?.name ?? 'Before rules'

// Declared rather than inferred: Studio reads fields once, and a fresh database has no rows to infer them from.
// Descriptions are what Studio's agents read about each field.
const FIELDS = [
  { id: 'id', format: 'integerFormat', name: '#' },
  { id: 'at', format: 'dateTimeFormat', name: 'Time', accessor: p => utc(p.at) },
  { id: 'merchant', format: 'textFormat', name: 'Merchant' },
  { id: 'items', format: 'textFormat', name: 'Purchase', accessor: p => itemsText(p.items) },
  { id: 'amount', format: 'currencyFormat', name: 'Amount', formatOptions: { format: '$#,##0.00' },
    description: "Order total in USD from the catalog, never the shopping agent's number." },
  { id: 'decision', format: 'textFormat', name: 'Decision',
    description: 'What the gate said: allow (paid automatically), approve (asked the user first), deny (stopped).' },
  { id: 'step', format: 'textFormat', name: 'Gate step', accessor: step,
    description: `Where the gate stopped the attempt, in order: ${STEPS.join(' → ')}.` },
  { id: 'status', format: 'textFormat', name: 'Status',
    description: "LeashPay's status, not PayPal's. pending: waiting for the user. charging, refunding: money in flight." },
  { id: 'rule', format: 'textFormat', name: 'Rule',
    description: 'The rule that decided. "agent said $X but the catalog total is $Y" means the agent misreported a total.' },
  { id: 'agent_reason', format: 'textFormat', name: 'Agent reason', description: "The shopping agent's own words." },
  { id: 'paypal_order_id', format: 'textFormat', name: 'PayPal order' },
]

const field = id => [{ id: `purchases.${id}` }]
const count = [{ id: 'purchases.id', aggregation: 'count' }]
const title = text => ({ title: { enabled: true, text } })
const STATE = {
  pages: [{
    id: 'audit',
    widgets: {
      attempts: { type: 'value', dataMapping: { value: count }, format: title('Attempts') },
      value: { type: 'value', dataMapping: { value: [{ id: 'purchases.amount', aggregation: 'sum' }] },
        format: title('Catalog value') },
      status: { type: 'button-filter', dataMapping: { value: field('status') }, format: title('Status') },
      decisions: { type: 'donut-chart', dataMapping: { categoryKey: field('decision'), valueKey: count },
        sort: [{ field: field('decision')[0], direction: 'asc' }], format: title('Decisions') },
      funnel: { type: 'gate-funnel', dataMapping: {}, format: title('Where the gate stopped them') },
      merchant: { type: 'list-filter', dataMapping: { value: field('merchant') }, format: title('Merchant') },
      purchases: { type: 'purchases', dataMapping: {}, format: title('Purchase attempts') },
    },
    widgetLayout: {
      attempts: { xTrack: 0, yTrack: 0, xSpan: 5, ySpan: 8 },
      value: { xTrack: 5, yTrack: 0, xSpan: 5, ySpan: 8 },
      status: { xTrack: 10, yTrack: 0, xSpan: 14, ySpan: 8 },
      decisions: { xTrack: 0, yTrack: 8, xSpan: 7, ySpan: 17 },
      funnel: { xTrack: 7, yTrack: 8, xSpan: 11, ySpan: 17 },
      merchant: { xTrack: 18, yTrack: 8, xSpan: 6, ySpan: 17 },
      purchases: { xTrack: 0, yTrack: 25, xSpan: 24, ySpan: 26 },
    },
  }],
  selectedPageId: 'audit',
  panels: { ai: { collapsed: false }, filters: { collapsed: true } },
}
// The chat isn't an editing panel, so view mode gets it too. Setting `panels` replaces both modes' defaults.
const PANELS = { view: { right: ['ai', 'filters'] }, edit: { left: ['data'], right: ['ai', 'edit', 'filters'] } }

// The rows a widget may show: Studio applies page filters, filter widgets and cross-filters, and hands the widget new
// params whenever any of them (or the data) change. A widget that highlights (featureConfig.crossFilter) also gets
// `all`, the rows before cross-filters, so its own selection doesn't empty it.
function useRows(params, only) {
  const { widgetApi } = params
  const [rows, setRows] = useState({ rows: [], all: [] })
  const fields = widgetApi.getSchema().find(t => t.id === 'purchases').fields
    .filter(f => !only || only.includes(name(f)))
  useEffect(() => {
    let live = true
    const pick = result => result.rows.map(row => Object.fromEntries(fields.map(f => [name(f), row[f.key]])))
    widgetApi.setDisplayState('loading', { prominent: false })
    widgetApi.getData({ kind: 'flat', fields }).then(r => {
      if (!live) return
      setRows({ rows: pick(r.crossFilter ?? r.results), all: pick(r.results) })
      widgetApi.setDisplayState(r.results.rows.length ? 'displayed' : 'noData')
    }).catch(() => {}) // a newer query replaced this one
    return () => { live = false }
  }, [params])
  return { ...rows, fields: Object.fromEntries(fields.map(f => [name(f), f])) }
}
const name = f => f.externalId // our FIELDS id; f.key ('purchases.amount') keys the rows

const COLUMNS = [
  { field: 'decision', width: 120, cellRenderer: p => <Badge value={p.value} /> },
  { field: 'id', width: 64 },
  { field: 'at', width: 170 },
  { field: 'items', flex: 2, minWidth: 220,
    cellRenderer: p => <><b>{p.value}</b> <span className="muted">{p.data.merchant}</span></> },
  { field: 'amount', width: 104, type: 'rightAligned' },
  { field: 'rule', flex: 1, minWidth: 180, tooltipField: 'rule' },
  { field: 'agent_reason', flex: 1, minWidth: 160, tooltipField: 'agent_reason' },
  { field: 'status', width: 124, pinned: 'right', cellRenderer: p => <Badge value={p.value} /> },
  { field: 'paypal_order_id', width: 170 },
]

// Every attempt, grouped by decision (merged cells, as AG Grid Community has no row grouping), with Refund on paid rows.
function Purchases(params) {
  const { widgetApi, context } = params
  const { rows, fields } = useRows(params)
  const [grouped, setGrouped] = useState(true)
  const [run, busy, error] = useBusy()
  const refund = run(async id => {
    try { await api(`/purchases/${id}/refund`, { method: 'POST' }) } finally { await context.reload() }
  })
  const newest = rows.toSorted((a, b) => b.id - a.id)
  const groups = Object.groupBy(newest, p => p.decision)
  const columns = [
    ...COLUMNS.map(c => ({
      headerName: widgetApi.getFieldName(fields[c.field]), spanRows: grouped && c.field === 'decision',
      valueFormatter: p => p.value == null ? '—' : widgetApi.formatFieldValue(fields[c.field], p.value), ...c,
    })),
    { colId: 'refund', headerName: '', width: 112, pinned: 'right', cellRenderer: p => p.data.status === 'paid' && (
      <button className="small" onClick={() => refund(p.data.id)} disabled={busy}>
        <Icon name="refund" size={13} />Refund
      </button>
    ) },
  ]
  return (
    <div className="widget">
      <div className="toolbar">
        <span className="muted small">{rows.length} attempts. Denials and failures are logged too.</span>
        <div className="segmented" role="group" aria-label="Order">
          <button aria-pressed={grouped} onClick={() => setGrouped(true)}>Group by decision</button>
          <button aria-pressed={!grouped} onClick={() => setGrouped(false)}>Newest first</button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="grid">
        <AgGridReact theme={studioGridTheme} modules={[AllCommunityModule]} enableCellSpan rowHeight={40}
          rowData={grouped ? DECISIONS.flatMap(d => groups[d] ?? []) : newest} columnDefs={columns}
          getRowId={p => String(p.data.id)} />
      </div>
    </div>
  )
}

// How many attempts reached each gate step, in check() order: [reached, stopped there] per step.
const reach = rows => {
  let left = rows.length
  return STEPS.map(s => {
    const reached = left, stop = rows.filter(r => r.step === s).length
    left -= stop
    return [reached, stop]
  })
}

// How far attempts got through the gate. Clicking a step cross-filters the rest of the dashboard to it; the faint part
// of each bar is what other filters left out.
function GateFunnel(params) {
  const { widgetApi } = params
  const { rows, all, fields } = useRows(params, ['id', 'step'])
  const [shown, total] = [reach(rows), reach(all)]
  const pct = n => `${all.length ? (100 * n) / all.length : 0}%`
  const picked = (widgetApi.getCrossFilterSelections() ?? []).flatMap(s => s.values ?? [])
  return (
    <ol className="funnel">
      {STEPS.map((s, i) => (
        <li key={s} className={OUTCOME[s] ?? 'deny'} aria-current={picked.includes(s) || undefined}>
          <button onClick={() => widgetApi.toggleCrossFilter({ type: 'value', field: fields.step, value: s, group: 0 })}>
            <b>{s}</b>
            <span className="meter">
              <i style={{ width: pct(shown[i][0]) }} /><i className="rest" style={{ width: pct(total[i][0] - shown[i][0]) }} />
            </span>
            <span className="mono small">
              {shown[i][0]} in · {shown[i][1]} {s === 'Passed' ? 'paid' : s === 'Approval' ? 'asked you' : 'stopped'}
            </span>
          </button>
        </li>
      ))}
    </ol>
  )
}

const noMapping = p => p.createDefaults({ dataMappingItems: [] }) // both read the schema themselves
const WIDGETS = defaults => createWidgets({
  additionalTypes: [
    { id: 'purchases', label: 'Purchases', comp: Purchases, form: noMapping,
      defaultSize: { width: 900, height: 420 }, minSize: { width: 480, height: 240 },
      ai: { description: 'Every purchase attempt, grouped by decision. Paid rows have a Refund button a person presses.' } },
    { id: 'gate-funnel', label: 'Gate funnel', comp: GateFunnel, form: noMapping,
      featureConfig: { crossFilter: { supportsHighlight: true } }, // its own selection highlights instead of emptying it
      defaultSize: { width: 520, height: 320 }, minSize: { width: 320, height: 240 },
      ai: { description: 'How many attempts reached each gate step and how many it stopped there. Click a step to filter.' } },
  ],
  menu: [...defaults.menu, { label: 'LeashPay', widgetIds: ['purchases', 'gate-funnel'] }],
})

// Studio's agents speak an OpenAI-style protocol. /api/studio/turn translates it for Claude and keeps the key on the
// server. One request per turn; the stream replays the finished answer.
const adapter = {
  executeTurn(request, { signal } = {}) {
    const complete = api('/studio/turn', { method: 'POST', body: request, signal }).catch(e => ({
      id: crypto.randomUUID(), createdAt: Date.now(), output: [],
      status: e.name === 'AbortError' ? 'cancelled' : 'failed', error: { code: e.name, message: e.message },
    }))
    async function* stream() {
      for (const it of (await complete).output) {
        if (it.type === 'message') yield { type: 'TEXT_MESSAGE_CHUNK', messageId: it.id, delta: it.content[0].text }
        if (it.type === 'function_call') {
          yield { type: 'TOOL_CALL_CHUNK', toolCallId: it.callId, toolCallName: it.name, delta: it.arguments }
        }
      }
    }
    return { stream: stream(), complete }
  },
}

const AUDITOR = `You are LeashPay's auditor. LeashPay lets an AI shopping agent pay with the user's PayPal account, but
only through a deterministic gate. This dashboard shows every purchase the agent attempted (table "purchases").
The gate checks, in order: the catalog (unknown item, bad quantity, or a total that doesn't match the catalog), then the
user's rules: category, per-order limit, monthly budget, then the ask-me limit. "allow" was paid automatically,
"approve" waited for the user, "deny" was stopped. Amounts are catalog prices, never the shopping agent's numbers.
Answer with numbers from execute_query and get_spending_rules, then one line on what they mean. To point the user at
specific purchases, add a page filter (for example on purchases.id) instead of listing them, and offer to remove it.
For new or changed charts, delegate to the lead with a precise brief. You can't buy or refund anything: if a purchase
should be refunded, filter to it and tell the user to press Refund in the Purchases widget.`

const STARTERS = [
  { label: 'Why denied?', prompt: 'Why were purchases denied this month?' },
  { label: 'Spend by merchant', prompt: 'Add a chart of catalog value by merchant, split by decision.' },
  { label: 'Anything suspicious?', prompt: 'Anything suspicious in what the shopping agent tried?' },
]

// Studio's five built-in agents (lead, planning, data, page, widget) plus our auditor up front. All run on Claude.
const ai = ({ api: studio }) => {
  const rules = studio.defineAiTool({
    name: 'get_spending_rules',
    description: "The user's spending rules and this UTC month's spend (paid plus money in flight). Not in the table.",
    params: s => s.object({}),
    async execute(_, ctx) {
      try {
        const w = await api('/wallet', { signal: ctx.signal })
        return ctx.success(JSON.stringify({ policy: w.policy, spent_this_month: w.spent }))
      } catch (e) { return ctx.error(e.message) }
    },
  })
  return createAiHarness(studio, ({ builtIn, tools }) => ({
    agents: [
      ...Object.values(builtIn).map(d => directLlmRunner({ ...d, adapter })),
      directLlmRunner({
        id: 'auditor', name: 'LeashPay auditor', adapter, schema: s => s.undefined(),
        description: 'Explains purchase attempts and the gate; hands dashboard building to the lead.',
        instructions: () => `${AUDITOR}\nToday (UTC) is ${new Date().toISOString().slice(0, 10)}.`,
        tools: () => [tools.studio.viewSchema(), tools.studio.viewPage(), tools.studio.executeQuery(),
          tools.studio.addPageFilter(), tools.studio.removePageFilter(), rules, tools.delegateTo(['lead'])],
      }),
    ],
    primary: 'auditor',
    promptStarters: STARTERS,
  }))
}

export default function AuditLog({ purchases, reload }) {
  const [mode, setMode] = useState('view')
  // App only swaps `purchases` when the JSON changes, so the 3 s poll doesn't make Studio reload for nothing.
  const data = useMemo(() => ({ sources: [{ id: 'purchases', data: purchases, fields: FIELDS }] }), [purchases])
  const context = useMemo(() => ({ reload }), [reload])
  return (
    <div className="page">
      <div className="row between">
        <div className="head">
          <h1>Audit log</h1>
          <p>Every purchase attempt, allowed or not. Amounts are the catalog's prices, never the agent's.</p>
        </div>
        <div className="segmented" role="group" aria-label="Dashboard mode">
          <button aria-pressed={mode === 'view'} onClick={() => setMode('view')}>View</button>
          <button aria-pressed={mode === 'edit'} onClick={() => setMode('edit')}>Edit layout</button>
        </div>
      </div>
      <AgStudioProvider licenseKey={import.meta.env.VITE_AG_LICENSE_KEY} modules={[AgStudioAiModule]}>
        <AgStudio className="studio" data={data} mode={mode} initialState={STATE} panels={PANELS} widgets={WIDGETS}
          ai={ai} context={context} />
      </AgStudioProvider>
    </div>
  )
}
