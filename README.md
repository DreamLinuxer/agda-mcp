# agda-mcp

MCP server for [Agda](https://agda.readthedocs.io/), providing type checking, go-to-definition, case splitting, auto proof search, and more — directly from Claude Code or any MCP client.

Talks to `agda --interaction-json` (the same interface powering agda-mode in Emacs/VS Code), so it has access to all of Agda's interactive features without needing `agda-language-server`.

## Installation

### Prerequisites

Make sure you have these installed:

- [Agda](https://agda.readthedocs.io/en/latest/getting-started/installation.html) (tested with 2.8.0) — `agda` must be on your `PATH`
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (for running the MCP server)

### Claude Code (quickstart)

```bash
claude mcp add agda-lsp -- uvx agda-mcp
```

That's it. Restart Claude Code and the tools are available.

### Other MCP clients

Add to your client's MCP config:

```json
{
  "mcpServers": {
    "agda-lsp": {
      "command": "uvx",
      "args": ["agda-mcp"],
      "type": "stdio"
    }
  }
}
```

For Claude Code, this goes in `~/.claude.json` under `mcpServers`. For other clients, see your client's documentation for where to add MCP server configs.

### From source

```bash
git clone https://github.com/chaohong/agda-mcp.git
cd agda-mcp
uv sync
uv run agda-mcp
```

## Tools

| Tool | Description |
|------|-------------|
| `agda_load` | Load/type-check a file, get goals + errors + warnings |
| `agda_hover` | Symbol kind + definition site at a position |
| `agda_definition` | Go to definition (file:line:col) |
| `agda_infer` | Infer the type of an expression |
| `agda_compute` | Normalize/evaluate an expression |
| `agda_case_split` | Case split on a variable in a goal |
| `agda_auto` | Try to automatically solve a goal |
| `agda_goal_info` | Get the type and context of a goal |
| `agda_why_in_scope` | Explain where a name comes from |

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

## License

MIT
