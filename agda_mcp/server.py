#!/usr/bin/env python3
"""Agda interaction MCP server.

Talks directly to `agda --interaction-json` over stdio, exposing
type checking, goal manipulation, proof search, and more.
"""

import asyncio
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agda-lsp")


def _escape(s: str) -> str:
    """Escape a string for embedding in an IOTCM command (Haskell Read syntax)."""
    return (s
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
            .replace("\r", "\\r"))


class AgdaProcess:
    """Manages a persistent `agda --interaction-json` subprocess."""

    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._highlights: dict[str, list[dict]] = {}
        self._goal_types: dict[str, list[dict]] = {}
        self._errors: dict[str, list[str]] = {}
        self._warnings: dict[str, list[str]] = {}
        self._loaded_files: set[str] = set()

    def _reset_state(self):
        self._highlights.clear()
        self._goal_types.clear()
        self._errors.clear()
        self._warnings.clear()
        self._loaded_files.clear()

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        self._reset_state()
        self.process = await asyncio.create_subprocess_exec(
            "agda", "--interaction-json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._read_until_prompt(timeout=30)

    async def _send(self, command: str):
        self.process.stdin.write((command + "\n").encode())
        await self.process.stdin.drain()

    async def _read_until_prompt(self, timeout: int = 120) -> bytes:
        buf = b""
        sentinel = b"JSON> "
        async def _read():
            nonlocal buf
            while True:
                chunk = await self.process.stdout.read(65536)
                if not chunk:
                    raise ConnectionError("Agda process exited unexpectedly")
                buf += chunk
                if buf.endswith(sentinel):
                    return buf
        return await asyncio.wait_for(_read(), timeout=timeout)

    async def _read_responses(self) -> list[dict]:
        raw = await self._read_until_prompt()
        text = raw.decode()
        responses = []
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("JSON>"):
                line = line[len("JSON>"):].strip()
            if not line:
                continue
            try:
                responses.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return responses

    async def _command(self, cmd: str) -> list[dict]:
        await self._send(cmd)
        return await self._read_responses()

    def _iotcm(self, filepath: str, cmd: str) -> str:
        return f'IOTCM "{_escape(filepath)}" NonInteractive Direct ({cmd})'

    # -- Command runner --------------------------------------------------------

    async def _run(self, filepath: str, cmd: str) -> list[dict]:
        """Run an IOTCM command (acquires lock, ensures file is loaded)."""
        async with self._lock:
            await self.start()
            filepath = await self._ensure_loaded_unlocked(filepath)
            return await self._command(self._iotcm(filepath, cmd))

    # -- Response extraction ---------------------------------------------------

    @staticmethod
    def _format_goal_info(goal_info: dict) -> str:
        """Format GoalSpecific goalInfo into readable text."""
        kind = goal_info.get("kind", "")
        if kind in ("InferredType", "NormalForm"):
            return goal_info.get("expr", json.dumps(goal_info))
        if kind == "CurrentGoal":
            return goal_info.get("type", json.dumps(goal_info))
        if kind == "HelperFunction":
            return goal_info.get("signature", json.dumps(goal_info))
        if kind == "GoalType":
            parts = []
            goal_type = goal_info.get("type", "")
            type_aux = goal_info.get("typeAux", {})
            aux_kind = type_aux.get("kind")
            if aux_kind == "GoalAndHave":
                parts.append(f"Goal: {goal_type}")
                parts.append(f"Have: {type_aux.get('expr', '')}")
            elif aux_kind == "GoalAndElaboration":
                parts.append(f"Goal: {goal_type}")
                parts.append(f"Elaboration: {type_aux.get('term', '')}")
            else:
                parts.append(f"Goal: {goal_type}")
            entries = goal_info.get("entries", [])
            if entries:
                parts.append("————————————————————————————")
                for e in entries:
                    name = e.get("reifiedName", e.get("originalName", "?"))
                    binding = e.get("binding", "?")
                    parts.append(f"{name} : {binding}")
            for b in goal_info.get("boundary", []):
                parts.append(f"  {b}")
            for c in goal_info.get("outputForms", []):
                parts.append(f"  {c}")
            return "\n".join(parts)
        return json.dumps(goal_info)

    @staticmethod
    def _format_context(entries: list[dict]) -> str:
        if not entries:
            return "Empty context."
        return "\n".join(
            f"{e.get('reifiedName', e.get('originalName', '?'))} : {e.get('binding', '?')}"
            for e in entries
        )

    @staticmethod
    def _format_named_entries(entries: list) -> str:
        parts = []
        for e in entries:
            if isinstance(e, dict):
                parts.append(f"{e.get('name', '?')} : {e.get('type', '?')}")
            else:
                parts.append(str(e))
        return "\n".join(parts)

    @staticmethod
    def _extract_display(responses: list[dict]) -> str:
        """Extract result from any DisplayInfo response."""
        for r in responses:
            if r.get("kind") == "DisplayInfo":
                info = r.get("info", {})
                kind = info.get("kind", "")
                if kind == "Error":
                    return f"Error: {info.get('error', {}).get('message', json.dumps(info))}"
                if kind == "GoalSpecific":
                    return AgdaProcess._format_goal_info(info.get("goalInfo", {}))
                if kind == "Context":
                    return AgdaProcess._format_context(info.get("context", []))
                if kind == "WhyInScope":
                    return info.get("message", json.dumps(info))
                if kind == "ModuleContents":
                    return AgdaProcess._format_named_entries(info.get("contents", []))
                if kind == "SearchAbout":
                    entries = info.get("results", [])
                    return AgdaProcess._format_named_entries(entries) if entries else "No results found."
                if kind == "Constraints":
                    constraints = info.get("constraints", [])
                    if not constraints:
                        return "No constraints."
                    return "\n".join(c if isinstance(c, str) else json.dumps(c) for c in constraints)
                if kind in ("InferredType", "NormalForm"):
                    return info.get("expr", json.dumps(info))
                if kind == "Auto":
                    return info.get("info", json.dumps(info))
                if kind == "AllGoalsWarnings":
                    parts = []
                    for g in info.get("visibleGoals", []):
                        parts.append(g if isinstance(g, str) else json.dumps(g))
                    for w in info.get("warnings", []):
                        parts.append(f"[Warning] {w}" if isinstance(w, str) else f"[Warning] {json.dumps(w)}")
                    for e in info.get("errors", []):
                        parts.append(f"[Error] {e}" if isinstance(e, str) else f"[Error] {json.dumps(e)}")
                    return "\n".join(parts) if parts else "No goals, warnings, or errors."
                if kind == "IntroNotFound":
                    return "No introduction found."
                if kind == "IntroConstructorUnknown":
                    constructors = info.get("constructors", [])
                    return f"Possible constructors: {', '.join(str(c) for c in constructors)}" if constructors else "Constructor unknown."
                return json.dumps(info)
        return "No result."

    @staticmethod
    def _extract_give(responses: list[dict]) -> str:
        """Extract result from GiveAction, falling back to DisplayInfo."""
        for r in responses:
            if r.get("kind") == "GiveAction":
                result = r.get("giveResult", {})
                if "str" in result:
                    return result["str"]
                return "Accepted."
        return AgdaProcess._extract_display(responses)

    @staticmethod
    def _extract_solve(responses: list[dict]) -> str:
        """Extract results from SolveAll response."""
        for r in responses:
            if r.get("kind") == "SolveAll":
                solutions = r.get("solutions", [])
                if solutions:
                    return "\n".join(
                        f"Goal {s.get('interactionPoint', '?')}: {s.get('expression', '?')}"
                        for s in solutions
                    )
                return "No solutions found."
        return AgdaProcess._extract_display(responses)

    # -- File loading ----------------------------------------------------------

    async def load(self, filepath: str) -> dict:
        async with self._lock:
            await self.start()
            return await self._load_unlocked(filepath)

    async def _load_unlocked(self, filepath: str) -> dict:
        filepath = str(Path(filepath).resolve())
        cmd = self._iotcm(filepath, f'Cmd_load "{_escape(filepath)}" []')
        responses = await self._command(cmd)

        highlights = []
        goal_types = []
        errors = []
        warnings = []

        for r in responses:
            kind = r.get("kind")
            if kind == "HighlightingInfo":
                payload = r.get("info", {}).get("payload", [])
                highlights.extend(payload)
            elif kind == "DisplayInfo":
                info = r.get("info", {})
                if info.get("kind") == "AllGoalsWarnings":
                    goal_types = info.get("visibleGoals", [])
                    for e in info.get("errors", []):
                        errors.append(e if isinstance(e, str) else json.dumps(e))
                    for w in info.get("warnings", []):
                        warnings.append(w if isinstance(w, str) else json.dumps(w))
                elif info.get("kind") == "Error":
                    errors.append(info.get("error", {}).get("message", str(info)))

        self._highlights[filepath] = highlights
        self._goal_types[filepath] = goal_types
        self._errors[filepath] = errors
        self._warnings[filepath] = warnings
        self._loaded_files.add(filepath)

        return {"goals": goal_types, "errors": errors, "warnings": warnings}

    async def _ensure_loaded_unlocked(self, filepath: str) -> str:
        filepath = str(Path(filepath).resolve())
        if filepath not in self._loaded_files:
            await self._load_unlocked(filepath)
        return filepath

    # -- Offset conversion -----------------------------------------------------

    @staticmethod
    def _pos_to_offset(filepath: str, line: int, col: int) -> int:
        raw = Path(filepath).read_bytes()
        offset = 1
        for i, file_line in enumerate(raw.split(b"\n"), 1):
            if i == line:
                return offset + col - 1
            offset += len(file_line) + 1
        return offset

    @staticmethod
    def _offset_to_line_col(filepath: str, offset: int) -> tuple[int, int]:
        raw = Path(filepath).read_bytes()
        pos = 1
        for i, file_line in enumerate(raw.split(b"\n"), 1):
            line_end = pos + len(file_line)
            if offset <= line_end:
                return i, offset - pos + 1
            pos = line_end + 1
        return 1, 1

    def find_highlight_at(self, filepath: str, offset: int) -> dict | None:
        for h in self._highlights.get(filepath, []):
            rng = h.get("range", [])
            if len(rng) == 2 and rng[0] <= offset < rng[1]:
                return h
        return None

    # -- Give-like commands ----------------------------------------------------

    async def give(self, filepath: str, goal_id: int, expr: str) -> str:
        return self._extract_give(await self._run(
            filepath, f'Cmd_give WithoutForce {goal_id} noRange "{_escape(expr)}"'))

    async def elaborate_give(self, filepath: str, goal_id: int, expr: str) -> str:
        return self._extract_give(await self._run(
            filepath, f'Cmd_elaborate_give Simplified {goal_id} noRange "{_escape(expr)}"'))

    async def refine(self, filepath: str, goal_id: int, expr: str = "") -> str:
        return self._extract_give(await self._run(
            filepath, f'Cmd_refine {goal_id} noRange "{_escape(expr)}"'))

    async def intro(self, filepath: str, goal_id: int) -> str:
        return self._extract_give(await self._run(
            filepath, f'Cmd_intro False {goal_id} noRange ""'))

    async def refine_or_intro(self, filepath: str, goal_id: int, expr: str = "") -> str:
        return self._extract_give(await self._run(
            filepath, f'Cmd_refine_or_intro False {goal_id} noRange "{_escape(expr)}"'))

    # -- Goal info commands ----------------------------------------------------

    async def goal_type(self, filepath: str, goal_id: int) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_goal_type Simplified {goal_id} noRange ""'))

    async def context(self, filepath: str, goal_id: int) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_context Simplified {goal_id} noRange ""'))

    async def goal_type_and_context(self, filepath: str, goal_id: int) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_goal_type_context Simplified {goal_id} noRange ""'))

    async def goal_type_context_infer(self, filepath: str, goal_id: int, expr: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_goal_type_context_infer Simplified {goal_id} noRange "{_escape(expr)}"'))

    async def goal_type_context_check(self, filepath: str, goal_id: int, expr: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_goal_type_context_check Simplified {goal_id} noRange "{_escape(expr)}"'))

    async def infer_in_goal(self, filepath: str, goal_id: int, expr: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_infer Simplified {goal_id} noRange "{_escape(expr)}"'))

    async def compute_in_goal(self, filepath: str, goal_id: int, expr: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_compute DefaultCompute {goal_id} noRange "{_escape(expr)}"'))

    async def helper_function(self, filepath: str, goal_id: int, expr: str = "") -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_helper_function Simplified {goal_id} noRange "{_escape(expr)}"'))

    # -- Case split ------------------------------------------------------------

    async def case_split(self, filepath: str, goal_id: int, variable: str) -> list[str]:
        responses = await self._run(filepath, f'Cmd_make_case {goal_id} noRange "{_escape(variable)}"')
        for r in responses:
            if r.get("kind") == "MakeCase":
                return r.get("clauses", [])
        fallback = self._extract_display(responses)
        return [fallback] if fallback != "No result." else ["No result."]

    # -- Auto/solve ------------------------------------------------------------

    async def auto(self, filepath: str, goal_id: int) -> str:
        return self._extract_give(await self._run(
            filepath, f'Cmd_autoOne Simplified {goal_id} noRange ""'))

    async def auto_all(self, filepath: str) -> str:
        responses = await self._run(filepath, 'Cmd_autoAll Simplified')
        results = []
        for r in responses:
            if r.get("kind") == "GiveAction":
                ip = r.get("interactionPoint", {})
                goal_id = ip.get("id", ip) if isinstance(ip, dict) else ip
                result = r.get("giveResult", {})
                expr = result.get("str", "accepted")
                results.append(f"Goal {goal_id}: {expr}")
        return "\n".join(results) if results else self._extract_display(responses)

    async def solve_one(self, filepath: str, goal_id: int) -> str:
        return self._extract_solve(await self._run(
            filepath, f'Cmd_solveOne Simplified {goal_id} noRange ""'))

    async def solve_all(self, filepath: str) -> str:
        return self._extract_solve(await self._run(filepath, 'Cmd_solveAll Simplified'))

    # -- Scope/module at goal --------------------------------------------------

    async def why_in_scope_goal(self, filepath: str, goal_id: int, name: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_why_in_scope {goal_id} noRange "{_escape(name)}"'))

    async def module_contents_goal(self, filepath: str, goal_id: int, module_name: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_show_module_contents Simplified {goal_id} noRange "{_escape(module_name)}"'))

    # -- Toplevel commands -----------------------------------------------------

    async def infer(self, filepath: str, expr: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_infer_toplevel Simplified "{_escape(expr)}"'))

    async def compute(self, filepath: str, expr: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_compute_toplevel DefaultCompute "{_escape(expr)}"'))

    async def why_in_scope(self, filepath: str, name: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_why_in_scope_toplevel "{_escape(name)}"'))

    async def constraints(self, filepath: str) -> str:
        return self._extract_display(await self._run(filepath, 'Cmd_constraints'))

    async def metas(self, filepath: str) -> str:
        return self._extract_display(await self._run(filepath, 'Cmd_metas Simplified'))

    async def search_about(self, filepath: str, query: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_search_about_toplevel Simplified "{_escape(query)}"'))

    async def module_contents_toplevel(self, filepath: str, module_name: str) -> str:
        return self._extract_display(await self._run(
            filepath, f'Cmd_show_module_contents_toplevel Simplified "{_escape(module_name)}"'))


agda = AgdaProcess()


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def agda_load(file_path: str) -> str:
    """Load and type-check an Agda file. Returns goals, errors, and warnings.

    This must be called before other commands. Call again after edits to refresh.

    Args:
        file_path: Absolute path to the .agda file
    """
    result = await agda.load(file_path)
    lines = []
    if result["errors"]:
        for e in result["errors"]:
            lines.append(f"[Error] {e}")
    if result["warnings"]:
        for w in result["warnings"]:
            lines.append(f"[Warning] {w}")
    if result["goals"]:
        for g in result["goals"]:
            constraint = g.get("constraintObj", {})
            rng = constraint.get("range", [{}])[0]
            start = rng.get("start", {})
            gtype = g.get("type", "?")
            gkind = g.get("kind", "")
            lines.append(
                f"[Goal {constraint.get('id', '?')}] "
                f"line {start.get('line', '?')}:{start.get('col', '?')} "
                f"— {gkind}: {gtype}"
            )
    if not lines:
        lines.append("Checked. No errors, warnings, or goals.")
    return "\n".join(lines)


@mcp.tool()
async def agda_hover(file_path: str, line: int, character: int) -> str:
    """Get type/definition info for a symbol at a position.

    Args:
        file_path: Absolute path to the .agda file
        line: Line number (1-based)
        character: Column number (1-based)
    """
    filepath = str(Path(file_path).resolve())
    if filepath not in agda._loaded_files:
        await agda.load(filepath)
    offset = agda._pos_to_offset(filepath, line, character)
    h = agda.find_highlight_at(filepath, offset)
    if not h:
        return "No information at this position."
    parts = []
    atoms = h.get("atoms", [])
    if atoms:
        parts.append(f"Kind: {', '.join(atoms)}")
    site = h.get("definitionSite")
    if site:
        def_path = site.get("filepath", "")
        def_pos = site.get("position", 0)
        def_line, def_col = agda._offset_to_line_col(def_path, def_pos)
        parts.append(f"Defined at: {def_path}:{def_line}:{def_col}")
    note = h.get("note", "")
    if note:
        parts.append(f"Note: {note}")
    return "\n".join(parts) if parts else "No information at this position."


@mcp.tool()
async def agda_definition(file_path: str, line: int, character: int) -> str:
    """Go to the definition of a symbol at a position.

    Args:
        file_path: Absolute path to the .agda file
        line: Line number (1-based)
        character: Column number (1-based)
    """
    filepath = str(Path(file_path).resolve())
    if filepath not in agda._loaded_files:
        await agda.load(filepath)
    offset = agda._pos_to_offset(filepath, line, character)
    h = agda.find_highlight_at(filepath, offset)
    if not h:
        return "No symbol at this position."
    site = h.get("definitionSite")
    if not site:
        return "No definition site available."
    def_path = site.get("filepath", "")
    def_pos = site.get("position", 0)
    def_line, def_col = agda._offset_to_line_col(def_path, def_pos)
    return f"{def_path}:{def_line}:{def_col}"


@mcp.tool()
async def agda_infer(file_path: str, expr: str) -> str:
    """Infer the type of an expression in the context of a loaded file.

    Args:
        file_path: Absolute path to the .agda file (for scope)
        expr: The expression to type-check (e.g. "add", "suc zero")
    """
    return await agda.infer(file_path, expr)


@mcp.tool()
async def agda_compute(file_path: str, expr: str) -> str:
    """Normalize (evaluate) an expression.

    Args:
        file_path: Absolute path to the .agda file (for scope)
        expr: The expression to evaluate (e.g. "add 2 3")
    """
    return await agda.compute(file_path, expr)


@mcp.tool()
async def agda_case_split(file_path: str, goal_id: int, variable: str) -> str:
    """Case split on a variable in a goal. Returns the new clauses.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        variable: The variable name to split on
    """
    clauses = await agda.case_split(file_path, goal_id, variable)
    return "\n".join(clauses)


@mcp.tool()
async def agda_goal_info(file_path: str, goal_id: int) -> str:
    """Get the type and context of a specific goal/hole.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
    """
    return await agda.goal_type_and_context(file_path, goal_id)


@mcp.tool()
async def agda_auto(file_path: str, goal_id: int) -> str:
    """Try to automatically solve a goal.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
    """
    return await agda.auto(file_path, goal_id)


@mcp.tool()
async def agda_why_in_scope(file_path: str, name: str) -> str:
    """Explain where a name is brought into scope.

    Args:
        file_path: Absolute path to the .agda file
        name: The name to look up (e.g. "ℕ", "suc", "add")
    """
    return await agda.why_in_scope(file_path, name)


# -- Give-like commands --------------------------------------------------------

@mcp.tool()
async def agda_give(file_path: str, goal_id: int, expr: str) -> str:
    """Fill a goal/hole with a complete solution.

    Agda will check the expression and, if correct, replace the hole in the
    source file. Call agda_load afterwards to refresh goals.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: The expression to fill the hole with
    """
    return await agda.give(file_path, goal_id, expr)


@mcp.tool()
async def agda_elaborate_give(file_path: str, goal_id: int, expr: str) -> str:
    """Fill a goal/hole with an elaborated solution.

    Like agda_give, but returns the fully elaborated (normalized) expression.
    Call agda_load afterwards to refresh goals.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: The expression to fill the hole with
    """
    return await agda.elaborate_give(file_path, goal_id, expr)


@mcp.tool()
async def agda_refine(file_path: str, goal_id: int, expr: str = "") -> str:
    """Refine a goal by filling it with an expression that may create new subgoals.

    If expr is empty, Agda tries to refine using the goal type.
    Call agda_load afterwards to refresh goals.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: Expression to refine with (can be empty)
    """
    return await agda.refine(file_path, goal_id, expr)


@mcp.tool()
async def agda_intro(file_path: str, goal_id: int) -> str:
    """Introduce a constructor or lambda abstraction in a goal.

    Tries to fill the hole with an appropriate constructor or lambda.
    Call agda_load afterwards to refresh goals.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
    """
    return await agda.intro(file_path, goal_id)


@mcp.tool()
async def agda_refine_or_intro(file_path: str, goal_id: int, expr: str = "") -> str:
    """Refine or introduce in a goal (auto-chooses the best action).

    Combines refine and intro: tries to refine with the expression if given,
    otherwise introduces a constructor or lambda.
    Call agda_load afterwards to refresh goals.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: Expression to refine with (can be empty for intro)
    """
    return await agda.refine_or_intro(file_path, goal_id, expr)


# -- Goal info commands --------------------------------------------------------

@mcp.tool()
async def agda_goal_type(file_path: str, goal_id: int) -> str:
    """Get the type of a specific goal/hole (without context).

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
    """
    return await agda.goal_type(file_path, goal_id)


@mcp.tool()
async def agda_context(file_path: str, goal_id: int) -> str:
    """Get the context (available bindings) at a specific goal/hole.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
    """
    return await agda.context(file_path, goal_id)


@mcp.tool()
async def agda_goal_type_context_infer(file_path: str, goal_id: int, expr: str) -> str:
    """Get goal type, context, and the inferred type of an expression.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: Expression whose type to infer in this goal's context
    """
    return await agda.goal_type_context_infer(file_path, goal_id, expr)


@mcp.tool()
async def agda_goal_type_context_check(file_path: str, goal_id: int, expr: str) -> str:
    """Get goal type, context, and check an expression against the goal type.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: Expression to check against the goal type
    """
    return await agda.goal_type_context_check(file_path, goal_id, expr)


@mcp.tool()
async def agda_infer_in_goal(file_path: str, goal_id: int, expr: str) -> str:
    """Infer the type of an expression in the context of a specific goal.

    Unlike agda_infer which works at top level, this infers within a goal's
    local context (with access to locally-bound variables).

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: The expression to type-check
    """
    return await agda.infer_in_goal(file_path, goal_id, expr)


@mcp.tool()
async def agda_compute_in_goal(file_path: str, goal_id: int, expr: str) -> str:
    """Normalize (evaluate) an expression in the context of a specific goal.

    Unlike agda_compute which works at top level, this evaluates within a
    goal's local context.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: The expression to evaluate
    """
    return await agda.compute_in_goal(file_path, goal_id, expr)


@mcp.tool()
async def agda_helper_function(file_path: str, goal_id: int, expr: str = "") -> str:
    """Generate a helper function type signature for a goal.

    The expression should be a partial application like "h x y" where h is the
    helper name and x, y are arguments. Agda will generate the type signature
    for h. If empty, uses whatever is in the goal.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        expr: Partial application (e.g. "helper x y")
    """
    return await agda.helper_function(file_path, goal_id, expr)


@mcp.tool()
async def agda_why_in_scope_goal(file_path: str, goal_id: int, name: str) -> str:
    """Explain where a name is brought into scope, in the context of a goal.

    Like agda_why_in_scope but with access to the goal's local scope.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        name: The name to look up
    """
    return await agda.why_in_scope_goal(file_path, goal_id, name)


@mcp.tool()
async def agda_module_contents_goal(file_path: str, goal_id: int, module_name: str) -> str:
    """List the contents of a module, in the context of a goal.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
        module_name: The module or record name to inspect
    """
    return await agda.module_contents_goal(file_path, goal_id, module_name)


@mcp.tool()
async def agda_solve_one(file_path: str, goal_id: int) -> str:
    """Solve a single goal if its solution has been determined by unification.

    Only works if the goal has been fully instantiated (e.g. by constraints).
    Call agda_load afterwards to refresh goals.

    Args:
        file_path: Absolute path to the .agda file
        goal_id: The goal/hole number (from agda_load output)
    """
    return await agda.solve_one(file_path, goal_id)


# -- Toplevel commands ---------------------------------------------------------

@mcp.tool()
async def agda_solve_all(file_path: str) -> str:
    """Solve all goals whose solutions have been determined by unification.

    Call agda_load afterwards to refresh goals.

    Args:
        file_path: Absolute path to the .agda file
    """
    return await agda.solve_all(file_path)


@mcp.tool()
async def agda_auto_all(file_path: str) -> str:
    """Try to automatically solve all visible goals.

    Call agda_load afterwards to refresh goals.

    Args:
        file_path: Absolute path to the .agda file
    """
    return await agda.auto_all(file_path)


@mcp.tool()
async def agda_constraints(file_path: str) -> str:
    """Show all current unsolved constraints.

    Useful for debugging type errors or understanding what Agda is stuck on.

    Args:
        file_path: Absolute path to the .agda file
    """
    return await agda.constraints(file_path)


@mcp.tool()
async def agda_metas(file_path: str) -> str:
    """Show all open meta-variables (goals) and their types.

    Args:
        file_path: Absolute path to the .agda file
    """
    return await agda.metas(file_path)


@mcp.tool()
async def agda_search_about(file_path: str, query: str) -> str:
    """Search for definitions whose type mentions the given names.

    Useful for finding relevant lemmas, functions, or constructors.

    Args:
        file_path: Absolute path to the .agda file
        query: Space-separated names to search for in types
    """
    return await agda.search_about(file_path, query)


@mcp.tool()
async def agda_module_contents(file_path: str, module_name: str) -> str:
    """List the top-level names exported by a module.

    Args:
        file_path: Absolute path to the .agda file
        module_name: The module name to inspect (e.g. "Data.Nat", "Data.List")
    """
    return await agda.module_contents_toplevel(file_path, module_name)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
