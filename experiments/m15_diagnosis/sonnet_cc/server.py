"""Stdio MCP server that hands one M15 task's tools to Claude Code (subscription Sonnet run).

Launched by `claude -p` through --mcp-config (see run.py), one process per task. Environment:
M15_TASK_ID, M15_MANIFEST, M15_FRAMING (a | bd), M15_OUT (episode JSON written here).

The tools are the agent's own: the same names, descriptions and JSON schemas (tool_specs()),
the same 8-call budget, run_tool at the task's as_of on a read-only backfill connection, the
same reframing (in_frame), the same task-facts receipt, and the same submit_answer handling
(_finish: strict benchmark verifier, summary unchecked). Only the agent loop is Claude Code's.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

from gridcase import config
from gridcase.rl.agent import (
    MAX_TOOL_CALLS,
    TOOL_BY_API,
    Episode,
    _content,
    _finish,
    _second_outcome,
    facts_receipt,
    frame_for,
    in_frame,
    tool_specs,
)
from gridcase.rl.tasks import load_manifest
from gridcase.store.db import connect
from gridcase.tools.base import ToolError, run_tool

TASK_ID = os.environ["M15_TASK_ID"]
TASK = next(t for t in load_manifest(Path(os.environ["M15_MANIFEST"])) if t.task_id == TASK_ID)
OUT = Path(os.environ["M15_OUT"])
CONN = connect(config.BACKFILL_DB, read_only=True)
FRAME = frame_for(TASK, os.environ.get("M15_FRAMING", "bd"))
if FRAME.flip:
    FRAME = replace(FRAME, outcome=_second_outcome(CONN, TASK))
FACTS = facts_receipt(TASK, FRAME)
RECEIPTS = {FACTS.receipt_id: FACTS}
EP = Episode(task_id=TASK.task_id, model="claude-code:sonnet")
LOG: list[dict[str, Any]] = []
SUBMITTED = False


def _save() -> None:
    OUT.write_text(
        json.dumps(
            {
                "task_id": EP.task_id,
                "submitted": SUBMITTED,
                "probability": EP.probability,
                "summary": EP.summary,
                "claims": [c.model_dump() for c in EP.claims],
                "dropped": EP.dropped,
                "tool_calls": EP.tool_calls,
                "budget_exceeded": EP.budget_exceeded,
                "malformed": EP.malformed,
                "transcript": LOG,
            },  # fmt: skip
            default=str,
        )
    )


def _text(s: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=s)], is_error=error)


async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(name=s["name"], description=s["description"], input_schema=s["input_schema"])
            for s in tool_specs()
        ]
    )


async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    global SUBMITTED
    name, args = params.name, dict(params.arguments or {})
    if SUBMITTED:
        return _text("The answer is already submitted; stop.", error=True)
    if name == "submit_answer":
        SUBMITTED = True
        _finish(EP, args, RECEIPTS)
        LOG.append({"role": "assistant", "submit": args})
        _save()
        return _text("Answer recorded. Stop now.")
    if EP.tool_calls >= MAX_TOOL_CALLS:
        EP.budget_exceeded = True
        content = json.dumps({"error": "tool budget used up; call submit_answer"})
    elif name not in TOOL_BY_API:
        content = json.dumps({"error": f"unknown tool {name}"})
    else:
        EP.tool_calls += 1
        try:
            r = in_frame(run_tool(CONN, TOOL_BY_API[name], args, TASK.as_of), TASK, FRAME)
            RECEIPTS[r.receipt_id] = r
            content = _content(r)
        except ToolError as e:
            content = json.dumps({"error": str(e)})
    LOG.append({"role": "tool", "calls": [[name, args]], "results": [["", content]]})
    _save()
    return _text(content)


async def main() -> None:
    server: Server[Any] = Server("m15", on_list_tools=list_tools, on_call_tool=call_tool)
    _save()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
