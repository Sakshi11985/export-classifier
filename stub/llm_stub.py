"""LLM stub (D6): an OpenAI-compatible /v1/chat/completions server that returns FIXED responses after
a configurable delay, so load tests measure this system and not a provider's rate limits.

The classifier script is stateless: the step is derived from how many tool results are already in the
conversation, so any number of concurrent runs can be served. Responses are fixed for the section 4.1
scenario (vacuum flask); the stub does not read the product description.

Control endpoints (for the circuit-breaker demonstration):
  POST /admin/mode  {"mode": "ok" | "error" | "slow", "delay_s": 1.0}
  GET  /admin/mode
Environment: STUB_DELAY_S (default 1.0), STUB_PORT (8080)."""
from __future__ import annotations

import asyncio
import json
import os
import random

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="llm-stub")
STATE = {"mode": "ok", "delay_s": float(os.environ.get("STUB_DELAY_S", "1.0")), "calls": 0}

FINAL = {
    "article": "vacuum flask", "materials": ["stainless steel", "plastic"], "heading": "9617", "gri_applied": "GRI-1",
    "reasoning_summary": "Heading 9617 names vacuum flasks, so GRI 1 applies before classification by material; "
                         "Section XV Note 1 excludes articles of Chapter 96 from Chapter 73.",
    "citations": [
        {"id": "GRI-1", "quote": "classification shall be determined according to the terms of the headings and any relative Section or Chapter Notes"},
        {"id": "HEADING-9617", "quote": "Vacuum flasks and other vacuum vessels, complete with cases; parts thereof other than glass inners."},
        {"id": "SEC-XV-NOTE-1", "quote": "other articles of Chapter 96 (miscellaneous manufactured articles)"}],
    "alternatives": [{"heading": "7323", "status": "excluded_by_note", "basis": "Section XV Note 1(m)", "note_id": "SEC-XV-NOTE-1"}],
    "needs_clarification": [], "out_of_scope": False, "tie": False, "confidence": "high",
}


def _tool(i, name, args):
    return {"id": f"call_stub_{i}_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def classifier_script(n_tool_results: int) -> dict:
    if n_tool_results == 0:
        calls = [_tool(0, "get_request_fields", {}),
                 _tool(1, "search_corpus", {"query": "stainless steel vacuum flask plastic body", "top_k": 5}),
                 _tool(2, "search_headings_by_article", {"article": "vacuum flask"})]
    elif n_tool_results == 3:
        calls = [_tool(3, "get_notes_for_heading", {"heading": "9617"}), _tool(4, "get_notes_for_heading", {"heading": "7323"}),
                 _tool(5, "search_corpus", {"query": "vacuum flask", "top_k": 4, "chapter": 96})]
    elif n_tool_results == 6:
        calls = [_tool(6, "get_clause", {"id": "GRI-1"})]
    else:
        return {"content": json.dumps(FINAL)}
    return {"content": None, "tool_calls": calls}


@app.get("/health")
async def health():
    return {"status": "ok", **STATE}


@app.get("/admin/mode")
async def get_mode():
    return STATE


@app.post("/admin/mode")
async def set_mode(request: Request):
    body = await request.json()
    if body.get("mode") in ("ok", "error", "slow"):
        STATE["mode"] = body["mode"]
    if "delay_s" in body:
        STATE["delay_s"] = float(body["delay_s"])
    return STATE


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    STATE["calls"] += 1
    mode = STATE["mode"]
    if mode == "error":
        return JSONResponse({"error": {"message": "stub configured to fail"}}, status_code=503)
    delay = STATE["delay_s"] * (0.8 + 0.4 * random.random())
    if mode == "slow":
        delay = STATE["delay_s"] * 20
    await asyncio.sleep(delay)
    messages = body.get("messages", [])
    system = (messages[0]["content"] if messages else "")[:200]
    is_verifier = "VERIFIER" in system
    if is_verifier:
        msg = {"role": "assistant", "content": json.dumps({"supported": True, "issues": []})}
    else:
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        out = classifier_script(n_tool)
        msg = {"role": "assistant", "content": out["content"]}
        if out.get("tool_calls"):
            msg["tool_calls"] = out["tool_calls"]
    text = json.dumps(messages)
    pt = max(1, len(text) // 4)
    ct = max(1, len(json.dumps(msg)) // 4)
    return {"id": "stub", "object": "chat.completion", "model": body.get("model", "stub"),
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("STUB_PORT", "8080")), log_level="warning")
