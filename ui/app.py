#!/usr/bin/env python3
"""Streamlit UI for the TEA agentic orchestrator."""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import streamlit as st
from dotenv import load_dotenv

from agents.agent_memory import AgentMemory
from agents.agent_orchestrator_v2 import AgentOrchestratorV2

load_dotenv()

st.set_page_config(page_title="TEA Agentic QA", layout="wide")

PHASE_GUIDE = {
    "jira_sync": {
        "title": "Jira Sync",
        "purpose": "Pull the selected Jira story, related content, and attachments into TEA.",
        "inputs": "Jira issue key, Jira credentials, story attachments",
        "outputs": "Normalized requirements JSON and downloaded attachments",
    },
    "dom_capture": {
        "title": "DOM Capture",
        "purpose": "Discover the live UI and capture semantic DOM memory for the app under test.",
        "inputs": "BASE_URL, app credentials, Playwright, Qdrant",
        "outputs": "DOM snapshot JSON, screenshots, UI memory collection",
    },
    "vectorize": {
        "title": "Vectorize Requirements",
        "purpose": "Convert requirements and PRD content into searchable Qdrant memory.",
        "inputs": "Requirements JSON, attachments, embedding model, Qdrant",
        "outputs": "PRD markdown and Qdrant requirements collection",
    },
    "quality_alignment": {
        "title": "Quality Alignment",
        "purpose": "Generate Gherkin scenarios from requirements and discovered UI context.",
        "inputs": "Requirements memory, PRD, DOM snapshot, LLM",
        "outputs": "Feature file and quality alignment report",
    },
    "step_generation": {
        "title": "Step Generation",
        "purpose": "Translate Gherkin scenarios into executable Playwright TypeScript tests.",
        "inputs": "Feature file, Qdrant UI memory, BasePage runtime",
        "outputs": "Playwright spec file",
    },
    "execution": {
        "title": "Execution",
        "purpose": "Run the generated Playwright tests against the application.",
        "inputs": "Generated spec file, Playwright config, app availability",
        "outputs": "test-results and Playwright report",
    },
    "self_healing": {
        "title": "Self Healing",
        "purpose": "Analyze failures, classify drift/flakiness, and optionally create defects.",
        "inputs": "Playwright results, Qdrant, Jira/Zephyr config",
        "outputs": "Healing logs and optional bugs/execution updates",
    },
    "reporting": {
        "title": "Reporting",
        "purpose": "Publish a final execution summary locally and back to Jira.",
        "inputs": "Healing logs, Jira credentials, run artifacts",
        "outputs": "Markdown report, Jira comments, attachments",
    },
}

PHASE_ORDER = [
    "jira_sync",
    "dom_capture",
    "vectorize",
    "quality_alignment",
    "step_generation",
    "execution",
    "self_healing",
    "reporting",
]
EDITABLE_PHASES = {
    "vectorize": ("PRD", "markdown"),
    "quality_alignment": ("Gherkin", "gherkin"),
    "step_generation": ("Steps", "typescript"),
}


def _available_projects() -> list[str]:
    projects = set()
    for pattern, suffix in [
        (ROOT_DIR / "docs" / "requirements", "_PRD.md"),
        (ROOT_DIR / "tests" / "features", ".feature"),
    ]:
        if pattern.is_dir():
            for path in pattern.iterdir():
                if path.name.endswith(suffix):
                    projects.add(path.name.replace(suffix, ""))
    return sorted(projects)


def _phase_title(phase: Optional[str]) -> str:
    if not phase:
        return "Not started"
    return PHASE_GUIDE.get(phase, {}).get("title", phase.replace("_", " ").title())


def _phase_summary(output: Dict[str, Any]) -> str:
    phase = output.get("phase")
    if not phase:
        return "No phase output available yet."

    meta = PHASE_GUIDE.get(phase, {})
    if output.get("ok"):
        parts = [f"{meta.get('title', phase)} completed."]
        artifact = output.get("artifact_path")
        if artifact:
            parts.append(f"Primary artifact: `{artifact}`.")
        if output.get("artifact_files"):
            parts.append(f"Recent generated files: {', '.join(output['artifact_files'])}.")
        if output.get("preview"):
            parts.append("A preview of the generated content is available below.")
        return " ".join(parts)

    error = (output.get("stderr") or output.get("error") or output.get("stdout") or "").strip()
    if not error:
        error = "The phase failed, but no detailed error message was captured."
    return f"{meta.get('title', phase)} failed. Main error: {error.splitlines()[0]}"


def _truncate(text: str, limit: int = 1200) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n..."


def _artifact_preview(output: Dict[str, Any]) -> str:
    return output.get("preview") or output.get("stdout") or ""


def _render_phase_card(state: Dict[str, Any]) -> None:
    output = state.get("last_output") or {}
    phase = output.get("phase") or state.get("current_phase")
    if not phase:
        st.info("No phase result to display yet.")
        return

    st.subheader(f"{_phase_title(phase)} Review")
    st.write(_phase_summary(output))

    if phase == "jira_sync":
        st.markdown("**What this step should produce**")
        st.write("A normalized requirement bundle from Jira, plus downloaded attachments.")
        if output.get("artifact_path"):
            st.caption(f"Requirements JSON: {output['artifact_path']}")
        if output.get("inbox_copy_path"):
            st.caption(f"Inbox copy: {output['inbox_copy_path']}")
        requirements_data = output.get("requirements_data") or {}
        if requirements_data:
            story = requirements_data.get("story") or {}
            epic = requirements_data.get("epic") or {}
            col1, col2, col3 = st.columns(3)
            col1.metric("Attachments Parsed", requirements_data.get("attachments_parsed", 0))
            col2.metric("Requirements Extracted", len(requirements_data.get("requirements", [])))
            col3.metric("Related Issues", len(requirements_data.get("related_issues", [])))
            if story:
                st.markdown("**Story pulled from Jira**")
                st.write(f"Key: `{story.get('key', '')}`")
                st.write(f"Summary: {story.get('summary', '')}")
            if epic:
                st.markdown("**Epic context**")
                st.write(f"Key: `{epic.get('key', '')}`")
                st.write(f"Summary: {epic.get('summary', '')}")
            if requirements_data.get("related_issues"):
                st.markdown("**Related issues**")
                for item in requirements_data["related_issues"][:10]:
                    st.write(f"- `{item.get('key', '')}` | {item.get('type', '')} | {item.get('summary', '')}")
            if output.get("attachment_files"):
                st.markdown("**Attachments downloaded from Jira**")
                for item in output["attachment_files"]:
                    st.write(f"- {item}")
            consolidated_text = requirements_data.get("consolidated_text", "")
            if consolidated_text:
                with st.expander("Extracted requirement text preview", expanded=False):
                    st.code(_truncate(consolidated_text, 1800), language="text")
        if output.get("ok"):
            st.success("Jira sync completed. You can approve this step if the issue and attachments look correct.")
        else:
            st.warning("Jira sync did not complete. Check Jira credentials, issue key, and network access.")

    elif phase == "vectorize":
        st.markdown("**What this step should produce**")
        st.write("PRD markdown and vector memory in Qdrant for the selected project.")
        preview = _artifact_preview(output)
        if preview:
            st.markdown("**PRD preview**")
            st.code(_truncate(preview), language="markdown")

    elif phase == "dom_capture":
        st.markdown("**What this step should produce**")
        st.write("A live DOM snapshot and semantic UI memory for the application under test.")
        if output.get("artifact_path"):
            st.caption(f"Snapshot path: {output['artifact_path']}")
        preview = _artifact_preview(output)
        if preview:
            st.markdown("**DOM capture preview**")
            st.code(_truncate(preview), language="json")

    elif phase == "quality_alignment":
        st.markdown("**What this step should produce**")
        st.write("A Gherkin feature file aligned with the requirements and discovered UI.")
        preview = _artifact_preview(output)
        if preview:
            st.markdown("**Gherkin preview**")
            st.code(_truncate(preview), language="gherkin")

    elif phase == "step_generation":
        st.markdown("**What this step should produce**")
        st.write("An executable Playwright TypeScript spec generated from Gherkin.")
        preview = _artifact_preview(output)
        if preview:
            st.markdown("**Generated test preview**")
            st.code(_truncate(preview), language="typescript")

    elif phase == "execution":
        st.markdown("**What this step should produce**")
        st.write("Playwright execution results and test artifacts.")
        stdout = output.get("stdout", "")
        stderr = output.get("stderr", "")
        if stdout:
            st.markdown("**Execution summary**")
            st.code(_truncate(stdout), language="text")
        if stderr:
            st.markdown("**Execution warnings/errors**")
            st.code(_truncate(stderr), language="text")

    elif phase == "self_healing":
        st.markdown("**What this step should produce**")
        st.write("Healing analysis, drift/flakiness classification, and optional Jira or Zephyr actions.")
        preview = _artifact_preview(output)
        if preview:
            st.markdown("**Healing output**")
            st.code(_truncate(preview), language="json")

    elif phase == "reporting":
        st.markdown("**What this step should produce**")
        st.write("A final execution report and Jira reporting updates.")
        if output.get("artifact_files"):
            st.markdown("**Recent report files**")
            for item in output["artifact_files"]:
                st.write(f"- {item}")
        preview = _artifact_preview(output)
        if preview:
            st.markdown("**Report preview**")
            st.code(_truncate(preview), language="markdown")


def _pending_review_message(state: Dict[str, Any]) -> str:
    phase = state.get("current_phase")
    if not phase:
        return "Review the current step before continuing."
    meta = PHASE_GUIDE.get(phase, {})
    return (
        f"You are reviewing `{meta.get('title', phase)}`. "
        f"Purpose: {meta.get('purpose', 'No description available.')}"
    )


def _build_orchestrator(project_key: str) -> AgentOrchestratorV2:
    return AgentOrchestratorV2(project_key=project_key, memory=AgentMemory())


def _run_without_hitl(orchestrator: AgentOrchestratorV2) -> Dict[str, Any]:
    outcome = orchestrator.run_until_pause()
    while outcome.get("status") == "waiting_for_human":
        orchestrator.apply_human_feedback("approve")
        outcome = orchestrator.run_until_pause()
    return outcome


def _persist(orchestrator: AgentOrchestratorV2) -> None:
    st.session_state["orchestrator"] = orchestrator
    st.session_state["run_state"] = orchestrator.export_state()


def _restore(project_key: str) -> Optional[AgentOrchestratorV2]:
    orchestrator = st.session_state.get("orchestrator")
    state = st.session_state.get("run_state")
    if not orchestrator or orchestrator.project_key != project_key:
        return None
    if state:
        orchestrator.hydrate_state(state)
    return orchestrator


def _apply_start_phase(orchestrator: AgentOrchestratorV2, start_phase: str) -> None:
    state = orchestrator.export_state()
    start_index = PHASE_ORDER.index(start_phase)
    state["completed"] = PHASE_ORDER[:start_index]
    state["current_phase"] = None
    state["last_output"] = None
    state["errors"] = []
    state["logs"] = []
    state["timeline"] = []
    state["pending_human"] = None
    state["status"] = "running"
    state["iteration_count"] = 0
    orchestrator.hydrate_state(state)


def _render_overview(state: Dict[str, Any]) -> None:
    st.subheader("Run Overview")
    col1, col2, col3 = st.columns(3)
    col1.metric("Current Phase", _phase_title(state.get("current_phase")))
    col2.metric("Completed Phases", len(state.get("completed", [])))
    col3.metric("Errors", len(state.get("errors", [])))

    completed = state.get("completed", [])
    if completed:
        st.write("Completed flow")
        st.write(" -> ".join(_phase_title(phase) for phase in completed))
    else:
        st.caption("No completed phases yet.")


def _render_flow_tracker(state: Dict[str, Any]) -> None:
    completed = set(state.get("completed", []))
    current = state.get("current_phase")
    waiting = bool(state.get("pending_human"))

    parts = [
        """
        <style>
        .tea-flow {
          display:flex;
          align-items:center;
          flex-wrap:wrap;
          gap:10px;
          margin: 0.5rem 0 1rem 0;
        }
        .tea-step {
          border:1px solid #d0d7de;
          background:#f6f8fa;
          color:#1f2328;
          border-radius:999px;
          padding:8px 14px;
          font-size:14px;
          font-weight:600;
        }
        .tea-step.done {
          background:#e8f5e9;
          border-color:#7fb77e;
          color:#216e39;
        }
        .tea-step.active {
          background:#fff4d6;
          border-color:#d4a72c;
          color:#9a6700;
          box-shadow:0 0 0 2px rgba(212,167,44,0.15);
        }
        .tea-step.pending {
          background:#eef2ff;
          border-color:#7c8cff;
          color:#3146b8;
        }
        .tea-arrow {
          color:#8c959f;
          font-size:18px;
          font-weight:700;
        }
        </style>
        <div class="tea-flow">
        """
    ]

    for index, phase in enumerate(PHASE_ORDER):
        classes = ["tea-step"]
        if phase in completed:
            classes.append("done")
        if phase == current:
            classes.append("pending" if waiting else "active")
        parts.append(f'<div class="{" ".join(classes)}">{_phase_title(phase)}</div>')
        if index < len(PHASE_ORDER) - 1:
            parts.append('<div class="tea-arrow">→</div>')

    parts.append("</div>")
    st.markdown("**Flow**", unsafe_allow_html=False)
    st.markdown("".join(parts), unsafe_allow_html=True)


def _render_live_view(state: Dict[str, Any]) -> None:
    st.subheader("Live Execution View")
    phase = state.get("current_phase")
    meta = PHASE_GUIDE.get(phase or "", {})
    st.write(f"Current phase: `{_phase_title(phase)}`")
    if meta:
        st.caption(f"Purpose: {meta['purpose']}")
        st.caption(f"Expected output: {meta['outputs']}")

    logs = state.get("logs", [])
    if logs:
        with st.expander("Execution log", expanded=True):
            for entry in logs[-12:]:
                st.text(f"[{entry['timestamp']}] {entry['level'].upper()}: {entry['message']}")
    else:
        st.info("The run has not produced logs yet.")

    timeline = state.get("timeline", [])
    with st.expander("Execution timeline", expanded=False):
        if timeline:
            for item in timeline[-15:]:
                st.write(
                    f"{item['timestamp']} | {item['event_type']} | "
                    f"{json.dumps(item['payload'], default=str)[:220]}"
                )
        else:
            st.caption("Timeline events will appear as the run progresses.")


def _render_latest_output(state: Dict[str, Any]) -> None:
    st.subheader("Latest Step Output")
    output = state.get("last_output")
    if not output:
        st.info("No step output yet.")
        return

    _render_phase_card(state)

    with st.expander("Raw technical output", expanded=False):
        st.json(output)


def _render_hitl(orchestrator: AgentOrchestratorV2, state: Dict[str, Any]) -> None:
    pending = state.get("pending_human")
    if not pending:
        return

    output = pending.get("output") or state.get("last_output") or {}
    phase = state.get("current_phase")

    summary_text = _phase_summary(output)
    if output.get("ok"):
        st.success(summary_text)
    else:
        st.error(summary_text)

    retry_reason = st.text_input(
        "Reason for rerun",
        key="retry_reason",
        placeholder="Optional note for why this step should run again",
        label_visibility="collapsed",
    )

    col1, col2, col3 = st.columns(3)
    if col1.button(
        "👍 Approve",
        use_container_width=True,
        help="Accept this output and continue automatically to the next step.",
    ):
        orchestrator.apply_human_feedback("approve")
        st.session_state.pop("hitl_edit_mode", None)
        outcome = orchestrator.run_until_pause()
        _persist(orchestrator)
        st.session_state["last_outcome"] = outcome
        st.rerun()
    if col2.button(
        "✏️ Edit",
        use_container_width=True,
        help="Edit this output. Your saved changes become the input for the next step.",
    ):
        st.session_state["hitl_edit_mode"] = phase
        st.rerun()
    if col3.button(
        "🔁 Rerun",
        use_container_width=True,
        help="Reject this output and rerun the same step immediately.",
    ):
        orchestrator.apply_human_feedback(
            "reject_retry",
            retry_reason=retry_reason or "Retry requested from UI",
        )
        st.session_state.pop("hitl_edit_mode", None)
        outcome = orchestrator.run_until_pause()
        _persist(orchestrator)
        st.session_state["last_outcome"] = outcome
        st.rerun()

    edit_mode = st.session_state.get("hitl_edit_mode")
    if edit_mode == phase:
        editable = EDITABLE_PHASES.get(phase)
        if editable:
            edit_label, language = editable
            preview = output.get("preview") or ""
            st.markdown(f"**Edit {edit_label}**")
            edited_text = st.text_area(
                f"{edit_label}",
                value=preview,
                height=220,
                key=f"edit_{phase}",
            )
            save_col, cancel_col = st.columns(2)
            if save_col.button("Save edit", use_container_width=True):
                orchestrator.apply_human_feedback("edit", edited_output={edit_label: edited_text})
                st.session_state.pop("hitl_edit_mode", None)
                outcome = orchestrator.run_until_pause()
                _persist(orchestrator)
                st.session_state["last_outcome"] = outcome
                st.rerun()
            if cancel_col.button("Cancel edit", use_container_width=True):
                st.session_state.pop("hitl_edit_mode", None)
                st.rerun()
            with st.expander("Current editable content", expanded=False):
                st.code(preview, language=language)
        else:
            st.info("This step does not have a direct editable artifact here. You can approve it or rerun it.")
            if st.button("Close edit mode", use_container_width=True):
                st.session_state.pop("hitl_edit_mode", None)
                st.rerun()

    with st.expander("Raw review payload", expanded=False):
        st.json(output)


def _render_results(state: Dict[str, Any]) -> None:
    summary = state.get("summary", {})
    tool_results = state.get("tool_results", [])
    bugs_created = state.get("bugs_created", [])
    errors = state.get("errors", [])

    if not any([summary, tool_results, bugs_created, errors]):
        return

    st.subheader("Run Summary")
    if summary:
        col1, col2, col3 = st.columns(3)
        col1.metric("Completed", len(summary.get("completed_phases", [])))
        col2.metric("Tool Calls", summary.get("tool_calls", 0))
        col3.metric("Bugs Created", summary.get("bugs_created", 0))

    if tool_results:
        with st.expander("Tool activity", expanded=False):
            st.json(tool_results)

    if bugs_created:
        with st.expander("Created bugs", expanded=False):
            st.json(bugs_created)

    if errors:
        with st.expander("Errors", expanded=False):
            st.json(errors)


def _sidebar_text_input(label: str, env_key: str, password: bool = False, help_text: str = "") -> str:
    return st.sidebar.text_input(
        label,
        value=os.getenv(env_key, ""),
        type="password" if password else "default",
        help=help_text,
        key=f"sidebar_{env_key}",
    )


def _apply_runtime_env(config: Dict[str, str]) -> None:
    for key, value in config.items():
        if value is None:
            continue
        os.environ[key] = value


def _render_sidebar(memory: AgentMemory) -> Dict[str, str]:
    st.sidebar.title("Dashboard")
    summary = memory.summarize_for_llm()
    stats = summary.get("stats", {})
    st.sidebar.metric("Total runs", stats.get("total_runs", 0))
    st.sidebar.metric("Failed runs", stats.get("failed_runs", 0))
    recent_runs = summary.get("recent_runs", [])
    if recent_runs:
        st.sidebar.write("Recent runs")
        for run in recent_runs[-5:]:
            st.sidebar.caption(f"{run.get('project_key')} | {run.get('status')}")

    st.sidebar.divider()
    st.sidebar.subheader("Runtime Config")

    project_key = st.sidebar.text_input(
        "Project key",
        value=os.getenv("PROJECT_KEY", "SCRUM-86"),
        help="Jira-style key used across the TEA flow, for example SCRUM-86.",
        key="sidebar_project_key",
    )

    start_phase = st.sidebar.selectbox(
        "Start phase",
        PHASE_ORDER,
        format_func=_phase_title,
        index=0,
        key="sidebar_start_phase",
    )

    llm_provider = st.sidebar.selectbox(
        "LLM provider",
        ["ollama", "openai", "claude", "gemini"],
        index=max(["ollama", "openai", "claude", "gemini"].index(os.getenv("LLM_PROVIDER", "ollama")), 0)
        if os.getenv("LLM_PROVIDER", "ollama") in ["ollama", "openai", "claude", "gemini"]
        else 0,
        key="sidebar_LLM_PROVIDER",
    )
    chat_model = _sidebar_text_input("Chat model", "CHAT_MODEL")
    model_name = _sidebar_text_input("Structured model", "MODEL_NAME")
    embedding_model = _sidebar_text_input("Embedding model", "EMBEDDING_MODEL")

    st.sidebar.caption("App under test")
    base_url = _sidebar_text_input("UI / App URL", "BASE_URL")
    admin_username = _sidebar_text_input("App username", "ADMIN_USERNAME")
    admin_password = _sidebar_text_input("App password", "ADMIN_PASSWORD", password=True)

    st.sidebar.caption("Jira / Qdrant")
    jira_base_url = _sidebar_text_input("Jira URL", "JIRA_BASE_URL")
    jira_email = _sidebar_text_input("Jira email", "JIRA_EMAIL")
    jira_api_token = _sidebar_text_input("Jira API token", "JIRA_API_TOKEN", password=True)
    jira_project_key = _sidebar_text_input("Jira project key", "JIRA_PROJECT_KEY")
    qdrant_url = _sidebar_text_input("Qdrant URL", "QDRANT_URL")

    st.sidebar.caption("Provider keys")
    ollama_host = _sidebar_text_input("Ollama host", "OLLAMA_HOST")
    openai_key = _sidebar_text_input("OpenAI API key", "OPENAI_API_KEY", password=True)
    anthropic_key = _sidebar_text_input("Anthropic API key", "ANTHROPIC_API_KEY", password=True)
    gemini_key = _sidebar_text_input("Gemini API key", "GEMINI_API_KEY", password=True)

    st.sidebar.divider()
    with st.sidebar.expander("Where MCP is used", expanded=False):
        st.write("MCP tools are available through the router for Jira, Qdrant, and DOM operations.")
        st.write("In the current guided run, the planner mostly chooses `run_phase`, so MCP tool usage is limited unless the planner selects `call_tool`.")
        st.write("When tool calls do happen, they appear under `Run Summary -> Tool activity`.")

    return {
        "PROJECT_KEY": project_key,
        "START_PHASE": start_phase,
        "LLM_PROVIDER": llm_provider,
        "CHAT_MODEL": chat_model,
        "MODEL_NAME": model_name,
        "EMBEDDING_MODEL": embedding_model,
        "BASE_URL": base_url,
        "ADMIN_USERNAME": admin_username,
        "ADMIN_PASSWORD": admin_password,
        "JIRA_BASE_URL": jira_base_url,
        "JIRA_EMAIL": jira_email,
        "JIRA_API_TOKEN": jira_api_token,
        "JIRA_PROJECT_KEY": jira_project_key,
        "QDRANT_URL": qdrant_url,
        "OLLAMA_HOST": ollama_host,
        "OPENAI_API_KEY": openai_key,
        "ANTHROPIC_API_KEY": anthropic_key,
        "GEMINI_API_KEY": gemini_key,
    }


def main() -> None:
    memory = AgentMemory()
    runtime_config = _render_sidebar(memory)
    _apply_runtime_env({k: v for k, v in runtime_config.items() if k != "START_PHASE"})

    st.title("TEA Agentic QA System")
    st.info("This UI runs the TEA flow one step at a time and pauses after each output for your decision.")

    with st.expander("How to use this UI", expanded=True):
        st.write("1. Choose or enter a TEA project key such as `SCRUM-86`.")
        st.write("2. Pick where you want the run to start.")
        st.write("3. Click `Start guided run`.")
        st.write("4. Review each phase output, then approve, edit, or retry.")
        st.write("5. Approve and Edit now auto-advance to the next review step.")
        st.write("6. Reject & Rerun now reruns the same phase automatically.")

    project_key = runtime_config.get("PROJECT_KEY", "SCRUM-86")
    start_phase = runtime_config.get("START_PHASE", PHASE_ORDER[0])

    detected_projects = _available_projects()
    if detected_projects:
        st.caption(f"Detected TEA projects: {', '.join(detected_projects)}")
    st.caption(f"Current project: `{project_key}` | Start phase: `{_phase_title(start_phase)}`")

    orchestrator = _restore(project_key) if project_key else None
    current_state = orchestrator.export_state() if orchestrator else {}
    has_pending_human = bool(current_state.get("pending_human"))

    top1, top2, top3, top4 = st.columns(4)
    if top1.button("Start guided run", use_container_width=True, disabled=not project_key):
        orchestrator = _build_orchestrator(project_key)
        _apply_start_phase(orchestrator, start_phase)
        outcome = orchestrator.run_until_pause()
        _persist(orchestrator)
        st.session_state["last_outcome"] = outcome
        st.rerun()

    if not has_pending_human:
        if top2.button(
            "Continue to next step",
            use_container_width=True,
            disabled=not project_key or not orchestrator,
        ):
            outcome = orchestrator.run_until_pause()
            _persist(orchestrator)
            st.session_state["last_outcome"] = outcome
            st.rerun()
    else:
        top2.empty()

    if top3.button(
        "Auto run without review",
        use_container_width=True,
        disabled=not project_key,
        help="Run the full TEA flow continuously and auto-approve every checkpoint.",
    ):
        orchestrator = _build_orchestrator(project_key)
        _apply_start_phase(orchestrator, start_phase)
        outcome = _run_without_hitl(orchestrator)
        _persist(orchestrator)
        st.session_state["last_outcome"] = outcome
        st.rerun()

    if top4.button("Reset run", use_container_width=True, disabled=not project_key):
        st.session_state.pop("orchestrator", None)
        st.session_state.pop("run_state", None)
        st.session_state.pop("last_outcome", None)
        st.session_state.pop("hitl_edit_mode", None)
        st.rerun()

    if not orchestrator:
        st.warning("Start a guided run to begin the step-by-step TEA flow.")
        return

    state = orchestrator.export_state()
    st.caption(f"Run ID: `{state.get('run_id', 'n/a')}` | Status: `{state.get('status', 'idle')}`")

    _render_flow_tracker(state)
    _render_overview(state)
    _render_live_view(state)
    _render_latest_output(state)
    _render_hitl(orchestrator, state)
    _render_results(state)


if __name__ == "__main__":
    main()
