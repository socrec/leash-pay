"""Run: uv run python test_policy.py (or pytest)."""
import json
from datetime import datetime
from pathlib import Path
from typing import get_args

from policy import Category, Policy, check

NOW = datetime(2026, 10, 15)
DEMO = {"max_per_order": 100, "max_per_month": 200, "allowed_categories": ["groceries", "books"], "approve_above": 30}


def buy(amount, *categories):
    return {"amount": amount, "categories": list(categories) or ["groceries"]}


def paid(amount, at=NOW, status="paid"):
    return {"amount": amount, "status": status, "at": at}


def test_demo_script():
    assert check(DEMO, [], buy(18), NOW)[0] == "allow"
    assert check(DEMO, [], buy(65, "books"), NOW)[0] == "approve"
    decision, rule = check(DEMO, [], buy(40, "gift_cards"), NOW)
    assert decision == "deny" and "gift_cards" in rule


def test_no_policy_fails_closed():
    assert check(None, [], buy(1), NOW)[0] == "deny"
    assert check({}, [], buy(1), NOW)[0] == "deny"
    assert check(dict.fromkeys(Policy.model_fields), [], buy(1), NOW)[0] == "deny"  # every rule null = no rules


def test_no_spending_cap_fails_closed():
    """Categories and "ask me above" alone don't bound how much an agent spends."""
    assert check({"allowed_categories": ["books"], "approve_above": 30}, [], buy(1, "books"), NOW)[0] == "deny"
    assert check({"max_per_order": 50}, [], buy(1), NOW)[0] == "allow"
    assert check({"max_per_month": 50}, [], buy(1), NOW)[0] == "allow"


def test_limits_must_be_finite():
    """A saved Infinity can't be sent back as JSON, so /api/wallet would fail for good."""
    try:
        Policy(max_per_order=float("inf"), max_per_month=None, allowed_categories=None, approve_above=None)
    except ValueError:  # pydantic's ValidationError
        return
    raise AssertionError("an infinite limit was accepted")


def test_per_order_cap_beats_approval():
    assert check(DEMO, [], buy(100.01), NOW)[0] == "deny"
    assert check(DEMO, [], buy(100), NOW)[0] == "approve"


def test_mixed_cart_denied_if_any_category_blocked():
    assert check(DEMO, [], buy(20, "books", "kitchenware"), NOW)[0] == "deny"


def test_monthly_budget_counts_money_spent_this_month():
    history = [paid(90), paid(90, status="charging"), paid(500, status="refunded"), paid(500, status="pending"),
               paid(500, at=datetime(2026, 9, 30))]
    assert check(DEMO, history, buy(20), NOW)[0] == "allow"   # 180 + 20 = 200, at the cap
    decision, rule = check(DEMO, history, buy(20.01), NOW)
    assert decision == "deny" and "monthly" in rule


def test_monthly_budget_float_sums():
    history = [paid(0.1)] * 3
    assert check({"max_per_month": 0.4}, history, buy(0.1), NOW)[0] == "allow"


def test_null_rules_mean_unlimited():
    unlimited = {"max_per_order": None, "allowed_categories": None, "max_per_month": 20_000}
    assert check(unlimited, [], buy(10_000, "kitchenware"), NOW)[0] == "allow"
    assert check({"allowed_categories": []}, [], buy(1), NOW)[0] == "deny"


def test_deny_rules_name_the_step_for_the_ui():
    """GATE in main.jsx finds the step that stopped a purchase by these phrases."""
    assert "not allowed" in check(DEMO, [], buy(40, "gift_cards"), NOW)[1]
    assert "per-order limit" in check(DEMO, [], buy(101), NOW)[1]
    assert "monthly budget" in check(DEMO, [paid(190)], buy(20), NOW)[1]
    assert "auto-approve limit" in check(DEMO, [], buy(31), NOW)[1]


def test_catalog_categories_are_known_to_policy():
    catalog = json.loads((Path(__file__).parent / "catalog.json").read_text())
    used = {i["category"] for m in catalog["merchants"] for i in m["items"]}
    assert used <= set(get_args(Category))


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} policy tests passed")
