# LeashPay

An AI shopping agent pays with your saved PayPal account, but only inside rules you set. Every purchase it attempts is **allowed** automatically, **sent to you for approval**, or **denied**, and every attempt lands in an audit log.

The rules are enforceable, not advisory: the agent has no path to PayPal except through a deterministic policy engine.

## How it works

```
"Buy me coffee beans"
        │
   Shopping agent (Claude, 2 tools)
        │  search_catalog(query)
        │  request_purchase(merchant, items, amount, reason)
        ▼
   gate.py ── re-prices the cart from the catalog (the agent's numbers are never trusted)
        │
   policy.check(policy, history, purchase)   ← pure function, no AI
        │
   ┌────┴─────────────┬──────────────────────┐
 allow             approve                 deny
   │                  │                      │
 charge vaulted    approval inbox         logged with
 PayPal account    (Approve → re-check     the rule that
 (buyer not        policy → charge)        fired
 present)
        └──────────── audit log · refunds · PayPal webhooks
```

1. **Wallet & rules.** Connect PayPal once with PayPal Vault (no purchase). Type rules in plain English, e.g. _"Max $100 per order and $200 a month. Groceries and books only. Ask me before anything over $30."_ Claude turns them into a JSON policy; you confirm it before it's saved.
2. **Shop.** Chat with the agent. It searches a demo catalog (BeanBox Coffee, PageTurn Books) and calls `request_purchase`. It cannot call PayPal.
3. **Policy engine** ([backend/policy.py](backend/policy.py)). `check()` returns `allow`, `approve` or `deny` plus the rule that fired. Rules run in order: category → per-order limit → monthly budget → approval threshold. No policy, or one with neither a per-order nor a monthly limit, means deny.
4. **Approvals.** A phone-friendly inbox shows pending purchases with the agent's reason. Approve re-checks the policy (the month may have filled up since) and then charges.
5. **Audit log.** An [AG Studio](https://www.ag-grid.com/studio/) dashboard of every attempt, with a chat panel whose agents build more of it on request. Group by decision, filter by merchant or status, and refund paid orders from the row. See below.

### Why the agent can't get around the rules

- `paypal.charge()` is only called from [backend/gate.py](backend/gate.py), and only after `policy.check()` returns `allow` (or a human approves and the re-check doesn't deny).
- The gate prices the cart from `catalog.json`. If the agent claims a different total, invents a SKU, or uses a SKU from another merchant, the attempt is denied and logged.
- Approve/reject/refund use an atomic status transition, so a double tap can't charge twice. Each charge also sends a per-purchase `PayPal-Request-Id`.
- Denied and pending attempts are logged the same way as paid ones.
- Only you can approve. Every write needs a header that other websites can't forge, and setting `APP_PASSWORD` puts the whole app behind a password.

## Audit log on AG Studio

The audit log is one AG Studio dashboard ([frontend/src/AuditLog.jsx](frontend/src/AuditLog.jsx)) over every purchase attempt, refreshed every 3 seconds.

- **Layout:** attempt and value tiles, a status button filter, a decision donut, a merchant list filter and two custom widgets. Click any chart or funnel step to cross-filter the rest. **Edit layout** switches to Studio's drag-and-drop editor, with both custom widgets in a "LeashPay" menu.
- **Purchases widget (custom):** AG Grid (Community) inside a Studio widget. It reads whatever the page's filters let through. Grouped by decision with merged cells, or newest first, with a **Refund** button on paid rows. Refund goes through the same `api()` → `gate.refund()` → `db.claim()` path as before.
- **Gate funnel widget (custom):** how many attempts reached each step of `policy.check()` (catalog → category → per order → monthly → approval → paid) and how many each step stopped. Clicking a step filters the dashboard to it. Its own selection highlights rather than empties it.
- **Theming:** Studio, its grids and charts read `--ag-*` variables that point at the app's own CSS tokens, so light and dark mode follow the OS without theme code.
- **Agents (Studio Agent Framework):** Studio's five built-in agents (lead, planning, data, page, widget) plus our **LeashPay auditor** as the primary agent. The auditor knows the gate, has a read-only `get_spending_rules` tool, filters the page to point at purchases, and delegates chart building to Studio's lead. Every agent runs on Claude: a small adapter posts each turn to `/api/studio/turn`, and [backend/llm.py](backend/llm.py) translates Studio's protocol for Claude. The API key never reaches the browser.
- **The dashboard agents are on a leash too.** Studio runs their tools in the page, against dashboard state only. None of them can reach the gate, so they can query, filter and build charts, but never buy or refund. A refund is always a person pressing the button.

## PayPal APIs used

| Step | API |
| --- | --- |
| OAuth | `POST /v1/oauth2/token` (cached until expiry) |
| Save PayPal without a purchase | `POST /v3/vault/setup-tokens` → buyer approves → `POST /v3/vault/payment-tokens` → store `vault_id` |
| Agent pays, buyer not present | `POST /v2/checkout/orders` with `payment_source.paypal.vault_id` (captures if the order isn't already `COMPLETED`) |
| Refund from the audit log | `POST /v2/payments/captures/{id}/refund` |
| Status updates | Webhooks `PAYMENT.CAPTURE.COMPLETED`, `.DECLINED`, `.DENIED`, `.REFUNDED`, `.REVERSED`, verified with `POST /v1/notifications/verify-webhook-signature` |

## Tools

- **PayPal REST APIs (sandbox):** Vault, Orders v2, Payments v2, Webhooks. See the table above.
- **AI: Claude (`claude-opus-5-5`)** through the Anthropic Python SDK. It powers the shopping agent (tool runner with `search_catalog` and `request_purchase`) and the plain-English → policy parser (structured output validated against a Pydantic model). All model calls are in [backend/llm.py](backend/llm.py); set `LLM_MODEL` to swap models.
- **AG Studio 3 (AG Grid):** the audit-log dashboard, its two custom widgets and its agent framework (`ag-studio-react`, plus `ag-grid-react` Community inside the Purchases widget). Without `VITE_AG_LICENSE_KEY` it runs with a watermark.
- **Render:** hosts the app as one Docker web service (FastAPI serves the API and the built React app).
- FastAPI, SQLModel/SQLite, React + Vite. Geist and Geist Mono from Google Fonts.

## Run it locally

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22, a PayPal Developer account and a Claude API key.

1. **PayPal sandbox app.** At [developer.paypal.com](https://developer.paypal.com) → Apps & Credentials → Sandbox, create an app and turn on **Save payment methods** (Vault) in its features. Use the default sandbox personal account as the buyer.
2. **Webhook (optional locally).** In the app's Webhooks section, add `https://<your-host>/api/webhooks/paypal` with the `PAYMENT.CAPTURE.*` events above and copy the Webhook ID. Locally you need a tunnel (ngrok, cloudflared) for PayPal to reach you; everything else works without it.
3. **Configure and start.**

   ```sh
   cp .env.example .env          # fill in PayPal + Anthropic keys
   cd backend && uv run --env-file ../.env uvicorn app:app --reload     # API on :8000
   cd frontend && npm install && npm run dev    # second terminal; UI on http://localhost:5173
   ```

4. **Try it.** "Buy me a bag of coffee beans" (allowed, $18), "Get the new cookbook set" ($65, goes to Approvals), "Buy a BeanBox $40 gift card" (denied: category). Then open **Audit log** and use the chat's starters: "Why denied?", "Spend by merchant" (the auditor hands it to Studio's lead, which adds a chart), "Anything suspicious?" (filters the page to the attempts worth a look).
5. **Tests** (policy engine, and the gate with PayPal stubbed):

   ```sh
   cd backend && uv run python test_policy.py && uv run python test_gate.py
   ```

## Deploy to Render

The repo has a `Dockerfile` and `render.yaml`. In Render, create a Blueprint from the repo and fill in `APP_PASSWORD` (optional; without it the app is open to anyone with the URL), `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` and `ANTHROPIC_API_KEY`, plus `VITE_AG_LICENSE_KEY` if you have an AG Studio key (it's read at build time). Then point the PayPal webhook at `https://<service>.onrender.com/api/webhooks/paypal`.

The free plan wipes SQLite on every restart. After connecting the sandbox buyer once, copy the vault ID from the service log line `Saved PayPal vault_id …` into `DEMO_VAULT_ID` so the hosted instance stays connected.

## Limitations and production path

- **One sandbox merchant.** All demo money goes to one sandbox merchant account; BeanBox and PageTurn are fake stores in `catalog.json`. A real deployment would pay real merchants through PayPal's agentic commerce services.
- **Single user, one shared password.** There is one wallet row, and the only lock is the optional shared `APP_PASSWORD` (HTTP Basic). Real users need accounts.
- **Per-order splitting.** The agent is told not to split orders to stay under the approval threshold, and the monthly budget caps the damage, but the engine doesn't detect splitting.
- **Failed charges aren't retried**; ask the agent again. A charge whose outcome is unclear (timeout, PayPal 5xx) stays `charging` and counts against the budget until PayPal's webhook settles it.
- **Dashboard layout isn't saved.** Widgets the agents add last until the page reloads; persisting `api.getState()` per user comes with accounts.
- **Chart colours carry no meaning.** Studio assigns palette colours by series position, so they're neutral on purpose; green/amber/red appears only on badges and the gate funnel, where it's tied to the value.
- Email notifications for approvals (e.g. via Resend) are not implemented; the inbox polls every 3 seconds.

## License

MIT, see [LICENSE](LICENSE).
