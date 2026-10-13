# Research OS

English | [简体中文](README.zh-CN.md)

An MCP-powered research workspace for importing papers, exploring method knowledge graphs, and developing algorithmic ideas grounded in original paper evidence.

## Demo

### Knowledge graph exploration

![Knowledge graph exploration](docs/images/knowledge-graph-exploration.png)

### Original PDF evidence

![Original PDF evidence](docs/images/pdf-evidence-demo.gif)

### Evidence-backed research ideas

![Evidence-backed research ideas demo](docs/images/knowledge-graph-exploration-demo.gif)

## Quick Start

### 1. Prepare the environment

You need Git, Docker with Compose, uv, a Python version compatible with [pyproject.toml](research-os-mcp/pyproject.toml), a local MCP-capable client such as Codex, and an OpenAI-compatible LLM endpoint that supports strict JSON Schema structured output. The commands below use a Bash-compatible shell.

```bash
git clone https://github.com/YangSuoze/research-os.git
cd research-os
```

### 2. Start the databases

Run from the repository root:

```bash
docker compose up -d
docker compose ps
```

| Service | Local address | Purpose |
| --- | --- | --- |
| Neo4j Browser | http://127.0.0.1:7474 | Inspect the research graph |
| Elasticsearch | http://127.0.0.1:9200 | Paper chunk storage and BM25 search |

The local Neo4j defaults are user `neo4j` and password `12345678`. If you change the Compose password, update the MCP configuration to match. These defaults are for local development only; Elasticsearch authentication is disabled and the published ports bind to loopback.

### 3. Configure the LLM

Enter the MCP project and create its `.env` only if it does not already exist:

```bash
cd research-os-mcp
if [ ! -e .env ] && [ ! -L .env ]; then
  cp .env.example .env
fi
```

Edit `research-os-mcp/.env` and fill in:

| Variable | Value |
| --- | --- |
| `BACKEND_LLM_BASE_URL` | Your provider's OpenAI-compatible API base URL |
| `BACKEND_LLM_API_KEY` | Your API key |
| `BACKEND_MODEL_NAME` | A model supporting the required structured output |

Keep the other values from [.env.example](research-os-mcp/.env.example) for the default local setup. `BACKEND_GITHUB_TOKEN` is optional for repository lookup; `BACKEND_WORKSPACE_ROOT` can override the storage directory using an absolute path. The `BACKEND_` prefix is intentional.

### 4. Install dependencies and start MCP

Run inside `research-os-mcp`:

```bash
uv sync --extra pdf --extra graph
uv run --extra pdf --extra graph python -m paper_import_mcp.server \
  --transport streamable-http --port 8002
```

Keep this terminal running. The endpoint is `http://127.0.0.1:8002/mcp`. The ES/indexing and extraction stage defaults to a concurrency limit of 5, configurable through `BACKEND_PAPER_EXTRACT_CONCURRENCY`.

This guide explicitly selects Streamable HTTP so original evidence can include clickable PDF viewer links. The server's default transport is stdio, which does not provide HTTP PDF links. See the [MCP documentation](research-os-mcp/README.md) for stdio setup and detailed behavior.

## Connect to Codex

With the HTTP server running, register it using the Codex CLI from another terminal:

```bash
codex mcp add paper2graph --url http://127.0.0.1:8002/mcp
codex mcp list
```

If you use the client's local MCP settings instead, set the name to `paper2graph`, choose Streamable HTTP, and enter the same URL. See the [official Codex MCP configuration guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

Start a new turn with the connection enabled and ask the agent to list the available `paper2graph` tools or call `get_graph_schema`. Expect the eight tools listed below; a startup log alone does not establish client connectivity.

## Install Skills

| Skill | Purpose |
| --- | --- |
| [paper-import](research-os-skills/paper-import/SKILL.md) | Import PDFs, track batch progress, and retry failures when authorized |
| [graph-explore](research-os-skills/graph-explore/SKILL.md) | Investigate relationships, statistics, cross-task knowledge, and original evidence |
| [research-idea](research-os-skills/research-idea/SKILL.md) | Develop evidence-backed algorithmic hypotheses and minimal experiments |

In another terminal, enter the repository root before running this macOS/Linux symlink example. Existing destinations, including broken symlinks, are left untouched:

```bash
research_os_root="$(pwd)"
mkdir -p "$HOME/.agents/skills"
for research_skill in paper-import graph-explore research-idea; do
  research_skill_target="$HOME/.agents/skills/$research_skill"
  if [ -e "$research_skill_target" ] || [ -L "$research_skill_target" ]; then
    printf 'Already exists; check its target: %s\n' "$research_skill_target"
    continue
  fi
  ln -s "$research_os_root/research-os-skills/$research_skill" "$research_skill_target"
done
```

Codex supports this user-level skill directory and follows symlinked folders. See the [official skills guide](https://learn.chatgpt.com/docs/build-skills).

On the next turn, select `$paper-import`, `$graph-explore`, or `$research-idea`. Restart the client if a new skill is not visible. Skill installation does not register the MCP connection; both steps are required.

## Usage Examples

### Import papers

```text
Use $paper-import with paper2graph MCP to import all PDFs directly in /absolute/path/to/papers into task_root_id medical_report_generation. Report progress and supervise the batch to completion, then summarize successful and failed papers with their failure reasons.
```

### Explore the graph and original evidence

```text
Use $graph-explore with paper2graph MCP to find the five most frequently adopted methods for task_root_id medical_report_generation. Count distinct papers, compare the problems addressed, and provide representative papers, original excerpts, and clickable PDF evidence links.
```

### Develop algorithmic research ideas

```text
Use $research-idea with paper2graph MCP to propose three concrete algorithmic research ideas for task_root_id medical_report_generation. Include source evidence, clickable PDF links, the specific algorithmic change, a minimal experiment, and the main risks.
```

## Data and Tools

By default, local files live under the repository's `data/workspace`; Neo4j and Elasticsearch persist in the named Docker volumes defined in [compose.yaml](compose.yaml).

| MCP tool | Purpose |
| --- | --- |
| `create_paper_import_batch` | Submit server-visible PDFs or stored paper IDs for import |
| `get_paper_import_batch` | Read batch status, stages, and errors |
| `retry_failed_paper_import_batch` | Resume only failed papers from their failed stages |
| `get_graph_schema` | Read the application graph structure and query rules |
| `browse_task` | Browse a task's families, methods, problems, and datasets |
| `search_graph` | Query the graph with natural language or direct read-only Cypher |
| `search_paper_chunks` | Search paper chunks using BM25 |
| `get_paper_context` | Retrieve exact original text, block locators, and PDF viewer links |

Natural-language graph search is text-to-Cypher, not vector retrieval. Chunk search uses BM25; the local embedding model is currently used for graph node alignment during ingestion. For exact parameters, query restrictions, and evidence fields, see the [MCP documentation](research-os-mcp/README.md).

## Troubleshooting

| Issue | Check |
| --- | --- |
| MCP connection fails | Keep the server running; use matching transport and port. If a proxy is configured, set `NO_PROXY=127.0.0.1,localhost` in the environments of the client and server, preserving other needed exclusions. |
| First import is slow | Initial parsing and embedding model downloads/loading can take time. Check per-paper stages and logs rather than inferring failure from elapsed time. |
| LLM extraction fails | Check the base URL, API key, model name, and strict structured-output support; inspect the returned stage and error. |
| A batch partially fails | Preserve its batch ID and use the authorized retry workflow. Do not resubmit the entire batch just to retry failed papers. |
| A PDF link is missing or will not open | Use HTTP mode and keep the server running; retain the stored PDF. stdio or a missing PDF produces null viewer URLs. The viewer also needs access to the PDF.js CDN. |
| A skill is missing | Check the symlink target and start a new turn or restart the client. Ensure the separate `paper2graph` MCP connection is enabled. |
