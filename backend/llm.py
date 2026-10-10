"""Every model call lives here. Swap the model with LLM_MODEL; swap the provider by rewriting the functions below."""
import json
import os
import time
from functools import cache

import anthropic
from anthropic import beta_tool

from policy import Policy

MODEL = os.environ.get("LLM_MODEL", "claude-opus-5-5")
# On a safety refusal, the API re-runs the request on Anthropic's recommended fallback model.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

POLICY_PROMPT = """Turn the user's plain-English spending rules for their shopping agent into a policy.
- max_per_order: hard limit; orders above it are refused.
- max_per_month: calendar-month budget; orders that would exceed it are refused.
- approve_above: orders above this wait for the user's approval ("ask me above $X").
- allowed_categories: map what the user names onto the available categories (coffee, food -> groceries;
  books, novels -> books; gift cards -> gift_cards; kitchen gear -> kitchenware). "X only" lists just those.
Use null for any rule the user did not state. Amounts are USD."""


@cache
def client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def run_tools(system: str, messages: list[dict], tools: list) -> str:
    """Run a tool-using agent to completion on a text-only chat; tools are plain Python functions with docstrings."""
    runner = client().beta.messages.tool_runner(
        model=MODEL, max_tokens=16000, max_iterations=10, system=system,
        tools=[beta_tool(t) for t in tools],
        messages=messages,
        output_config={"effort": "medium"}, **FALLBACK)
    last = None
    for last in runner:
        pass
    if last is None or last.stop_reason == "refusal":
        return "Sorry, I can't help with that request."
    text = "\n".join(b.text for b in last.content if b.type == "text")
    return text or "I stopped before finishing. Any purchase attempts are listed below."  # ran out of iterations


def parse_policy(text: str) -> Policy:
    resp = client().beta.messages.parse(
        model=MODEL, max_tokens=16000, system=POLICY_PROMPT,
        messages=[{"role": "user", "content": text}],
        output_format=Policy, output_config={"effort": "low"}, **FALLBACK)
    policy = resp.parsed_output
    # policy.check() denies everything without a per-order or monthly cap, so say so before the user saves one
    if resp.stop_reason == "refusal" or policy is None or (policy.max_per_order is None and policy.max_per_month is None):
        raise ValueError("I need a spending limit; try e.g. \"max $50 per order\" or \"$200 a month\"")
    return policy


# Studio rebuilds its instructions and tool schemas every turn, which invalidates earlier thinking blocks: drop those
# instead of failing the turn with a 400.
STUDIO_BETAS = [*FALLBACK["betas"], "thinking-binding-controls-2026-08-01"]


def _to_claude(items: list[dict]) -> tuple[str, list[dict]]:
    """AG Studio's conversation items (OpenAI Responses-style) → extra system text and alternating Claude messages."""
    system, messages = [], []

    def add(role: str, block: dict):
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": role, "content": [block]})

    for it in items:
        kind = it.get("type")
        if kind == "message":
            texts = [c["text"] for c in it.get("content", []) if c.get("type") == "text" and c.get("text")]
            if it.get("role") == "system":
                system += texts
            else:
                for text in texts:
                    add("assistant" if it.get("role") == "assistant" else "user", {"type": "text", "text": text})
        elif kind == "reasoning" and it.get("block"):
            add("assistant", it["block"])  # signed thinking: Claude needs it back exactly as it wrote it
        elif kind == "function_call":
            add("assistant", {"type": "tool_use", "id": it["callId"], "name": it["name"],
                              "input": json.loads(it.get("arguments") or "{}")})
        elif kind == "function_call_output":
            add("user", {"type": "tool_result", "tool_use_id": it["callId"], "content": it.get("output", "")})
    return "\n\n".join(system), messages


def _from_claude(resp) -> dict:
    """A Claude message → the AgLlmResponse AG Studio expects. Thinking travels as an opaque reasoning item."""
    output = []
    for b in resp.content:
        item = {"id": f"{resp.id}-{len(output)}", "kind": "output"}
        if b.type == "text":
            output.append({**item, "type": "message", "role": "assistant", "status": "completed",
                           "content": [{"type": "text", "text": b.text, "annotations": []}]})
        elif b.type == "tool_use":
            output.append({**item, "type": "function_call", "callId": b.id, "name": b.name,
                           "arguments": json.dumps(b.input), "status": "completed"})
        else:  # thinking (and any newer block type) goes back to Claude untouched on the next turn
            output.append({**item, "type": "reasoning", "summary": [],
                           "block": b.model_dump(mode="json", exclude_none=True)})
    status = {"refusal": "failed", "max_tokens": "incomplete"}.get(resp.stop_reason, "completed")
    out = {"id": resp.id, "createdAt": int(time.time() * 1000), "status": status, "output": output, "model": resp.model,
           "usage": {"inputTokens": resp.usage.input_tokens, "outputTokens": resp.usage.output_tokens}}
    if status == "failed":
        out["error"] = {"code": "refusal", "message": "Claude declined this request."}
    return out


def studio_turn(req: dict) -> dict:
    """One turn for AG Studio's dashboard agents. Studio runs the loop and the tools in the browser; this only talks."""
    extra, messages = _to_claude(req["input"])
    if not messages or messages[-1]["role"] == "assistant":  # Claude takes no prefill and no forced tool: ask instead
        tool = req.get("toolChoice")
        nudge = f"Call the {tool['name']} tool now." if isinstance(tool, dict) and tool.get("name") else "Continue."
        messages.append({"role": "user", "content": [{"type": "text", "text": nudge}]})
    resp = client().beta.messages.create(
        model=MODEL, max_tokens=16000, system="\n\n".join(filter(None, [req.get("instructions"), extra])),
        messages=messages,
        tools=[{"name": t["name"], "description": t.get("description", ""), "input_schema": t["parameters"]}
               for t in req.get("tools") or []],
        thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        output_config={"effort": "low"},  # one dashboard build is many turns across Studio's agents
        betas=STUDIO_BETAS, fallbacks=FALLBACK["fallbacks"])
    return _from_claude(resp)
