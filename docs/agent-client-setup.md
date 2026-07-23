# Connect pg_play to agent clients

`pg_play` exposes a local stdio MCP server and five optional Agent Skills. The
MCP server provides the tools; the skills teach an agent the safe workflow for
planning, starting, monitoring, recovering, and interpreting experiments.

## Prerequisites

Install `pg-play` and resolve the server to an absolute path:

```bash
python -m pip install pg-play
command -v pg-play-mcp
```

In every example below, replace `/absolute/path/to/pg-play-mcp` with that path.
Pass the executable directly, without `bash -lc` or another shell wrapper.

For a source checkout, the skills are in `src/pg_play/skills/`. For an installed
package, run this with the same Python environment that contains `pg-play`:

```bash
python -c 'from importlib.resources import files; print(files("pg_play").joinpath("skills"))'
```

## Codex CLI

Official documentation: [MCP](https://developers.openai.com/codex/mcp/) ·
[Skills](https://developers.openai.com/codex/skills/)

```bash
codex mcp add pg_play -- /absolute/path/to/pg-play-mcp
codex mcp list
```

Copy the skill directories to `~/.agents/skills/` for user scope or
`.agents/skills/` for repository scope. Use `/mcp` and `/skills` in Codex to
verify discovery.

## Claude Code

Official documentation: [MCP](https://code.claude.com/docs/en/mcp) ·
[Skills](https://code.claude.com/docs/en/slash-commands)

```bash
claude mcp add --transport stdio --scope user pg_play -- \
  /absolute/path/to/pg-play-mcp
claude mcp list
```

Use `--scope project` instead of `--scope user` to create a repository-owned
`.mcp.json`. Copy skills to `~/.claude/skills/` or `.claude/skills/`. Use
`/mcp` to verify the server; skills can be invoked as `/run-postgres-experiment`
and by their other directory names.

## Hermes Agent

Official documentation: [MCP](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md) ·
[Skills](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md)

Add the server to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  pg_play:
    command: "/absolute/path/to/pg-play-mcp"
    args: []
    enabled: true
```

Copy skills to `~/.hermes/skills/`, then restart `hermes chat`. Keep Hermes'
default sequential MCP execution; do not enable parallel tool calls for this
stateful orchestrator.

## Kimi Code CLI

Official documentation: [MCP](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/mcp.html) ·
[Skills](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/skills.html)

Create `~/.kimi-code/mcp.json`, or `.kimi-code/mcp.json` for one repository:

```json
{
  "mcpServers": {
    "pg_play": {
      "command": "/absolute/path/to/pg-play-mcp",
      "args": [],
      "startupTimeoutMs": 20000,
      "toolTimeoutMs": 120000
    }
  }
}
```

Copy skills to `~/.agents/skills/`, `~/.kimi-code/skills/`, or their project
equivalents. Use `/mcp` to verify the server and `/skill:<name>` to invoke a
skill explicitly.

## Gemini CLI

Official documentation: [MCP](https://geminicli.com/docs/cli/tutorials/mcp-setup/) ·
[Skills](https://geminicli.com/docs/cli/using-agent-skills/)

Add this to `~/.gemini/settings.json`, or `.gemini/settings.json` in a project:

```json
{
  "mcpServers": {
    "pg_play": {
      "command": "/absolute/path/to/pg-play-mcp",
      "args": []
    }
  }
}
```

Copy skills to `~/.agents/skills/`, `~/.gemini/skills/`, or their project
equivalents. Use `/mcp list` and `/skills list`; use `/skills reload` after
changing skill files.

## OpenCode

Official documentation: [MCP](https://opencode.ai/docs/mcp-servers/) ·
[Skills](https://opencode.ai/docs/skills)

Add this to the project `opencode.json` or the user configuration:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "pg_play": {
      "type": "local",
      "command": ["/absolute/path/to/pg-play-mcp"],
      "enabled": true
    }
  }
}
```

Copy skills to `~/.agents/skills/`, `~/.config/opencode/skills/`, or their
project equivalents. Run `opencode mcp list` to verify the connection.

## Recommended first prompt

```text
Use the pg_play MCP tools and the matching pg_play skill. Validate and plan
before starting any mutation. Show me the plan and ask for missing access or
tuning inputs. Use detached start operations and monitor status and events.
```

`pg-play-mcp` currently uses stdio only. The client must therefore run on a host
where the executable, Docker access, SSH keys, manifests, and report paths are
available. A hosted agent needs a separately secured remote MCP transport.
