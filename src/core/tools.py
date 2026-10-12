"""Tool registry for runtime agents (Phase 1 of RESEARCH_PROPER_AGENTS.md).

ACI discipline (Anthropic, "Building Effective Agents"): a tool needs a distinct
purpose, a clear when-to-use description, a typed args schema, and documented
failure modes. Bad tool descriptions send agents down wrong paths — the tool
description IS the interface the model sees.

Deliberately framework-free: tools are plain async callables, descriptions are
plain dicts, and `Tool.schema()` renders Gemini's function-declaration shape.
The agent loop in lead_agent.py consumes the registry via dispatch(); the
safety floor stays in code — dispatch never raises, every tool result is a
JSON-serializable dict, and per-tool call caps bound runaway loops. Mutation
caps (50 emails/run, 15 IG DMs/24h, lint-before-send, PII guard) live in the
underlying modules and are NOT bypassable from here by design.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

ToolFn = Callable[..., Awaitable[dict]]

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


@dataclass
class Tool:
    """One agent-callable capability.

    `description` is the ACI: say what it is for, when to use it, what it
    returns, and how it fails. `params` maps arg name ->
    {"type": str, "description": str, "required": bool, "enum": [...]}.
    """

    name: str
    description: str
    params: dict
    fn: ToolFn

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"tool name {self.name!r} must match {_NAME_RE.pattern} (Gemini function-name rules)"
            )

    def schema(self) -> dict:
        """Gemini function declaration shape (google.genai types-compatible dict)."""
        props: dict = {}
        required: list[str] = []
        for pname, spec in self.params.items():
            node = {"type": spec.get("type", "string"), "description": spec.get("description", "")}
            if spec.get("enum"):
                node["enum"] = list(spec["enum"])
            props[pname] = node
            if spec.get("required"):
                required.append(pname)
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": props, "required": required},
        }


class ToolRegistry:
    """Name-keyed tool set with loop guards baked in.

    Guards (per registry instance = per agent run):
    - `max_calls_per_tool`: a tool that keeps failing gets cut off with an
      instruction to stop retrying and answer with what it has.
    - `max_args_chars`: models sometimes try to shove bulk data through tool
      args; reject that early with a readable message.
    `calls` records usage so the run report can show the effort actually spent.
    """

    def __init__(self, *, max_calls_per_tool: int = 6, max_args_chars: int = 4000) -> None:
        self._tools: dict[str, Tool] = {}
        self.calls: dict[str, int] = {}
        self.max_calls_per_tool = max_calls_per_tool
        self.max_args_chars = max_args_chars

    # -- registration ----------------------------------------------------

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    # -- introspection ---------------------------------------------------

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def schemas(self) -> list[dict]:
        """Gemini tool declarations for the whole registry (stable order)."""
        return [self._tools[n].schema() for n in sorted(self._tools)]

    def describe(self) -> str:
        """Compact text rendering for agent prompts / run reports."""
        lines = []
        for n in sorted(self._tools):
            t = self._tools[n]
            args = ", ".join(
                f"{p}{'' if spec.get('required') else '?'}" for p, spec in t.params.items()
            )
            lines.append(f"- {t.name}({args}): {t.description}")
        return "\n".join(lines)

    def calls_made(self) -> int:
        return sum(self.calls.values())

    # -- dispatch ---------------------------------------------------------

    async def dispatch(self, name: str, raw_args: Any) -> dict:
        """Call a tool by name with defensively-parsed args.

        Always returns a dict and never raises — a tool failure is an
        observation for the agent, not a crash:
            {"ok": True,  "result": {...}}   on success
            {"ok": False, "error": "..."}    on any failure (with a fix hint)
        """
        tool = self._tools.get(name)
        if tool is None:
            return {
                "ok": False,
                "error": f"unknown tool '{name}'. Available: {', '.join(self.names()) or '(none)'}",
            }
        if self.calls.get(name, 0) >= self.max_calls_per_tool:
            return {
                "ok": False,
                "error": (
                    f"tool '{name}' reached its {self.max_calls_per_tool}-call budget "
                    "for this run. Stop retrying; answer with what you have."
                ),
            }

        args = self._parse_args(name, raw_args)
        if not args.get("ok", False):
            return args

        missing = [p for p, spec in tool.params.items() if spec.get("required") and p not in args["args"]]
        if missing:
            return {
                "ok": False,
                "error": f"tool '{name}' missing required arg(s): {', '.join(missing)}",
            }

        self.calls[name] = self.calls.get(name, 0) + 1
        try:
            result = await tool.fn(**args["args"])
        except Exception as exc:
            return {"ok": False, "error": f"tool '{name}' failed: {exc!r}"}
        if not isinstance(result, dict):
            result = {"result": result}
        return {"ok": True, "result": result}

    # -- helpers -----------------------------------------------------------

    def _parse_args(self, name: str, raw_args: Any) -> dict:
        """Accept dict, JSON string (bare or fenced), or empty. Never raises."""
        if raw_args is None or raw_args == "":
            return {"ok": True, "args": {}}
        if isinstance(raw_args, dict):
            return {"ok": True, "args": raw_args}
        if isinstance(raw_args, str):
            cleaned = raw_args.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                cleaned = cleaned.removeprefix("json")
                cleaned = cleaned.strip()
            try:
                parsed = json.loads(cleaned) if cleaned else {}
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "error": f"args for '{name}' are not valid JSON ({exc.msg} at char {exc.pos}); "
                    "expected an object like {\"key\": \"value\"}",
                }
            if not isinstance(parsed, dict):
                return {
                    "ok": False,
                    "error": f"args for '{name}' must be a JSON object, got {type(parsed).__name__}",
                }
            if len(json.dumps(parsed)) > self.max_args_chars:
                return {
                    "ok": False,
                    "error": f"args for '{name}' exceed {self.max_args_chars} chars; "
                    "pass ids/names, not bulk data",
                }
            return {"ok": True, "args": parsed}
        return {
            "ok": False,
            "error": f"args for '{name}' must be a JSON object, got {type(raw_args).__name__}",
        }


async def maybe_await(value: Any) -> Any:
    """Await coroutines, pass sync values through — lets tools wrap sync and
    async collaborators without caring which they got."""
    if asyncio.iscoroutine(value):
        return await value
    return value
