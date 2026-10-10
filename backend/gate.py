"""The gate: the only code path from an agent's request to PayPal. Every charge passes policy.check() first."""
import json
import threading
from pathlib import Path

from fastapi import HTTPException
from sqlmodel import Session, select

import paypal
import policy
from db import Purchase, Wallet, claim, engine, log, now

CATALOG = json.loads((Path(__file__).parent / "catalog.json").read_text())
# ponytail: process-wide lock so two budget checks can't both pass on the same spend (uvicorn runs one worker);
# with several workers, take a DB lock instead (BEGIN IMMEDIATE / SELECT ... FOR UPDATE).
_lock = threading.Lock()


def _merchant(name: str) -> dict | None:
    key = name.strip().lower()
    return next((m for m in CATALOG["merchants"] if key in (m["id"], m["name"].lower())), None)


def _resolve(merchant: dict | None, items: list[dict]) -> tuple[list[dict], str | None]:
    """Price the cart from the catalog. Never trust the agent's prices or categories."""
    if not merchant:
        return [], "unknown merchant"
    stock = {i["sku"]: i for i in merchant["items"]}
    lines = []
    for it in items:
        item, qty = stock.get(it.get("sku")), it.get("qty")
        if not item:
            return [], f"{it.get('sku')!r} is not sold by {merchant['name']}"
        if type(qty) is not int or not 1 <= qty <= 20:  # type(), not isinstance(): True is an int
            return [], f"invalid quantity {qty!r}"
        if any(line["sku"] == item["sku"] for line in lines):
            return [], f"{item['sku']} is listed twice"
        lines.append({**item, "qty": qty})
    return lines, None if lines else "empty order"


def _history(s: Session, exclude: int | None = None) -> list[dict]:
    """Every purchase whose money counts toward the budget, except `exclude` (it may already be "charging")."""
    return [{"amount": o.amount, "status": o.status, "at": o.at}
            for o in s.exec(select(Purchase).where(Purchase.status.in_(policy.SPENT), Purchase.id != exclude))]


def _check(s: Session, p: Purchase) -> tuple[str, str]:
    return policy.check(s.get(Wallet, 1).policy, _history(s, p.id),
                        {"amount": p.amount, "categories": [line["category"] for line in p.items]})


def spent_this_month() -> float:
    """This month's spend as the budget check sees it, for the UI."""
    with Session(engine) as s:
        return round(policy.month_spend(_history(s), now()), 2)


def _ref(p: Purchase) -> str:
    """Unique per purchase even if the SQLite file is reset; used as PayPal custom_id and idempotency key."""
    return f"lp-{p.id}-{p.at:%Y%m%d%H%M%S%f}"


def _pay(s: Session, p: Purchase):
    wallet = s.get(Wallet, 1)
    try:
        if not wallet.vault_id:
            raise paypal.PayPalError("no PayPal account connected")
        p.paypal_order_id, p.capture_id, status = paypal.charge(
            wallet.vault_id, p.amount, f"LeashPay #{p.id} · {p.merchant}", _ref(p), _ref(p))
        # PayPal PENDING is not our "pending" (= waiting for the user): stay "charging" until the webhook.
        p.status = {"COMPLETED": "paid", "PENDING": "charging"}.get(status, "failed")
        log(s, p.id, "charged", {"order_id": p.paypal_order_id, "capture_id": p.capture_id, "status": status})
    except paypal.PayPalError as e:  # PayPal refused, or was never reached: nothing was charged
        p.status = "failed"
        log(s, p.id, "charge_failed", {"error": str(e)})
    except Exception as e:  # a timeout, a 5xx or an odd reply: the money may have moved, so keep counting it as spent
        # ponytail: stays "charging" until a webhook settles it; add a reconciliation (GET the order) if webhooks miss.
        p.status = "charging"
        log(s, p.id, "charge_unknown", {"error": repr(e)})
    s.add(p)
    s.commit()


def request_purchase(merchant: str, items: list[dict], amount: float, reason: str) -> dict:
    m = _merchant(merchant)
    lines, problem = _resolve(m, items)
    total = round(sum(line["price"] * line["qty"] for line in lines), 2)
    with Session(engine) as s:
        with _lock:  # check and commit "charging" together
            # amount is never the agent's: an inf (JSON 1e400) on a denied row would break the purchase list for good
            p = Purchase(merchant=m["name"] if m else merchant, items=lines or items, amount=total,
                         agent_reason=reason[:500], decision="deny", rule=problem or "", status="denied")
            if not problem and round(amount, 2) != total:  # != so a NaN amount is caught too
                p.rule = f"agent said ${amount:.2f} but the catalog total is ${total:.2f}"
            elif not problem:
                p.decision, p.rule = _check(s, p)
                p.status = {"allow": "charging", "approve": "pending", "deny": "denied"}[p.decision]
            s.add(p)
            s.commit()
        s.refresh(p)
        log(s, p.id, "requested", {"merchant": merchant, "items": items, "amount": amount, "reason": reason})
        log(s, p.id, p.decision, {"rule": p.rule})
        s.commit()
        if p.decision == "allow":
            _pay(s, p)
        s.refresh(p)
        return p.model_dump()


def approve(pid: int) -> dict:
    with Session(engine) as s:
        with _lock:
            if not claim(s, pid, "pending", "charging"):
                raise HTTPException(409, "purchase is not pending")
            p = s.get(Purchase, pid)
            decision, rule = _check(s, p)  # rules or this month's spend may have changed since the request
            if (p.at.year, p.at.month) != (now().year, now().month):  # budgets count p in its own, closed month
                decision, rule = "deny", "it was requested in an earlier month; ask the agent again"
            if decision == "deny":
                p.status, p.rule = "denied", f"re-checked at approval: {rule}"
                log(s, pid, "deny", {"rule": p.rule})
                s.add(p)
            else:
                log(s, pid, "approved")
            s.commit()  # before paying: a write transaction left open across the PayPal call locks SQLite
        if p.status == "charging":
            _pay(s, p)
        s.refresh(p)
        return p.model_dump()


def reject(pid: int) -> dict:
    with Session(engine) as s:
        if not claim(s, pid, "pending", "rejected"):
            raise HTTPException(409, "purchase is not pending")
        log(s, pid, "rejected")
        s.commit()
        return s.get(Purchase, pid).model_dump()


def refund(pid: int) -> dict:
    with Session(engine) as s:
        if not claim(s, pid, "paid", "refunding"):
            raise HTTPException(409, "purchase is not paid")
        p = s.get(Purchase, pid)
        try:
            r = paypal.refund(p.capture_id, _ref(p) + "-refund")  # a retry gets the first attempt's result
        except Exception as e:
            p.status = "paid"
            log(s, pid, "refund_failed", {"error": str(e)})
            s.add(p)
            s.commit()
            raise HTTPException(502, f"refund failed: {e}")
        p.status = "refunded" if r.get("status") == "COMPLETED" else "refunding"  # webhook finishes a pending refund
        log(s, pid, "refund", {"refund_id": r.get("id"), "status": r.get("status")})
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.model_dump()


# event → (statuses it may move a purchase from, new status). Late or repeated deliveries change nothing.
WEBHOOK_STATUS = {"PAYMENT.CAPTURE.COMPLETED": ({"charging", "failed"}, "paid"),
                  "PAYMENT.CAPTURE.DENIED": ({"charging"}, "failed"),
                  "PAYMENT.CAPTURE.DECLINED": ({"charging"}, "failed"),
                  "PAYMENT.CAPTURE.REFUNDED": ({"paid", "refunding"}, "refunded"),
                  "PAYMENT.CAPTURE.REVERSED": ({"paid", "refunding"}, "refunded")}  # PayPal took it back (a dispute)


def apply_webhook(event: dict):
    """Apply a verified PayPal webhook to the matching purchase; every event is logged either way."""
    kind, res = event.get("event_type"), event.get("resource", {})
    is_refund = kind in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED")
    if is_refund:  # resource is the refund; its "up" link is the capture
        capture_id = next((link["href"].rsplit("/", 1)[-1] for link in res.get("links", []) if link["rel"] == "up"), None)
    else:
        capture_id = res.get("id")
    with Session(engine) as s:
        p = s.exec(select(Purchase).where(Purchase.capture_id == capture_id)).first() if capture_id else None
        if not p and str(res.get("custom_id", "")).startswith("lp-"):
            p = s.get(Purchase, int(res["custom_id"].split("-")[1]))
            p = p if p and _ref(p) == res["custom_id"] else None
        sources, target = WEBHOOK_STATUS.get(kind, ((), None))
        if p and p.status in sources:
            p.status = target
            if not is_refund:
                p.capture_id = capture_id
            s.add(p)
        log(s, p.id if p else None, f"webhook:{kind}", event)
        s.commit()
