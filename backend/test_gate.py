"""Gate tests with PayPal stubbed out. Run: uv run python test_gate.py (or pytest)."""
import json
import os
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

DB = f"{tempfile.mkdtemp()}/test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

import httpx  # noqa: E402
from anthropic.types.beta import BetaMessage  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

import agent  # noqa: E402
import app  # noqa: E402
import gate  # noqa: E402
import llm  # noqa: E402
import paypal  # noqa: E402
from db import Purchase, Wallet, engine, now  # noqa: E402

DEMO = {"max_per_order": 100, "max_per_month": 200, "allowed_categories": ["groceries", "books"], "approve_above": 30}
APP = {"X-LeashPay": "1"}  # what api() in lib.jsx sends
local = TestClient(app.app, base_url="http://localhost")
charges = []
refunds = []
stub = {}


def db_unlocked():
    """The PayPal stubs call this: a write transaction held across a PayPal call blocks every other writer."""
    con = sqlite3.connect(DB, timeout=0, isolation_level=None)
    con.execute("BEGIN IMMEDIATE")  # raises "database is locked" if another connection is mid-write
    con.close()


def fake_charge(vault_id, amount, *_):
    time.sleep(stub["delay"])
    db_unlocked()
    charges.append(amount)
    return f"ORDER{len(charges)}", f"CAP{len(charges)}", stub["capture"]


def fake_refund(capture_id, request_id):
    db_unlocked()
    refunds.append(request_id)
    if stub["refund"] is None:
        raise httpx.ReadTimeout("no reply")
    return {"id": "R1", "status": stub["refund"]}


real_charge, paypal.charge, paypal.refund = paypal.charge, fake_charge, fake_refund
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.merge(Wallet(id=1, vault_id="VAULT"))  # importing app already made wallet 1
    s.commit()


def fresh(policy=DEMO):
    """Start a test with no purchases, the given policy and PayPal answering COMPLETED instantly."""
    charges.clear()
    refunds.clear()
    stub.update(capture="COMPLETED", refund="COMPLETED", delay=0)
    with Session(engine) as s:
        s.exec(delete(Purchase))
        w = s.get(Wallet, 1)
        w.policy = policy
        s.add(w)
        s.commit()


def buy(sku, amount, qty=1, merchant="BeanBox Coffee"):
    return gate.request_purchase(merchant, [{"sku": sku, "qty": qty}], amount, "test")


def status(pid):
    with Session(engine) as s:
        return s.get(Purchase, pid).status


def webhook(kind, capture_id):
    gate.apply_webhook({"event_type": f"PAYMENT.CAPTURE.{kind}", "resource": {"id": capture_id}})


def raises_409(fn, *args):
    try:
        fn(*args)
    except HTTPException as e:
        return e.status_code == 409
    return False


def test_demo_flow():
    fresh()
    coffee = buy("BB-HOUSE", 18)
    assert (coffee["decision"], coffee["status"], coffee["capture_id"]) == ("allow", "paid", "CAP1")

    books = buy("PT-COOKSET", 65, merchant="PageTurn Books")
    assert (books["decision"], books["status"]) == ("approve", "pending")
    assert gate.approve(books["id"])["status"] == "paid"  # "failed" if approve() holds the DB lock while paying
    assert raises_409(gate.approve, books["id"])  # double tap doesn't charge twice

    gift = buy("BB-GIFT40", 40)
    assert (gift["decision"], gift["status"]) == ("deny", "denied")
    assert charges == [18, 65]

    assert gate.refund(coffee["id"])["status"] == "refunded"
    assert raises_409(gate.refund, coffee["id"])


def test_agent_cannot_lie_or_invent():
    fresh()
    assert "catalog total" in buy("BB-HOUSE", 1.00)["rule"]          # understated price
    assert "catalog total" in buy("BB-HOUSE", float("nan"))["rule"]  # NaN fails every comparison
    assert "not sold" in buy("PT-HARBOR", 14)["rule"]                # wrong merchant for the sku
    assert "quantity" in buy("BB-HOUSE", -18, qty=-1)["rule"]
    assert "quantity" in buy("BB-HOUSE", 18, qty=True)["rule"]
    assert "twice" in gate.request_purchase("BeanBox Coffee", [{"sku": "BB-HOUSE", "qty": 20}] * 2, 720, "t")["rule"]
    assert buy("BB-HOUSE", 18, merchant="Evil Corp")["decision"] == "deny"
    assert charges == []


def test_reject_and_recheck_at_approval():
    fresh()
    p = buy("BB-SAMPLER", 48)
    assert gate.reject(p["id"])["status"] == "rejected"
    assert raises_409(gate.approve, p["id"])

    p = buy("BB-SAMPLER", 48)  # pending; then the month fills up before the user taps approve
    for _ in range(9):  # 9 × 18 = 162 paid; +48 > 200
        buy("BB-HOUSE", 18)
    assert gate.approve(p["id"])["status"] == "denied"


def test_budget_counts_charges_in_flight():
    fresh({"max_per_order": None, "max_per_month": 30, "allowed_categories": None, "approve_above": None})
    stub["delay"] = 0.2  # the second request is checked while the first charge is still at PayPal
    check = gate.policy.check
    gate.policy.check = lambda *a: time.sleep(0.1) or check(*a)  # and both checks overlap unless gate locks
    try:
        with ThreadPoolExecutor(2) as pool:
            statuses = sorted(p["status"] for p in pool.map(lambda _: buy("BB-HOUSE", 18), range(2)))
    finally:
        gate.policy.check = check
    assert statuses == ["denied", "paid"]


def test_spent_this_month_counts_money_in_flight():
    fresh()
    stub["capture"] = "PENDING"
    buy("BB-HOUSE", 18)  # charging
    stub["capture"] = "COMPLETED"
    buy("BB-ETHIOPIA", 22)  # paid
    buy("BB-SAMPLER", 48)  # pending approval: not spent yet
    assert gate.spent_this_month() == 40


def test_paypal_pending_capture_is_not_an_approval():
    fresh()
    stub["capture"] = "PENDING"
    p = buy("BB-HOUSE", 18)
    assert p["status"] == "charging"  # our "pending" means "waiting for the user" and shows in the inbox
    webhook("COMPLETED", p["capture_id"])
    assert status(p["id"]) == "paid"
    stub["capture"] = "DECLINED"
    assert buy("BB-HOUSE", 18)["status"] == "failed"


def test_webhooks_only_move_forward():
    fresh()
    p = buy("BB-DECAF", 19)
    webhook("DENIED", p["capture_id"])
    assert status(p["id"]) == "paid"
    stub["refund"] = "PENDING"
    assert gate.refund(p["id"])["status"] == "refunding"
    webhook("COMPLETED", p["capture_id"])  # late duplicate
    assert status(p["id"]) == "refunding"
    gate.apply_webhook({"event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {  # resource is the refund
        "id": "R9", "links": [{"rel": "up", "href": f"https://api/v2/payments/captures/{p['capture_id']}"}]}})
    assert status(p["id"]) == "refunded"


def test_declined_and_reversed_captures():
    fresh()
    stub["capture"] = "PENDING"
    p = buy("BB-HOUSE", 18)
    webhook("DECLINED", p["capture_id"])
    assert status(p["id"]) == "failed"  # instead of "charging" forever
    stub["capture"] = "COMPLETED"
    p = buy("BB-DECAF", 19)
    gate.apply_webhook({"event_type": "PAYMENT.CAPTURE.REVERSED", "resource": {  # resource is the refund
        "id": "R9", "links": [{"rel": "up", "href": f"https://api/v2/payments/captures/{p['capture_id']}"}]}})
    assert status(p["id"]) == "refunded"


def test_unclear_charge_outcome_still_counts():
    """A timeout or a PayPal 5xx may hide a completed charge: keep counting it until a webhook settles it."""
    fresh()
    paypal._token.update(value="test", expires=time.time() + 3600)
    send, paypal.charge = httpx.request, real_charge
    try:
        for answer, expected in ((httpx.ReadTimeout("no reply"), "charging"), (503, "charging"),
                                 (httpx.ConnectError("no route"), "failed"), (422, "failed")):
            def reply(method, url, **_):
                db_unlocked()
                if isinstance(answer, Exception):
                    raise answer
                return httpx.Response(answer, request=httpx.Request(method, url))
            httpx.request = reply
            assert buy("BB-HOUSE", 18)["status"] == expected, answer
    finally:
        httpx.request, paypal.charge = send, fake_charge


def test_approval_expires_with_its_month():
    """The budget counts a purchase in the month it was requested, so approving it later would count it nowhere."""
    fresh()
    p = buy("PT-COOKSET", 65, merchant="PageTurn Books")
    with Session(engine) as s:
        row = s.get(Purchase, p["id"])
        row.at = now().replace(day=1) - timedelta(days=1)
        s.add(row)
        s.commit()
    assert gate.approve(p["id"])["status"] == "denied"
    assert charges == []


def test_agent_amount_is_never_stored():
    """JSON 1e400 parses to inf; stored on a denied row it made /api/purchases fail for good."""
    fresh()
    p = gate.request_purchase("Nowhere", [], float("inf"), "x")
    assert (p["decision"], p["amount"]) == ("deny", 0)
    assert local.get("/api/purchases").status_code == 200


def test_refund_retry_reuses_its_request_id():
    """With the same PayPal-Request-Id, a retry after a timeout gets the first refund back instead of a second one."""
    fresh()
    p = buy("BB-HOUSE", 18)
    stub["refund"] = None  # the first try times out
    try:
        gate.refund(p["id"])
    except HTTPException:
        pass
    stub["refund"] = "COMPLETED"
    assert gate.refund(p["id"])["status"] == "refunded"
    assert len(refunds) == 2 and refunds[0] == refunds[1]


def test_other_sites_cannot_act_for_you():
    """A form post or no-cors fetch from another site carries no custom header (CSRF)."""
    fresh()
    p = buy("PT-COOKSET", 65, merchant="PageTurn Books")
    assert local.post(f"/api/purchases/{p['id']}/approve").status_code == 403
    assert status(p["id"]) == "pending"
    assert local.post(f"/api/purchases/{p['id']}/approve", headers=APP).status_code == 200
    assert local.get("/api/wallet").headers["x-frame-options"] == "DENY"  # no framing the Approve button


def test_password_guards_everything_but_the_webhook():
    fresh()
    remote = TestClient(app.app, base_url="https://leashpay.example")
    app.PASSWORD = "s3cret"
    try:
        assert remote.get("/api/wallet").status_code == 401
        assert remote.get("/api/wallet", auth=("any", "wrong")).status_code == 401
        assert remote.get("/api/wallet", auth=("any", "s3cret")).status_code == 200
        assert remote.post("/api/webhooks/paypal", json={}).status_code == 400  # PayPal can't log in: signature check
    finally:
        app.PASSWORD = None
    assert local.post("/api/agent", json={"text": "x" * 5000}, headers=APP).status_code == 422  # caps the model bill


def test_agent_sends_chat_history():
    """The model API is stateless: a follow-up like "the BeanBox one" only makes sense with the earlier turns."""
    sent = []
    llm.run_tools = lambda system, messages, tools: sent.append(messages) or "ok"
    history = [{"role": "user", "content": "Buy a $40 gift card"},
               {"role": "assistant", "content": "BeanBox or PageTurn?"}]
    agent.run("The BeanBox one", history)
    assert sent == [[*history, {"role": "user", "content": "The BeanBox one"}]]


def test_studio_turn_speaks_claude():
    """AG Studio's agents talk OpenAI-style items; Claude needs its signed thinking back, no prefill, no forced tool."""
    sent = []
    think = lambda sig: {"type": "thinking", "thinking": "", "signature": sig}  # noqa: E731
    say = lambda role, t: {"type": "message", "role": role, "content": [{"type": "text", "text": t}]}  # noqa: E731
    reply = {"id": "msg_2", "type": "message", "role": "assistant", "model": llm.MODEL, "stop_reason": "tool_use",
             "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
             "content": [think("SIG2"), {"type": "text", "text": "Adding it."},
                         {"type": "tool_use", "id": "toolu_2", "name": "add_widget", "input": {"type": "donut-chart"}}]}
    create = lambda **kw: sent.append(kw) or BetaMessage.model_validate(reply)  # noqa: E731
    llm.client = lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    turn = {"instructions": "You build dashboards.", "toolChoice": {"name": "complete_task"},
            "tools": [{"name": "view_schema", "description": "Tables and fields", "parameters": {"type": "object"}}],
            "input": [say("system", "UTC"), say("user", "Chart"),
                      {"type": "reasoning", "summary": [], "block": think("SIG1")},
                      {"type": "function_call", "callId": "toolu_1", "name": "view_schema", "arguments": "{}"},
                      {"type": "function_call_output", "callId": "toolu_1", "output": "purchases"},
                      say("assistant", "Done.")]}  # a delegate ending on text: Studio retries, forcing a tool
    r = local.post("/api/studio/turn", json=turn, headers=APP)
    assert r.status_code == 200
    kw = sent[0]
    assert kw["system"] == "You build dashboards.\n\nUTC" and "tool_choice" not in kw
    assert kw["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert kw["tools"][0]["input_schema"] == {"type": "object"}
    assert [m["role"] for m in kw["messages"]] == ["user", "assistant", "user", "assistant", "user"]
    assert kw["messages"][1]["content"] == [think("SIG1"),
                                            {"type": "tool_use", "id": "toolu_1", "name": "view_schema", "input": {}}]
    assert kw["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "purchases"}
    assert kw["messages"][-1]["content"] == [{"type": "text", "text": "Call the complete_task tool now."}]
    out = r.json()["output"]
    assert [o["type"] for o in out] == ["reasoning", "message", "function_call"] and r.json()["status"] == "completed"
    assert out[0]["block"] == think("SIG2") and out[2]["callId"] == "toolu_2"
    assert json.loads(out[2]["arguments"]) == {"type": "donut-chart"}

if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} gate tests passed")
