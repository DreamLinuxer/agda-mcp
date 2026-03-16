# agda-mcp

MCP server for [Agda](https://agda.readthedocs.io/), providing type checking, go-to-definition, case splitting, auto proof search, and more — directly from Claude Code or any MCP client.

Talks to `agda --interaction-json` (the same interface powering agda-mode in Emacs/VS Code), so it has access to all of Agda's interactive features without needing `agda-language-server`.

## Installation

### Prerequisites

- [Agda](https://agda.readthedocs.io/en/latest/getting-started/installation.html) (tested with 2.8.0) — `agda` must be on your `PATH`
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

### 1. Clone and build

```bash
git clone https://github.com/DreamLinuxer/agda-mcp.git
cd agda-mcp
uv sync
```

### 2. Add to your MCP client

**Claude Code** (globally, for all projects):

```bash
claude mcp add --scope user agda-lsp -- uv run --directory /path/to/agda-mcp agda-mcp
```

**Other MCP clients** — add to your client's MCP config:

```json
{
  "mcpServers": {
    "agda-lsp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/agda-mcp", "agda-mcp"],
      "type": "stdio"
    }
  }
}
```

Replace `/path/to/agda-mcp` with the absolute path where you cloned the repo.

## Tools

### File & navigation

| Tool | Description |
|------|-------------|
| `agda_load` | Load/type-check a file, get goals + errors + warnings |
| `agda_hover` | Symbol kind + definition site at a position |
| `agda_definition` | Go to definition (file:line:col) |

### Goal manipulation (modify source)

| Tool | Description |
|------|-------------|
| `agda_give` | Fill a hole with a complete solution |
| `agda_elaborate_give` | Fill a hole with an elaborated solution |
| `agda_refine` | Refine a goal (may create new subgoals) |
| `agda_intro` | Introduce a constructor or lambda |
| `agda_refine_or_intro` | Auto-choose between refine and intro |
| `agda_case_split` | Case split on a variable in a goal |
| `agda_auto` | Try to automatically solve a goal |
| `agda_solve_one` | Solve a goal if determined by unification |

### Goal inspection

| Tool | Description |
|------|-------------|
| `agda_goal_info` | Get the type and context of a goal |
| `agda_goal_type` | Get just the type of a goal |
| `agda_context` | Get the context (bindings) at a goal |
| `agda_goal_type_context_infer` | Goal type + context + inferred type of expr |
| `agda_goal_type_context_check` | Goal type + context + check expr against type |
| `agda_infer_in_goal` | Infer type of an expression at a goal |
| `agda_compute_in_goal` | Normalize an expression at a goal |
| `agda_helper_function` | Generate helper function type for a goal |
| `agda_why_in_scope_goal` | Explain where a name comes from (at a goal) |
| `agda_module_contents_goal` | List module contents (at a goal) |

### Toplevel commands

| Tool | Description |
|------|-------------|
| `agda_infer` | Infer the type of an expression |
| `agda_compute` | Normalize/evaluate an expression |
| `agda_why_in_scope` | Explain where a name comes from |
| `agda_constraints` | Show all unsolved constraints |
| `agda_metas` | Show all open goals/meta-variables |
| `agda_search_about` | Search for definitions mentioning given names |
| `agda_module_contents` | List names exported by a module |
| `agda_solve_all` | Solve all goals determined by unification |
| `agda_auto_all` | Try to automatically solve all goals |

## Usage examples

### Load and type-check a file

```
agda_load("/path/to/file.agda")
→ Checked. No errors, warnings, or goals.
```

### Infer a type

```
agda_infer("/path/to/file.agda", "map")
→ {a b : Set} {n : ℕ} → (a → b) → Vec a n → Vec b n
```

### Evaluate an expression

```
agda_compute("/path/to/file.agda", "2 + 3")
→ 5
```

### Case split

```
agda_case_split("/path/to/file.agda", 0, "xs")
→ f [] = ?
  f (x ∷ xs) = ?
```

### Auto-solve a goal

```
agda_auto("/path/to/file.agda", 0)
→ a , b
```

## How it works

The server spawns a single persistent `agda --interaction-json` process and communicates via the IOTCM protocol over stdio. File state (highlighting data for go-to-definition, goal types, diagnostics) is cached per file and refreshed on each `agda_load` call.