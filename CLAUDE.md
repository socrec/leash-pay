# LeashPay

AI shopping agent that can only pay through a deterministic policy gate. Small on purpose:
~600 lines of backend and three React files. Keep it that way.

## Layout

| File | Job | May import |
| --- | --- | --- |
| `backend/policy.py` | `check()` → allow / approve / deny. Pure: no I/O, no AI, no DB | pydantic only |
| `backend/gate.py` | The only path from a purchase request to PayPal. Prices from catalog, runs policy, logs, charges | db, policy, paypal |
| `backend/paypal.py` | Thin REST client. `charge()` is called from `gate.py` and nowhere else | httpx |
| `backend/llm.py` | Every model call, including `studio_turn()` (AG Studio's agent protocol ↔ Claude). Nothing else imports `anthropic` except `app.py`'s error handler | policy |
| `backend/agent.py` | System prompt + two tools. No PayPal, no DB | gate, llm |
| `backend/db.py` | SQLModel tables, `log()`, `claim()` | — |
| `backend/app.py` | HTTP routes only. No business logic | everything |
| `frontend/src/lib.jsx` | `api()`, `money`, `utc`, `when`, `itemsText`, `GATE`, `Icon`, `Badge`, `useBusy()`. Shared UI helpers go here | — |
| `frontend/src/AuditLog.jsx` | AG Studio dashboard (lazy-loaded): fields, layout, the Purchases and Gate funnel widgets, the Claude adapter and the auditor agent. Its agents' tools never reach the gate | lib |

## Commands

```sh
cd backend && uv run --env-file ../.env uvicorn app:app --reload        # API :8000
cd frontend && npm run dev                                               # UI :5173
cd backend && uv run python test_policy.py && uv run python test_gate.py # tests
cd frontend && npm run build                                             # must stay green
```

A change is done when both test files pass and the frontend builds.

## Invariants (security; never break these)

1. **Fail closed.** No policy, or a policy with neither a per-order nor a monthly cap → deny. An unknown merchant, an unknown SKU, a bad qty or a total mismatch → deny. When unsure, deny.
2. **Never trust the agent.** Prices, categories and totals come from `catalog.json`. The agent's numbers are only compared against them, never used.
3. **Every attempt is logged** with `db.log()`, including denials and failures.
4. **User-triggered transitions go through `db.claim()`** (atomic compare-and-set), so a double tap can't act twice.
5. **Our status ≠ PayPal's status.** Map PayPal values (`COMPLETED`, `PENDING`, `DECLINED`, …) to ours with an explicit dict (see `gate._pay`). Never use `status.lower()`: PayPal `PENDING` would turn into our `pending`, which means "waiting for the user's approval".
6. **Webhooks only move a purchase forward.** Each event has a set of statuses it may move from (`gate.WEBHOOK_STATUS`). Duplicate or late deliveries must be no-ops.
7. **No network call inside an open SQLite write transaction.** Commit before calling PayPal or the LLM, or other writers hit `database is locked` after 5 s.
   The PayPal stubs in `test_gate.py` fail if the database is write-locked when they're called.
8. **Budget checks count money in flight.** Count `charging` and `refunding` as spent, not just `paid`, and exclude the purchase being checked.
   `policy.SPENT` is the only definition of that set. The check and the commit that follows it run under `gate._lock`.
9. Secrets come only from env vars. Never log tokens, `.env` contents or full webhook headers.
10. **Only the user can act.** `app.guard` refuses non-GET requests without the `X-LeashPay` header (`api()` sends it), so
   other sites can't approve or refund (CSRF). Without `APP_PASSWORD` it serves only `localhost`; with it, every request
   needs the password. The PayPal webhook is the one exemption: it is checked by signature.

## Purchase status lifecycle

```
request ─┬─ deny ────────────────────────────► denied
         ├─ approve ─► pending ─┬─ reject ───► rejected
         │                      └─ approve ─► (re-check) ─► charging | denied   (denied if requested in an earlier month)
         └─ allow ────────────────────────────► charging
charging ─► paid | failed          (PayPal PENDING, a timeout or a 5xx stays charging; webhook COMPLETED → paid, also from failed)
paid ─► refunding ─► refunded      (refund error rolls back to paid; a PayPal reversal also ends in refunded)
```

If you add a status, update this diagram, the comment on `Purchase.status`, the badge CSS in `style.css`, `policy.SPENT` and `gate.WEBHOOK_STATUS`.

## Change checklists

**New policy rule:** the `Policy` field in `policy.py` → `check()` (keep the order: category, per-order, monthly, approval) → `POLICY_PROMPT` in `llm.py` → `PolicyView` in `main.jsx` → a test in `test_policy.py`.

**New category:** the `Category` Literal → `catalog.json` → the mapping in `POLICY_PROMPT`. `test_catalog_categories_are_known_to_policy` catches drift.

**New PayPal call:** add it to `paypal.py` via `call()`. Writes pass a deterministic `request_id` (see `gate._ref`) so retries are idempotent.
`PayPalError` means PayPal certainly did not act; a timeout or a 5xx is an unknown outcome, so don't turn it into one.

## Code style

- **KISS / YAGNI first.** No classes beyond SQLModel/Pydantic models. No interface with one implementation, no factories, no config for values that never change, no "for later" scaffolding.
- **No new dependencies** without a reason the stdlib or an installed package can't cover. That includes a router, a state library or a UI kit on the frontend.
- **DRY at three uses, not two.** Before writing a helper, check `lib.jsx`, `db.py` and `gate.py`. Don't hardcode the wallet id `1` in new places; the app is single-user (see the `ponytail:` note in `db.py`).
- **Domain logic stays out of `app.py`;** HTTP concerns stay out of `policy.py`. (`gate.py` raises `HTTPException(409)` for now. Swap it for a domain error if gate ever gets a non-HTTP caller.)
- **Money:** USD floats, rounded to cents at each boundary with `round(x, 2)`. Never compare floats with `==` or with an `abs(a - b) > eps` check, because NaN passes both. Compare rounded values, or write the check as `not (… <= eps)`.
- **Validate at trust boundaries:** agent tool input in `gate`, HTTP bodies with Pydantic in `app`, webhooks by signature first. Inside, trust your own types.
- **Comments say why,** not what. Deliberate shortcuts get a `ponytail:` comment naming the limit and the upgrade path, like the one in `db.py`.
- **Match density:** short functions and one-line docstrings on modules and on non-obvious functions. Keep the existing formatting (about 120 columns, double quotes in Python; single quotes and no semicolons in JS).
- **Tests:** plain `assert` functions, runnable with `uv run python test_x.py` and pytest. Stub PayPal by reassigning `paypal.charge` and `paypal.refund`, as `test_gate.py` does. Each gate test starts with `fresh()` and must pass alone, in any order.
  Every bug fix gets one test that fails without it. Don't add fixtures or frameworks.
- **Frontend:** every request goes through `api()`. Wrap user actions in `useBusy()` from `lib.jsx`: it ignores repeat taps and
  returns `busy` and `error`. Shop stays mounted (hidden) so the chat survives a tab switch.

## Known limits (documented, intentional)

Single user behind one shared password (`APP_PASSWORD`), no accounts. One sandbox merchant. Failed charges aren't retried.
A charge with an unclear outcome stays `charging` until a webhook settles it (no reconciliation job). Pending approvals
expire at the end of their month. Webhooks are verified with PayPal's postback API, so each POST to the webhook costs a
PayPal call (local verification would need an RSA library). The monthly budget uses the UTC calendar month. Splitting orders isn't detected. The budget lock works within one process only (one uvicorn worker). Don't "fix" these in passing; each one is its own task.
