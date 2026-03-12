#!/usr/bin/env python3
"""Agda interaction MCP server.

Talks directly to `agda --interaction-json` over stdio, exposing
load, hover, go-to-definition, case split, auto, compute, and more.
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
        """Clear all cached state (used on process restart)."""
        self._highlights.clear()
        self._goal_types.clear()
        self._errors.clear()
        self._warnings.clear()
        self._loaded_files.clear()

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        # Process died or never started — reset cached state
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
        """Read stdout until we see 'JSON> ' (the Agda prompt).

        The prompt appears without a trailing newline, so we read in
        chunks and check if the buffer ends with the sentinel.
        """
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
        """Read all JSON responses until the next JSON> prompt."""
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

    # -- Lock-holding commands (public API) ------------------------------------
    # These acquire self._lock. Internal helpers called within must NOT
    # re-acquire it (use _load_unlocked / _ensure_loaded_unlocked instead).

    async def load(self, filepath: str) -> dict:
        """Load and type-check a file. Returns goals, errors, warnings."""
        async with self._lock:
            await self.start()
            return await self._load_unlocked(filepath)

    async def _load_unlocked(self, filepath: str) -> dict:
        """Load implementation — caller must hold self._lock."""
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
                    errors.append(info.get("message", str(info)))

        self._highlights[filepath] = highlights
        self._goal_types[filepath] = goal_types
        self._errors[filepath] = errors
        self._warnings[filepath] = warnings
        self._loaded_files.add(filepath)

        return {
            "goals": goal_types,
            "errors": errors,
            "warnings": warnings,
        }

    async def _ensure_loaded_unlocked(self, filepath: str) -> str:
        """Ensure a file is loaded — caller must hold self._lock."""
        filepath = str(Path(filepath).resolve())
        if filepath not in self._loaded_files:
            await self._load_unlocked(filepath)
        return filepath

    # -- Offset conversion -----------------------------------------------------
    # Agda uses 1-based byte offsets in UTF-8 encoded source files.

    @staticmethod
    def _pos_to_offset(filepath: str, line: int, col: int) -> int:
        """Convert 1-based line:col to 1-based UTF-8 byte offset."""
        raw = Path(filepath).read_bytes()
        offset = 1
        for i, file_line in enumerate(raw.split(b"\n"), 1):
            if i == line:
                return offset + col - 1
            offset += len(file_line) + 1  # +1 for newline
        return offset

    @staticmethod
    def _offset_to_line_col(filepath: str, offset: int) -> tuple[int, int]:
        """Convert 1-based UTF-8 byte offset to 1-based line:col."""
        raw = Path(filepath).read_bytes()
        pos = 1
        for i, file_line in enumerate(raw.split(b"\n"), 1):
            line_end = pos + len(file_line)
            if offset <= line_end:
                return i, offset - pos + 1
            pos = line_end + 1
        return 1, 1

    def find_highlight_at(self, filepath: str, offset: int) -> dict | None:
        """Find the highlight entry covering the given offset."""
        for h in self._highlights.get(filepath, []):
            rng = h.get("range", [])
            if len(rng) == 2 and rng[0] <= offset < rng[1]:
                return h
        return None

    # -- Commands that need a loaded file --------------------------------------

    async def infer(self, filepath: str, expr: str) -> str:
        async with self._lock:
            await self.start()
            filepath = await self._ensure_loaded_unlocked(filepath)
            cmd = self._iotcm(filepath, f'Cmd_infer_toplevel Simplified "{_escape(expr)}"')
            responses = await self._command(cmd)
            for r in responses:
                if r.get("kind") == "DisplayInfo":
                    info = r["info"]
                    if info.get("kind") == "InferredType":
                        return info.get("expr", "")
                    if info.get("kind") == "Error":
                        return f"Error: {info.get('message', '')}"
            return "No result."

    async def compute(self, filepath: str, expr: str) -> str:
        async with self._lock:
            await self.start()
            filepath = await self._ensure_loaded_unlocked(filepath)
            cmd = self._iotcm(filepath, f'Cmd_compute_toplevel DefaultCompute "{_escape(expr)}"')
            responses = await self._command(cmd)
            for r in responses:
                if r.get("kind") == "DisplayInfo":
                    info = r["info"]
                    if info.get("kind") == "NormalForm":
                        return info.get("expr", "")
                    if info.get("kind") == "Error":
                        return f"Error: {info.get('message', '')}"
            return "No result."

    async def why_in_scope(self, filepath: str, name: str) -> str:
        async with self._lock:
            await self.start()
            filepath = await self._ensure_loaded_unlocked(filepath)
            cmd = self._iotcm(filepath, f'Cmd_why_in_scope_toplevel "{_escape(name)}"')
            responses = await self._command(cmd)
            for r in responses:
                if r.get("kind") == "DisplayInfo":
                    info = r["info"]
                    if info.get("kind") == "WhyInScope":
                        return info.get("message", "")
                    if info.get("kind") == "Error":
                        return f"Error: {info.get('message', '')}"
            return "No result."

    async def case_split(self, filepath: str, goal_id: int, variable: str) -> list[str]:
        async with self._lock:
            await self.start()
            filepath = await self._ensure_loaded_unlocked(filepath)
            cmd = self._iotcm(filepath, f'Cmd_make_case {goal_id} noRange "{_escape(variable)}"')
            responses = await self._command(cmd)
            for r in responses:
                if r.get("kind") == "MakeCase":
                    return r.get("clauses", [])
                if r.get("kind") == "DisplayInfo":
                    info = r["info"]
                    if info.get("kind") == "Error":
                        return [f"Error: {info.get('message', '')}"]
            return ["No result."]

    async def goal_type_and_context(self, filepath: str, goal_id: int) -> str:
        async with self._lock:
            await self.start()
            filepath = await self._ensure_loaded_unlocked(filepath)
            cmd = self._iotcm(filepath, f'Cmd_goal_type_context Simplified {goal_id} noRange ""')
            responses = await self._command(cmd)
            for r in responses:
                if r.get("kind") == "DisplayInfo":
                    info = r["info"]
                    if info.get("kind") == "GoalSpecific":
                        return info.get("goalInfo", {}).get("payload", str(info))
                    if info.get("kind") == "Error":
                        return f"Error: {info.get('message', '')}"
            return "No result."

    async def auto(self, filepath: str, goal_id: int) -> str:
        async with self._lock:
            await self.start()
            filepath = await self._ensure_loaded_unlocked(filepath)
            cmd = self._iotcm(filepath, f'Cmd_autoOne Simplified {goal_id} noRange ""')
            responses = await self._command(cmd)
            for r in responses:
                if r.get("kind") == "GiveAction":
                    return r.get("giveResult", {}).get("str", str(r))
                if r.get("kind") == "DisplayInfo":
                    info = r["info"]
                    if info.get("kind") == "Error":
                        return f"Error: {info.get('message', '')}"
            return "No result."


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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
