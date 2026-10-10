"""Thin PayPal REST client (sandbox by default). Only gate.py may call charge()."""
import os
import time
import uuid

import httpx

BASE = os.environ.get("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
_token = {"value": "", "expires": 0.0}


class PayPalError(Exception):
    pass


def token() -> str:
    if time.time() > _token["expires"] - 60:
        creds = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET")
        if not all(creds):
            raise PayPalError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are not set")
        try:
            r = httpx.post(f"{BASE}/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=creds, timeout=30)
        except httpx.HTTPError as e:  # nothing has been charged yet
            raise PayPalError(f"OAuth: {e!r}") from e
        if r.is_error:
            raise PayPalError(f"OAuth {r.status_code}: {r.text}")
        body = r.json()
        _token.update(value=body["access_token"], expires=time.time() + body["expires_in"])
    return _token["value"]


def call(method: str, path: str, body: dict | None = None, request_id: str | None = None) -> dict:
    """PayPalError means PayPal did not act. Any other error (a timeout, a 5xx) means it may have."""
    try:
        r = httpx.request(method, BASE + path, json=body, timeout=60, headers={
            "Authorization": f"Bearer {token()}",
            "PayPal-Request-Id": request_id or str(uuid.uuid4()),  # idempotency key
            "Prefer": "return=representation",
        })
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # the request never reached PayPal
        raise PayPalError(f"{method} {path}: {e!r}") from e
    if r.is_client_error:
        raise PayPalError(f"{method} {path} → {r.status_code}: {r.text}")
    r.raise_for_status()
    return r.json() if r.content else {}


def create_setup_token(return_url: str, cancel_url: str) -> tuple[str, str]:
    """Start saving the buyer's PayPal account (no purchase). Returns (setup_token_id, approve_url)."""
    st = call("POST", "/v3/vault/setup-tokens", {"payment_source": {"paypal": {
        "description": "LeashPay: lets your shopping agent pay within your rules",
        "usage_type": "MERCHANT",
        "customer_type": "CONSUMER",
        "permit_multiple_payment_tokens": True,
        "experience_context": {
            "brand_name": "LeashPay",
            "shipping_preference": "NO_SHIPPING",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }}})
    return st["id"], next(link["href"] for link in st["links"] if link["rel"] == "approve")


def create_payment_token(setup_token_id: str) -> tuple[str, str | None]:
    """Exchange an approved setup token for a vault_id. Returns (vault_id, payer_email)."""
    pt = call("POST", "/v3/vault/payment-tokens",
              {"payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}})
    return pt["id"], pt.get("payment_source", {}).get("paypal", {}).get("email_address")


def charge(vault_id: str, amount: float, description: str, custom_id: str, request_id: str) -> tuple[str, str, str]:
    """Charge the vaulted account with no buyer present. Returns (order_id, capture_id, capture_status)."""
    order = call("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
                            "description": description[:127], "custom_id": custom_id}],
        "payment_source": {"paypal": {"vault_id": vault_id}},
    }, request_id)
    if order["status"] != "COMPLETED":  # vaulted orders usually complete on create; capture if not
        order = call("POST", f"/v2/checkout/orders/{order['id']}/capture", {}, request_id + "-capture")
    capture = order["purchase_units"][0]["payments"]["captures"][0]
    return order["id"], capture["id"], capture["status"]


def refund(capture_id: str, request_id: str) -> dict:
    return call("POST", f"/v2/payments/captures/{capture_id}/refund", {}, request_id)


def verify_webhook(headers, event: dict) -> bool:
    try:
        body = {k: headers[f"paypal-{k.replace('_', '-')}"]
                for k in ("auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time")}
    except KeyError:
        return False
    if not os.environ.get("PAYPAL_WEBHOOK_ID"):
        raise PayPalError("PAYPAL_WEBHOOK_ID is not set")
    body.update(webhook_id=os.environ["PAYPAL_WEBHOOK_ID"], webhook_event=event)
    return call("POST", "/v1/notifications/verify-webhook-signature", body).get("verification_status") == "SUCCESS"
