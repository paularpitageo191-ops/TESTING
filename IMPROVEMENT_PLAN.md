# TEA Improvement Plan And Roadmap

This document captures the current maturity of the TEA framework and the recommended roadmap to evolve it from a hybrid script-plus-orchestrator system into a more complete agentic QA platform.

It answers three questions:

1. Where are we now?
2. What should improve next?
3. In what order should we build it?

## Current Position

TEA is currently in a hybrid state:

- the original script-based QA flow is still the operational backbone
- a Streamlit control layer now exists
- an agentic orchestrator exists
- MCP tools and router exist
- persistent run memory exists
- guided mode and auto-run mode exist

But:

- most execution still goes through wrapped phase scripts
- MCP is available but not yet the dominant execution path
- phase outputs are only partially normalized
- enterprise integrations are still incomplete

## Current Maturity Snapshot

| Capability | Status | Notes |
| --- | --- | --- |
| Jira requirement ingestion | Implemented | `jira_sync_agent.py` works and UI now surfaces its artifacts |
| DOM capture | Implemented | Script exists and is part of guided flow |
| Requirement vectorization | Implemented | Qdrant upload and PRD generation exist |
| Gherkin generation | Implemented | `quality_alignment.py` generates features |
| Playwright step generation | Implemented | `step_generator.py` generates specs |
| Test execution | Implemented | Playwright execution wired into flow |
| Self-healing analysis | Implemented | `self_healing_agent.py` exists |
| Jira reporting | Implemented | `report_to_jira.py` exists |
| Streamlit guided flow | Implemented | Guided mode exists and is usable |
| Streamlit auto-run flow | Implemented | Auto-approves checkpoints end to end |
| Sidebar runtime config from `.env` | Implemented | UI defaults to `.env` values |
| Flow tracker in UI | Implemented | Current step is highlighted |
| Persistent run memory | Implemented | `agent_memory.py` exists |
| MCP router | Implemented | Central router exists |
| Jira MCP tools | Implemented | Create bug, add comment, get issue |
| Qdrant MCP tools | Implemented | Search and upsert tools exist |
| DOM MCP tools | Implemented | Snapshot and element lookup exist |
| MCP-first orchestration | Partial | Tools exist, but planner mostly uses phase execution |
| Structured phase output contracts | Partial | Some artifacts surfaced, not fully standardized |
| Phase-specific editing UX | Partial | Editing exists for some phases |
| Multi-browser execution | Not implemented | Needs matrix strategy and UI controls |
| Zephyr first-class integration | Partial | Present in healing path, not elevated in UI/orchestration |
| GitHub integration | Not implemented | No branch/PR flow yet |
| Jenkins integration | Not implemented | No CI trigger or job sync yet |
| Test coverage for orchestrator/UI | Not implemented | Needs unit and integration tests |

## Where We Are Already

### Completed foundations

- Existing TEA scripts preserved and still runnable
- Agentic orchestrator added
- MCP tool layer added
- Streamlit UI added
- Guided step-by-step mode added
- Auto-run mode added
- Runtime sidebar config added
- Phase tracker UI added
- Artifact previews added
- README aligned with the current flow
- Dedicated architecture document added

## Current Integrations

The TEA system currently integrates with the following platforms and execution layers:

### Jira

Used for:

- story and epic ingestion
- related issue lookup
- attachment download
- execution reporting
- bug creation
- issue comments and linking

Current implementation points:

- `jira_sync_agent.py`
- `report_to_jira.py`
- `self_healing_agent.py`
- `agents/mcp_tools/jira_tools.py`

### Qdrant

Used for:

- requirement vector memory
- DOM / UI semantic memory
- semantic similarity search
- test generation context
- self-healing analysis support

Current implementation points:

- `vectorize_and_upload.py`
- `dom_capture.py`
- `quality_alignment.py`
- `step_generator.py`
- `self_healing_agent.py`
- `agents/mcp_tools/qdrant_tools.py`

### DOM / Playwright Discovery

Used for:

- live UI capture
- semantic element discovery
- screenshot capture
- auth-aware application exploration

Current implementation points:

- `dom_capture.py`
- `tests/_bmad/BasePage.ts`
- `agents/mcp_tools/dom_tools.py`

### Gherkin and Playwright Generation

Used for:

- converting requirements into Gherkin feature files
- converting Gherkin into runnable Playwright TypeScript specs

Current implementation points:

- `quality_alignment.py`
- `step_generator.py`

### Self-Healing and Failure Analysis

Used for:

- drift detection
- flaky behavior classification
- semantic failure analysis
- optional Jira / Zephyr reporting support

Current implementation points:

- `self_healing_agent.py`

### Jira Bug Reporting

Used for:

- filing bugs for failed scenarios
- linking defects to stories
- adding execution comments and attachments

Current implementation points:

- `report_to_jira.py`
- `self_healing_agent.py`
- `agents/mcp_tools/jira_tools.py`

## Current Ingestion Pipeline

The current TEA ingestion and generation pipeline is:

```text
Jira Story / Epic / Attachments
  -> Jira Sync
  -> DOM Capture
  -> Requirement Vectorization + PRD Generation
  -> Gherkin Test Case Generation
  -> Playwright TypeScript Generation
  -> Playwright Execution
  -> Self-Healing Analysis
  -> Jira Reporting / Bug Reporting
```

### Stage-by-stage breakdown

#### 1. Jira ingestion

Source:

- Jira issue
- epic context
- related issues
- attachments

Outputs:

- normalized requirements JSON
- consolidated extracted text
- downloaded attachment files

#### 2. DOM ingestion

Source:

- live application URL
- optional credentials

Outputs:

- DOM snapshot JSON
- screenshots
- semantic UI memory in Qdrant

#### 3. Requirement semantic ingestion

Source:

- requirements JSON
- attachment content
- optional DOM-derived context

Outputs:

- vectorized requirement memory in Qdrant
- PRD markdown

#### 4. Gherkin-style test generation

Source:

- requirement memory
- PRD
- DOM context

Outputs:

- Gherkin feature files suitable for review and alignment

#### 5. Runnable TypeScript Playwright generation

Source:

- Gherkin feature files
- Qdrant UI memory
- BasePage runtime behavior

Outputs:

- runnable TypeScript Playwright spec files

#### 6. Execution and healing

Source:

- generated Playwright specs
- Playwright runtime
- semantic memory

Outputs:

- execution artifacts
- healing logs
- failure classifications

#### 7. Reporting and defects

Source:

- execution output
- healing analysis
- Jira credentials and issue context

Outputs:

- Jira comments
- local markdown reports
- bug tickets when applicable

## Current Tools Used

### Core orchestration and runtime

- `AgentOrchestratorV2`
- `Streamlit`
- `LLMGateway`
- `AgentMemory`
- `MCPRouter`

### Platform integrations

- Jira REST API
- Qdrant
- Playwright
- Zephyr support in healing flow

### Generation and automation layers

- Gherkin feature generation
- Playwright TypeScript spec generation
- semantic element resolution through `BasePage.ts`

### Current MCP tools

- `jira.create_bug`
- `jira.add_comment`
- `jira.get_issue`
- `qdrant.vector_search`
- `qdrant.vector_upsert`
- `dom.find_element`
- `dom.get_dom_snapshot`

## Current Test Generation Capability

Today the system already supports:

- Jira-backed requirement ingestion
- DOM-aware test generation
- Gherkin-style test case creation
- runnable TypeScript Playwright generation
- semantic UI interaction
- self-healing analysis
- Jira bug reporting

This means TEA is already more than a static generator. It is currently a working hybrid QA automation platform with:

- requirement ingestion
- semantic memory
- UI discovery
- test generation
- execution
- healing
- reporting

### What is only partially done

- MCP is present but not heavily used during normal execution
- UI is cleaner, but outputs are still a mix of structured content and script artifacts
- Edit handling works for selected phases, but not all
- Planner still relies on fallback behavior more than ideal

### What is still missing

- full MCP-driven orchestration
- browser matrix support
- stronger Zephyr UX
- GitHub integration
- Jenkins integration
- automated testing and hardening

## Target Future State

The target TEA platform should look like this:

```text
Agentic QA Control Plane
  - Jira ingestion and reporting
  - MCP-based operational tool layer
  - Qdrant semantic memory
  - DOM intelligence
  - Gherkin and Playwright generation
  - Guided HITL mode
  - Full auto-run mode
  - Multi-browser execution
  - Zephyr reporting
  - GitHub branch / PR workflows
  - Jenkins / CI execution
  - Persistent learning from past runs
```

## Improvement Themes

## 1. Orchestration Quality

Goal:

- make the orchestrator smarter, more deterministic, and less dependent on fallback behavior

Improvements:

- add stronger planner guardrails
- prefer deterministic phase progression when appropriate
- improve retry decisions
- add explicit stop conditions
- separate planning from execution more cleanly

## 2. MCP-First Behavior

Goal:

- make MCP usage visible and central, not just optional plumbing

Improvements:

- call Jira MCP for issue fetches, comments, and bug actions
- call Qdrant MCP for memory inspection and validation
- call DOM MCP for snapshot inspection during runs
- expose tool usage as first-class run events
- reduce reliance on raw script behavior where tools can safely replace it

## 3. Structured Phase Contracts

Goal:

- make every phase produce normalized output for the UI and orchestrator

Target contract:

```python
{
  "status": "success|failed|warning",
  "summary": "...",
  "artifacts": [],
  "editable_content": {},
  "metrics": {},
  "errors": [],
}
```

Benefits:

- better UI rendering
- cleaner approvals
- easier automation
- less dependence on `stdout`

## 4. Better HITL

Goal:

- make guided mode feel like a controlled product workflow, not a debug shell

Improvements:

- phase-specific edit experiences
- approve with comment
- clearer rerun reasons
- explicit change tracking when user edits outputs
- better display of what becomes next-step input

## 5. Stronger Auto-Run

Goal:

- make unattended mode production-ready

Improvements:

- stop-on-failure vs continue-on-failure mode
- automatic rerun policies
- retry limits
- browser selection for auto-run
- final summary and downloadable report

## 6. Multi-Browser Execution

Goal:

- execute the same TEA-generated flow across multiple browser engines

Target support:

- Chromium
- Firefox
- WebKit

Improvements:

- sidebar browser selector
- single-browser or all-browser mode
- browser-wise summary in UI
- browser-specific artifacts and healing logs

## 7. Zephyr Integration

Goal:

- make Zephyr a first-class reporting path

Improvements:

- expose Zephyr config in UI clearly
- allow:
  - Jira-only reporting
  - Zephyr-only reporting
  - both
- display cycles, test cases, execution IDs, and sync state

## 8. GitHub Integration

Goal:

- connect generated and edited automation artifacts to source control workflows

Improvements:

- push generated tests to a branch
- create PRs
- comment run summary on PRs or issues
- optionally commit self-healed changes

## 9. Jenkins Integration

Goal:

- support CI-driven TEA execution

Improvements:

- trigger Jenkins jobs from TEA
- pass project/browser/environment parameters
- fetch build status back into the UI
- allow hybrid local-control and remote-execution model

## 10. Memory And Learning

Goal:

- make memory influence future runs in a useful way

Improvements:

- store repeated failure patterns
- store successful human edits
- store rerun reasons
- suggest likely failure causes
- suggest step skipping when earlier outputs are unchanged

## 11. Observability And Diagnostics

Goal:

- make debugging runs easier without cluttering guided mode

Improvements:

- dedicated orchestrator log files
- downloadable run bundle
- per-phase duration
- retry history
- better diagnostics panel

## 12. Quality And Hardening

Goal:

- make the platform reliable enough for broader use

Improvements:

- schema validation
- unit tests for router/orchestrator/memory
- integration tests for guided mode
- integration tests for auto-run mode
- failure simulation coverage

## Roadmap

## Phase 0: Current Baseline

Status: In progress, foundation established

Already available:

- script-based TEA pipeline
- Streamlit UI
- guided mode
- auto-run mode
- MCP router and tools
- memory
- flow tracker
- sidebar runtime config

Main limitation:

- architecture is hybrid, not yet fully MCP-first

## Phase 1: Stabilize The Agentic Core

Priority: Highest

Goals:

- make the current system robust and predictable

Deliverables:

- standardized phase output model
- better planner validation
- fewer null/fallback planner states
- improved rerun semantics
- clearer edit propagation
- remove residual debug-oriented UI behavior

Success criteria:

- every phase renders a structured result
- guided mode is easy to use without needing technical interpretation
- auto-run completes predictable flows without manual intervention

## Phase 2: Make MCP Operationally Real

Priority: High

Goals:

- move from “MCP exists” to “MCP is actively used”

Deliverables:

- Jira MCP-first read/write actions
- DOM MCP usage during inspection and validation
- Qdrant MCP usage during memory checks and retries
- explicit tool activity timeline in UI
- planner prompt tuned to favor tools when useful

Success criteria:

- normal runs show visible MCP tool usage
- fewer cases require full script execution for simple operations

## Phase 3: Expand Execution Capability

Priority: High

Goals:

- broaden where and how TEA can run

Deliverables:

- multi-browser matrix support
- stop-on-failure vs continue-on-failure execution policies
- stronger auto-run configuration
- richer result summaries per browser and per step

Success criteria:

- one flow can be executed across all selected browsers
- artifacts are clearly grouped by browser and run

## Phase 4: Reporting And Enterprise Integration

Priority: Medium to High

Goals:

- make TEA more enterprise-friendly

Deliverables:

- Zephyr-first UI and reporting options
- GitHub branch and PR workflow support
- Jenkins trigger and status sync support

Success criteria:

- generated and edited automation can be promoted through source control
- CI systems can execute TEA runs reliably
- Zephyr reporting is visible and configurable in the UI

## Phase 5: Learning And Production Hardening

Priority: Medium

Goals:

- turn TEA into a more dependable long-running system

Deliverables:

- memory-informed suggestions
- schema validation
- better diagnostics
- unit and integration test coverage
- stronger error handling

Success criteria:

- repeated failure causes are detected and surfaced
- regression risk is reduced through automated tests

## Recommended Build Order

1. Standardize phase outputs
2. Improve orchestrator decision quality
3. Increase real MCP usage
4. Improve guided-mode edit and rerun behavior
5. Strengthen auto-run policies
6. Add multi-browser support
7. Strengthen Zephyr integration
8. Add GitHub integration
9. Add Jenkins integration
10. Add test coverage and hardening

## Suggested Immediate Next Sprint

If we were to pick the most valuable next short-term work, it should be:

### Sprint A

- standardize phase output objects
- improve planner reliability
- surface MCP usage clearly in the UI

### Sprint B

- make Jira/Qdrant/DOM tool calls part of normal orchestration
- improve edit propagation and rerun logic
- clean final UI wording and state transitions

### Sprint C

- add multi-browser execution
- add Zephyr options in UI and reporting

## Final Assessment

Current state:

```text
TEA is beyond a pure script pipeline,
but not yet a fully MCP-native orchestration platform.
```

That is a good place to be:

- core automation value already exists
- the new control plane has been introduced
- the roadmap is now about deepening capability, not starting from scratch
