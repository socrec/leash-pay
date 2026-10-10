"""The policy engine: a pure function, no AI, no I/O. Every purchase passes through check()."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Category = Literal["groceries", "books", "gift_cards", "kitchenware"]
Money = Annotated[float, Field(ge=0, allow_inf_nan=False)]  # JSON can't send Infinity back to the browser
# Purchase statuses whose money has left, or is leaving, the buyer: they count toward the monthly budget.
SPENT = {"paid", "charging", "refunding"}


class Policy(BaseModel):
    max_per_order: Money | None = Field(description="Refuse any single order above this amount (USD).")
    max_per_month: Money | None = Field(description="Refuse orders that would push this calendar month's spend above this (USD).")
    allowed_categories: list[Category] | None = Field(description="Only these categories may be bought; null means any category.")
    approve_above: Money | None = Field(description="Orders above this amount wait for the user's approval (USD).")


def month_spend(history: list[dict], now: datetime) -> float:
    """What the monthly budget counts: SPENT rows in now's UTC calendar month."""
    return sum(p["amount"] for p in history
               if p["status"] in SPENT and (p["at"].year, p["at"].month) == (now.year, now.month))


def check(policy: dict | None, history: list[dict], purchase: dict, now: datetime | None = None) -> tuple[str, str]:
    """Return (decision, rule): decision is "allow", "approve" or "deny"; rule says which rule fired.

    purchase: {"amount": float, "categories": [str]}
    history:  [{"amount": float, "status": str, "at": datetime}] — only SPENT rows count toward the month.
    """
    # Categories and "ask me" don't bound what an agent can spend; a per-order or monthly cap does.
    if not policy or (policy.get("max_per_order") is None and policy.get("max_per_month") is None):
        return "deny", "no spending limit set (per order or per month)"
    now = now or datetime.now(timezone.utc)
    amount = purchase["amount"]

    allowed = policy.get("allowed_categories")
    if allowed is not None:
        blocked = sorted(set(purchase["categories"]) - set(allowed))
        if blocked:
            return "deny", f"category {', '.join(blocked)} not allowed (allowed: {', '.join(allowed) or 'none'})"

    cap = policy.get("max_per_order")
    if cap is not None and amount > cap:
        return "deny", f"${amount:.2f} is over the ${cap:.2f} per-order limit"

    budget = policy.get("max_per_month")
    if budget is not None:
        spent = month_spend(history, now)
        if round(spent + amount, 2) > budget:
            return "deny", f"would bring this month to ${spent + amount:.2f}, over the ${budget:.2f} monthly budget"

    ask = policy.get("approve_above")
    if ask is not None and amount > ask:
        return "approve", f"${amount:.2f} is over the ${ask:.2f} auto-approve limit"

    return "allow", "within all limits"
