"""The shopping agent: an LLM with two tools. It has no PayPal access; request_purchase goes through the gate."""
import json
from typing import TypedDict

import gate
import llm

SYSTEM = """You are LeashPay's shopping agent. You buy things for the user from the demo store.
Search the catalog first, then call request_purchase with exact SKUs, quantities and the catalog total.
Every purchase is checked against the user's spending rules: it is paid automatically, sent to the user
for approval, or denied. Report the outcome plainly, including the rule when one fired. Never try to get
around a rule (splitting orders, changing amounts or items); explain it and let the user decide.
Earlier turns are shown as text only. Your requests in them did happen: the bracketed gate results after each
earlier reply are their real outcomes and current status, so never redo or disown them.
Keep replies to two or three sentences of plain text without Markdown: the chat shows replies as-is."""

ITEMS = [{**item, "merchant": m["name"]} for m in gate.CATALOG["merchants"] for item in m["items"]]


class Item(TypedDict):
    sku: str
    qty: int


def search_catalog(query: str) -> str:
    """Search the store. Returns matching items as JSON (sku, name, price in USD, category, merchant).

    Args:
        query: Keywords, e.g. "coffee beans" or "cookbook". Empty returns everything.
    """
    words = query.lower().split()
    hits = [i for i in ITEMS if any(w in f"{i['name']} {i['merchant']} {i['category']}".lower() for w in words)]
    return json.dumps(hits or ITEMS)


def run(message: str, history: list[dict]) -> dict:
    made = []

    def request_purchase(merchant: str, items: list[Item], amount: float, reason: str) -> str:
        """Ask to buy items from one merchant. The user's spending policy decides: paid, pending approval, or denied.

        Args:
            merchant: Merchant name from the catalog, e.g. "BeanBox Coffee".
            items: SKUs and quantities to buy, all from that merchant.
            amount: Order total in USD, as computed from catalog prices.
            reason: One sentence for the user explaining why you are buying this.
        """
        p = gate.request_purchase(merchant, list(items), amount, reason)
        made.append(p)
        return json.dumps({k: p[k] for k in ("id", "amount", "decision", "rule", "status")})

    messages = [*history, {"role": "user", "content": message}]
    return {"reply": llm.run_tools(SYSTEM, messages, [search_catalog, request_purchase]), "purchases": made}
