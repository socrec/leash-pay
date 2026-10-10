import base64
import logging
import os
import secrets
from pathlib import Path
from typing import Literal

import anthropic
import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, SQLModel, select

import agent
import gate
import llm
import paypal
from db import Purchase, Wallet, engine
from policy import Policy

SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    if not s.get(Wallet, 1):
        # DEMO_VAULT_ID re-attaches a saved sandbox account after the free host wipes SQLite.
        s.add(Wallet(id=1, vault_id=os.environ.get("DEMO_VAULT_ID") or None))
        s.commit()

app = FastAPI(title="LeashPay")
# Optional. Set, the browser asks for it once (any username) and resends it. Unset, anyone who can reach the app can use it.
PASSWORD = os.environ.get("APP_PASSWORD")
WEBHOOK = "/api/webhooks/paypal"


def _authorized(header: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    try:
        password = base64.b64decode(encoded, validate=True).partition(b":")[2]
    except ValueError:
        return False
    return scheme.lower() == "basic" and secrets.compare_digest(password, PASSWORD.encode())


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path != WEBHOOK:  # PayPal can't log in; the webhook is checked by its signature instead
        if PASSWORD and not _authorized(request.headers.get("authorization", "")):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="LeashPay"'})
        # Any site can send a form post or a no-cors fetch here (CSRF), but not with a custom header: that needs a CORS
        # preflight, which this app never grants. api() in lib.jsx sends it.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-leashpay") != "1":
            return JSONResponse({"detail": "missing X-LeashPay header"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"  # no clicking Approve through another site's invisible frame
    return response


@app.exception_handler(paypal.PayPalError)
@app.exception_handler(httpx.HTTPError)
@app.exception_handler(anthropic.AnthropicError)
def upstream_error(_, e: Exception):
    return JSONResponse({"detail": str(e)}, status_code=502)


class Text(BaseModel):
    text: str = Field(max_length=4000)  # caps what one request can cost in model tokens


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class Chat(Text):
    # Earlier turns, sent by the browser because the model API is stateless. The user can rewrite them; that's harmless
    # because the gate re-prices and re-checks every purchase.
    history: list[Turn] = Field(default=[], max_length=20)


class StudioTurn(BaseModel):
    # One model turn for AG Studio's dashboard agents (the adapter in AuditLog.jsx). Studio runs the tools the model
    # asks for in the page, on dashboard state; none of them can reach the gate, so nothing sent here can move money.
    # ponytail: caps item and tool counts, not bytes; add a request-size cap if this ever serves strangers.
    input: list[dict] = Field(max_length=400)
    instructions: str = Field("", max_length=200_000)
    tools: list[dict] = Field([], max_length=64)
    toolChoice: str | dict | None = None


@app.get("/api/wallet")
def get_wallet():
    with Session(engine) as s:
        w = s.get(Wallet, 1)
        return {"connected": bool(w.vault_id), "payer_email": w.payer_email, "policy": w.policy,
                "spent": gate.spent_this_month()}


@app.get("/api/catalog")
def get_catalog():
    return gate.CATALOG


@app.post("/api/wallet/connect")
def connect_wallet(request: Request):
    base = str(request.base_url)
    token_id, approve_url = paypal.create_setup_token(base + "api/wallet/return", base)
    with Session(engine) as s:
        w = s.get(Wallet, 1)
        w.setup_token = token_id
        s.add(w)
        s.commit()
    return {"approve_url": approve_url}


@app.get("/api/wallet/return")
def wallet_return():
    """PayPal redirects here once the buyer approves saving their account."""
    with Session(engine) as s:
        w = s.get(Wallet, 1)
        if not w.setup_token:
            raise HTTPException(400, "no PayPal connection in progress")
        w.vault_id, w.payer_email = paypal.create_payment_token(w.setup_token)
        w.setup_token = None
        s.add(w)
        s.commit()
        if "sandbox" in paypal.BASE:  # for DEMO_VAULT_ID; a live vault id stays out of the logs
            logging.getLogger("uvicorn.error").info("Saved PayPal vault_id %s", w.vault_id)
    return RedirectResponse("/#/wallet")


@app.post("/api/policy/parse")
def parse_policy(body: Text) -> Policy:
    try:
        return llm.parse_policy(body.text)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.put("/api/policy")
def save_policy(policy: Policy):
    with Session(engine) as s:
        w = s.get(Wallet, 1)
        w.policy = policy.model_dump()
        s.add(w)
        s.commit()
        return w.policy


@app.post("/api/agent")
def run_agent(body: Chat):
    return agent.run(body.text, [t.model_dump() for t in body.history])


@app.post("/api/studio/turn")
def studio_turn(body: StudioTurn):
    return llm.studio_turn(body.model_dump())


@app.get("/api/purchases")
def list_purchases():
    with Session(engine) as s:
        return s.exec(select(Purchase).order_by(Purchase.id.desc())).all()


@app.post("/api/purchases/{pid}/{action}")
def act(pid: int, action: Literal["approve", "reject", "refund"]):
    return getattr(gate, action)(pid)


@app.post(WEBHOOK)
def paypal_webhook(request: Request, event: dict = Body()):
    if not paypal.verify_webhook(request.headers, event):
        raise HTTPException(400, "webhook signature check failed")
    gate.apply_webhook(event)
    return {"ok": True}


dist = Path(__file__).parent.parent / "frontend" / "dist"
if dist.is_dir():
    app.mount("/", StaticFiles(directory=dist, html=True))
