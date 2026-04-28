# TEA Framework

TEA (Test Execution Agent) is an AI-powered QA automation framework that turns Jira requirements into executable Playwright tests, runs them, applies self-healing analysis, and reports results back to Jira.

The repo now supports two operating modes:

1. The original CLI pipeline, which remains intact.
2. A newer agentic layer with MCP-style tools, persistent memory, HITL checkpoints, and a Streamlit UI.

## What The System Does

TEA combines:

- Jira requirement ingestion
- Requirement vectorization into Qdrant
- Live DOM discovery
- Gherkin generation
- Playwright step generation
- Test execution
- Self-healing and drift analysis
- Jira and markdown reporting

## Current Architecture

### Legacy execution path

```text
Jira
  -> jira_sync_agent.py
  -> dom_capture.py
  -> vectorize_and_upload.py
  -> quality_alignment.py
  -> step_generator.py
  -> Playwright execution
  -> self_healing_agent.py
  -> report_to_jira.py
```

### Agentic execution path

```text
Streamlit UI
  -> AgentOrchestratorV2
     -> LLMGateway
        -> MCP Router
           -> Jira tools
           -> Qdrant tools
           -> DOM tools
     -> Existing phase scripts
     -> Human approval after each step
     -> Persistent run memory
```

## Project Structure

```bash
Testing_Special_Agents/
├── jira_sync_agent.py
├── vectorize_and_upload.py
├── dom_capture.py
├── quality_alignment.py
├── step_generator.py
├── self_healing_agent.py
├── report_to_jira.py
├── llm_gateway.py
├── bmad_factory.py
├── agents/
│   ├── agent_memory.py
│   ├── agent_orchestrator_v2.py
│   ├── mcp_router.py
│   └── mcp_tools/
│       ├── jira_tools.py
│       ├── qdrant_tools.py
│       └── dom_tools.py
├── ui/
│   └── app.py
├── docs/
├── test-results/
├── tests/
│   ├── features/
│   ├── steps/
│   └── _bmad/
├── requirements.txt
├── package.json
└── README.md
```

## How The Current Flow Works

### Phase-by-phase flow

| Phase | Script / Layer | Main Purpose | Typical Input | Typical Output |
| --- | --- | --- | --- | --- |
| 0 | `jira_sync_agent.py` | Pull Jira issue, epic context, and attachments | Jira issue key | `docs/requirements_<PROJECT>.json`, downloaded attachments |
| 1 | `dom_capture.py` | Crawl live UI and capture semantic DOM memory | `BASE_URL`, credentials, optional session | DOM JSON, screenshots, Qdrant `<PROJECT>_ui_memory` |
| 2 | `vectorize_and_upload.py` | Normalize and vectorize requirements into Qdrant, generate PRD | Requirements JSON, attachments, optional DOM snapshot | Qdrant `<PROJECT>_requirements`, PRD markdown |
| 3 | `quality_alignment.py` | Generate Gherkin from requirements + DOM | Qdrant collections, PRD, inbox JSON, DOM snapshot | `tests/features/<PROJECT>.feature`, quality report JSON |
| 4 | `step_generator.py` | Generate Playwright TypeScript from Gherkin | Feature file, Qdrant DOM memory, `BASE_URL` fallback | `tests/steps/<PROJECT>.spec.ts` |
| 5 | Playwright | Execute generated tests | Step file, `BasePage.ts`, app under test | `test-results/`, Playwright report |
| 6 | `self_healing_agent.py` | Analyze failures, drift, flaky behavior, optional Zephyr/Jira sync | Playwright JSON results, Qdrant, Jira/Zephyr config | Healing logs, bugs, test execution metadata |
| 7 | `report_to_jira.py` | Write summary report back to Jira and local markdown | Healing logs, screenshots, Jira config | `docs/jira-reports/*.md`, Jira comments, attachments |

### Current guided Streamlit order

The Streamlit app currently runs the TEA flow in this order:

```text
Jira Sync
  -> DOM Capture
  -> Vectorize Requirements
  -> Quality Alignment
  -> Step Generation
  -> Execution
  -> Self Healing
  -> Reporting
```

This order is intentional and reflects the current UI/orchestrator behavior.

### Agentic flow behavior

The new orchestrator in `agents/agent_orchestrator_v2.py` does not hardcode a fixed pipeline inside the UI layer. Instead it:

- asks the LLM for the next action
- restricts actions to approved phases and MCP tools
- pauses after each step for human review in guided mode
- supports approve, edit, and reject plus rerun
- also supports an auto-run mode that auto-approves every checkpoint
- records decisions and failures in `docs/agent-memory/memory.json`

## How To Run

## 1. Setup

### Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Node and Playwright

```bash
npm install
npx playwright install
```

### Qdrant

```bash
docker run -p 6333:6333 qdrant/qdrant
```

### Ollama, if using local LLMs

```bash
ollama serve
ollama pull mxbai-embed-large
ollama pull llama3
```

## 2. Configure `.env`

Create a `.env` file at the repo root.

Minimal example:

```env
# Jira
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your_jira_api_token
JIRA_PROJECT_KEY=SCRUM

# Zephyr
ZEPHYR_BASE_URL=https://api.zephyrscale.smartbear.com/v2
ZEPHYR_TOKEN=

# LLM provider
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
CHAT_MODEL=llama3:8b
MODEL_NAME=codellama:13b
EMBEDDING_MODEL=mxbai-embed-large:latest

# OpenAI, if used
OPENAI_API_KEY=

# Anthropic, if used
ANTHROPIC_API_KEY=

# Gemini, if used
GEMINI_API_KEY=

# Vector DB
QDRANT_URL=http://localhost:6333

# Application under test
BASE_URL=https://your-app-url.com
ADMIN_USERNAME=
ADMIN_PASSWORD=

# Optional factory default
PROJECT_KEY=SCRUM-86
```

## 3. Run The Original CLI Flow

### Full manual phase flow

```bash
python3 jira_sync_agent.py --issue SCRUM-86 --project SCRUM
python3 dom_capture.py --project SCRUM-86
python3 vectorize_and_upload.py --project SCRUM-86
python3 quality_alignment.py --project SCRUM-86
python3 step_generator.py --project SCRUM-86
npx playwright test tests/steps/SCRUM-86.spec.ts --project=chromium --headed
python3 self_healing_agent.py --project SCRUM-86
python3 report_to_jira.py --project SCRUM-86
```

### Factory mode

`bmad_factory.py` is the existing CLI orchestrator. It is still supported.

```bash
python3 bmad_factory.py --project SCRUM-86
```

Examples:

```bash
python3 bmad_factory.py --project SCRUM-86
python3 bmad_factory.py --project SCRUM-86 --phase 1 2 3 4
python3 bmad_factory.py --project SCRUM-86 --monitor
```

## 4. Run The New Streamlit UI

```bash
streamlit run ui/app.py
```

What the UI provides:

- sidebar-driven runtime config using `.env` defaults
- project and start-phase selection
- guided step-by-step review mode
- auto-run mode without HITL
- live execution log
- execution timeline
- flow tracker with the current step highlighted
- per-step decision buttons:
  - `Approve`
  - `Edit`
  - `Rerun`
- phase-specific editing for PRD, Gherkin, and generated steps
- run summary and memory-backed analytics

### Current UI controls

The main run controls are:

- `Start guided run`
- `Auto run without review`
- `Continue to next step`
- `Reset run`

In guided mode:

- `Approve` immediately advances to the next step
- `Edit` saves the edited artifact and uses it as input for the next step
- `Rerun` reruns the same step immediately

In auto-run mode:

- the orchestrator auto-approves each checkpoint and runs the full flow end to end

## Input And Output Mapping

## Inputs by source

### Jira inputs

Used by:

- `jira_sync_agent.py`
- `self_healing_agent.py`
- `report_to_jira.py`
- `agents/mcp_tools/jira_tools.py`

Main runtime inputs:

- Jira issue key such as `SCRUM-86`
- Jira project key such as `SCRUM`
- issue attachments from Jira
- credentials from `.env`

### Repository and local file inputs

Used by:

- `vectorize_and_upload.py`
- `quality_alignment.py`
- `step_generator.py`
- Streamlit and orchestrator

Main local inputs:

- `docs/inbox/*`
- `docs/requirements/*.md`
- `docs/live_dom_elements_*.json`
- `tests/features/*.feature`
- `tests/steps/*.spec.ts`
- `test-results/results.json`

### Application under test inputs

Used by:

- `dom_capture.py`
- generated Playwright tests

Main app inputs:

- `BASE_URL`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- optional session file for DOM capture

### Qdrant inputs

Used by:

- `vectorize_and_upload.py`
- `quality_alignment.py`
- `step_generator.py`
- `self_healing_agent.py`
- MCP Qdrant tools

Main Qdrant inputs:

- requirement text chunks
- DOM element snapshots
- semantic search queries

## Outputs by phase

| Output Path / Target | Producer | Purpose |
| --- | --- | --- |
| `docs/requirements_<PROJECT>.json` | `jira_sync_agent.py` | Normalized Jira requirement bundle |
| `docs/inbox/attachments/*` | `jira_sync_agent.py` | Downloaded attachments |
| `docs/requirements/<PROJECT>_PRD.md` or `docs/<PROJECT>_prd.md` | `vectorize_and_upload.py` | Generated PRD / requirement summary |
| Qdrant `<PROJECT>_requirements` | `vectorize_and_upload.py` | Requirement memory |
| `docs/live_dom_elements_<PROJECT>_<timestamp>.json` | `dom_capture.py` | DOM snapshot |
| `docs/screenshots/*.png` | `dom_capture.py`, Playwright | UI evidence |
| Qdrant `<PROJECT>_ui_memory` | `dom_capture.py`, optional vectorization | Semantic UI memory |
| `tests/features/<PROJECT>.feature` | `quality_alignment.py` | Gherkin feature file |
| `docs/quality_alignment_report_<PROJECT>.json` | `quality_alignment.py` | Traceability and validation report |
| `tests/steps/<PROJECT>.spec.ts` | `step_generator.py` | Generated Playwright test |
| `test-results/` | Playwright | Raw run artifacts |
| `playwright-report/` | Playwright | HTML report |
| `docs/healing-logs/*.json` | `self_healing_agent.py` | Failure classification and drift analysis |
| `docs/jira-reports/*.md` | `report_to_jira.py` | Local Jira-facing markdown report |
| `docs/agent-memory/memory.json` | `agent_memory.py` | Persistent orchestrator memory |

## `.env` Variable Mapping

## Required or commonly used variables

| Variable | Used By | Purpose |
| --- | --- | --- |
| `JIRA_BASE_URL` | Jira sync, healing, reporting, Jira MCP tools | Base Jira REST URL |
| `JIRA_EMAIL` | Jira sync, healing, reporting, Jira MCP tools | Jira auth username |
| `JIRA_API_TOKEN` | Jira sync, healing, reporting, Jira MCP tools | Jira auth token |
| `JIRA_PROJECT_KEY` | Jira sync, Jira MCP tools | Default Jira project |
| `ZEPHYR_BASE_URL` | `self_healing_agent.py` | Zephyr API base |
| `ZEPHYR_TOKEN` | `self_healing_agent.py` | Zephyr auth token |
| `LLM_PROVIDER` | `llm_gateway.py` and all LLM-backed phases | Select `ollama`, `openai`, `claude`, or `gemini` |
| `OLLAMA_HOST` | `llm_gateway.py`, embedding consumers | Ollama endpoint |
| `CHAT_MODEL` | `llm_gateway.py` | Main chat model |
| `MODEL_NAME` | `llm_gateway.py` | Optional alternate model for some structured analysis |
| `EMBEDDING_MODEL` | Embedding generation | Embedding model name |
| `OPENAI_API_KEY` | `llm_gateway.py` | OpenAI auth |
| `ANTHROPIC_API_KEY` | `llm_gateway.py` | Anthropic auth |
| `GEMINI_API_KEY` | `llm_gateway.py` | Gemini auth |
| `QDRANT_URL` | Vectorization, alignment, steps, healing, MCP tools | Qdrant endpoint |
| `BASE_URL` | DOM capture, quality alignment, step generation | App under test base URL |
| `ADMIN_USERNAME` | `dom_capture.py` | Login for app discovery |
| `ADMIN_PASSWORD` | `dom_capture.py` | Login for app discovery |
| `PROJECT_KEY` | `bmad_factory.py` | Optional default project for factory mode |

## Provider selection examples

### Ollama

```env
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
CHAT_MODEL=llama3:8b
EMBEDDING_MODEL=mxbai-embed-large:latest
```

### OpenAI

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=your_key
CHAT_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
```

### Anthropic

```env
LLM_PROVIDER=claude
ANTHROPIC_API_KEY=your_key
CHAT_MODEL=claude-3-sonnet
```

### Gemini

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key
CHAT_MODEL=gemini-pro
```

## MCP Tool Layer

The MCP-like tool layer lives in `agents/mcp_tools/` and is routed through `agents/mcp_router.py`.

Available tools:

- `jira.create_bug`
- `jira.add_comment`
- `jira.get_issue`
- `qdrant.vector_search`
- `qdrant.vector_upsert`
- `dom.find_element`
- `dom.get_dom_snapshot`

Tool calls are intended to be:

- stateless
- reusable
- callable from orchestration logic
- independent of the legacy phase scripts

## Human In The Loop

In the Streamlit path, every orchestrator step can pause for review.

Supported human actions:

1. Approve
2. Edit
3. Reject plus retry

Edit support:

- PRD edits can be written back to markdown artifacts
- Gherkin edits can be written back to `.feature` files
- Step edits can be written back to `.ts` step files

## Persistent Memory

The orchestrator stores run history in:

```bash
docs/agent-memory/memory.json
```

Stored information includes:

- runs
- decisions
- failures
- simple run stats

The memory is used to help future planner decisions avoid repeated failure patterns.

## Debugging Notes

### Useful files to inspect during a run

- `docs/logs/*`
- `docs/live_dom_elements_*.json`
- `docs/quality_alignment_report_*.json`
- `docs/healing-logs/*.json`
- `docs/jira-reports/*.md`
- `test-results/results.json`
- `playwright-report/index.html`

### Common failure sources

- Jira credentials missing or invalid
- Qdrant not running on `QDRANT_URL`
- LLM provider not reachable
- `BASE_URL` not set or not accessible
- no DOM snapshot available before quality alignment
- no feature file available before step generation

## Backward Compatibility

The following are preserved:

- all original root scripts
- the existing phase-by-phase CLI flow
- `bmad_factory.py`
- Playwright test generation and execution behavior

The new `agents/` package and `ui/app.py` are additive layers, not replacements for the old CLI path.

## Recommended Run Order

If you want the safest path today:

1. Confirm `.env` is valid.
2. Start Qdrant.
3. Start your chosen LLM provider.
4. Run the CLI flow once for a known project.
5. Then use `streamlit run ui/app.py` for the HITL agentic path.

## Summary

TEA is now a hybrid system:

- a stable script-based automation pipeline for direct execution
- a newer agentic orchestration layer for UI-driven, reviewable runs

That lets you keep existing automation behavior while evolving toward:

- LLM-directed flow selection
- MCP-style tool execution
- human oversight
- persistent operational memory
