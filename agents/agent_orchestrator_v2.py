#!/usr/bin/env python3
"""Agentic orchestrator with MCP tools, HITL pauses, and persistent memory."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

from agents.agent_memory import AgentMemory
from agents.mcp_router import MCPRouter
from llm_gateway import get_llm_gateway

REPO_ROOT = Path(__file__).resolve().parents[1]

PHASE_COMMANDS = {
    "jira_sync": [sys.executable, "jira_sync_agent.py", "--issue", "{project_key}", "--project", "{project_prefix}"],
    "dom_capture": [sys.executable, "dom_capture.py", "--project", "{project_key}"],
    "vectorize": [sys.executable, "vectorize_and_upload.py", "--project", "{project_key}"],
    "quality_alignment": [sys.executable, "quality_alignment.py", "--project", "{project_key}"],
    "step_generation": [sys.executable, "step_generator.py", "--project", "{project_key}"],
    "execution": ["npm", "run", "test"],
    "self_healing": [sys.executable, "self_healing_agent.py", "--project", "{project_key}"],
    "reporting": [sys.executable, "report_to_jira.py", "--project", "{project_key}"],
}

ALLOWED_PHASES = list(PHASE_COMMANDS.keys())
ALLOWED_TOOLS = [
    "jira.create_bug",
    "jira.add_comment",
    "jira.get_issue",
    "qdrant.vector_search",
    "qdrant.vector_upsert",
    "dom.find_element",
    "dom.get_dom_snapshot",
]

DEFAULT_STATE = {
    "completed": [],
    "last_output": None,
    "errors": [],
    "logs": [],
    "timeline": [],
    "pending_human": None,
    "current_phase": None,
    "status": "idle",
    "tool_results": [],
    "bugs_created": [],
    "reports": [],
    "summary": {},
    "last_decision": None,
    "iteration_count": 0,
}


class AgentOrchestratorV2:
    """Stepwise agentic orchestrator that wraps the existing TEA scripts."""

    def __init__(
        self,
        project_key: str,
        memory: Optional[AgentMemory] = None,
        router: Optional[MCPRouter] = None,
        max_iterations: int = 20,
        workspace: Optional[str] = None,
    ) -> None:
        self.project_key = project_key
        self.project_prefix = project_key.split("-")[0] if project_key else ""
        self.workspace = workspace or str(REPO_ROOT)
        self.memory = memory or AgentMemory()
        self.router = router or MCPRouter()
        self.gateway = get_llm_gateway()
        self.max_iterations = max_iterations
        self.run_id = f"{project_key}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.state: Dict[str, Any] = deepcopy(DEFAULT_STATE)
        self.state.update(
            {
                "run_id": self.run_id,
                "project_key": project_key,
                "status": "running",
                "started_at": datetime.utcnow().isoformat(),
            }
        )

    def export_state(self) -> Dict[str, Any]:
        return deepcopy(self.state)

    def hydrate_state(self, state: Dict[str, Any]) -> None:
        self.state = deepcopy(state)

    def _append_log(self, message: str, level: str = "info") -> None:
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": level,
            "message": message,
        }
        self.state.setdefault("logs", []).append(entry)

    def _timeline(self, event_type: str, payload: Dict[str, Any]) -> None:
        self.state.setdefault("timeline", []).append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "event_type": event_type,
                "payload": payload,
            }
        )

    def _build_tool_specs(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "decide_next_action",
                    "description": "Select the next orchestration action for the TEA run.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["run_phase", "call_tool", "ask_human", "stop"],
                            },
                            "phase": {"type": "string", "enum": ALLOWED_PHASES},
                            "tool": {"type": "string", "enum": ALLOWED_TOOLS},
                            "args": {"type": "object"},
                            "reason": {"type": "string"},
                        },
                        "required": ["action", "reason"],
                    },
                },
            }
        ]

    def _decision_prompt(self) -> str:
        memory = self.memory.summarize_for_llm()
        return (
            "You are the TEA orchestrator planner.\n"
            "Decide the next best action for this run.\n"
            "Rules:\n"
            "- Prefer running the missing next phase needed to advance the test flow.\n"
            "- Use call_tool only when inspecting Jira/Qdrant/DOM data would help.\n"
            "- Use ask_human when review or correction is needed after a step.\n"
            "- Use stop only when the run is complete or blocked.\n"
            "- Never repeat a phase already completed unless there is an error-driven retry.\n\n"
            f"Project key: {self.project_key}\n"
            f"Allowed phases: {ALLOWED_PHASES}\n"
            f"Allowed tools: {ALLOWED_TOOLS}\n"
            f"Memory summary: {json.dumps(memory)}\n"
            f"Current state: {json.dumps(self.state, default=str)}\n"
        )

    def _fallback_decision(self) -> Dict[str, Any]:
        for phase in ALLOWED_PHASES:
            if phase not in self.state.get("completed", []):
                return {
                    "action": "run_phase",
                    "phase": phase,
                    "args": {},
                    "reason": f"Fallback progression to next incomplete phase: {phase}",
                }
        return {"action": "stop", "args": {}, "reason": "All phases completed"}

    def decide_next_action(self) -> Dict[str, Any]:
        if self.state.get("iteration_count", 0) >= self.max_iterations:
            return {
                "action": "stop",
                "args": {},
                "reason": f"Reached max iterations ({self.max_iterations})",
            }

        prompt = self._decision_prompt()
        try:
            response = self.gateway.chat_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=self._build_tool_specs(),
                auto_execute=False,
            )
            tool_calls = response.get("tool_calls", [])
            if tool_calls:
                arguments = tool_calls[0].get("arguments", {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                decision = arguments
            else:
                decision = json.loads(response.get("content", "{}"))
        except Exception:
            decision = self._fallback_decision()

        if decision.get("action") not in {"run_phase", "call_tool", "ask_human", "stop"}:
            decision = self._fallback_decision()
        if decision.get("action") == "run_phase" and decision.get("phase") not in ALLOWED_PHASES:
            decision = self._fallback_decision()
        if decision.get("action") == "call_tool" and decision.get("tool") not in ALLOWED_TOOLS:
            decision = self._fallback_decision()
        return decision

    def _render_command(self, phase: str) -> List[str]:
        template = PHASE_COMMANDS[phase]
        return [
            token.format(project_key=self.project_key, project_prefix=self.project_prefix)
            for token in template
        ]

    def _collect_phase_artifacts(self, phase: str) -> Dict[str, Any]:
        docs_dir = os.path.join(self.workspace, "docs")
        inbox_dir = os.path.join(docs_dir, "inbox")
        artifact_map = {
            "jira_sync": os.path.join(docs_dir, f"requirements_{self.project_key}.json"),
            "vectorize": os.path.join(docs_dir, "requirements", f"{self.project_key}_PRD.md"),
            "quality_alignment": os.path.join(self.workspace, "tests", "features", f"{self.project_key}.feature"),
            "step_generation": os.path.join(self.workspace, "tests", "steps", f"{self.project_key}.spec.ts"),
            "reporting": os.path.join(docs_dir, "jira-reports"),
        }
        target = artifact_map.get(phase)
        payload: Dict[str, Any] = {}

        if phase == "jira_sync":
            primary = os.path.join(docs_dir, f"requirements_{self.project_key}.json")
            inbox_copy = os.path.join(inbox_dir, f"requirements_{self.project_key}.json")
            attachments_dir = os.path.join(inbox_dir, "attachments")
            if os.path.exists(primary):
                with open(primary, "r", encoding="utf-8", errors="ignore") as handle:
                    requirements_data = json.load(handle)
                payload.update(
                    {
                        "artifact_path": primary,
                        "inbox_copy_path": inbox_copy if os.path.exists(inbox_copy) else "",
                        "requirements_data": requirements_data,
                        "preview": json.dumps(
                            {
                                "target_issue_id": requirements_data.get("target_issue_id"),
                                "project_key": requirements_data.get("project_key"),
                                "story": requirements_data.get("story"),
                                "epic": requirements_data.get("epic"),
                                "related_issues": requirements_data.get("related_issues", []),
                                "attachments_parsed": requirements_data.get("attachments_parsed", 0),
                                "requirements_count": len(requirements_data.get("requirements", [])),
                            },
                            indent=2,
                        ),
                    }
                )
            if os.path.isdir(attachments_dir):
                payload["attachment_files"] = sorted(os.listdir(attachments_dir))
            return payload

        if target and os.path.exists(target):
            if os.path.isdir(target):
                return {"artifact_path": target, "artifact_files": sorted(os.listdir(target))[-5:]}
            if target.endswith((".md", ".json", ".feature", ".ts")):
                with open(target, "r", encoding="utf-8", errors="ignore") as handle:
                    content = handle.read()
                return {"artifact_path": target, "preview": content[:3000]}
        return {}

    def run_phase(self, phase: str) -> Dict[str, Any]:
        if phase not in ALLOWED_PHASES:
            return {"ok": False, "phase": phase, "error": f"Unsupported phase: {phase}"}

        command = self._render_command(phase)
        self.state["current_phase"] = phase
        self._append_log(f"Running phase '{phase}' with command: {' '.join(command)}")
        self._timeline("phase_started", {"phase": phase, "command": command})

        try:
            result = subprocess.run(
                command,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=900,
            )
            payload = {
                "ok": result.returncode == 0,
                "phase": phase,
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout[-6000:],
                "stderr": result.stderr[-3000:],
            }
            payload.update(self._collect_phase_artifacts(phase))
            if payload["ok"]:
                if phase not in self.state["completed"]:
                    self.state["completed"].append(phase)
            else:
                self.state["errors"].append({"phase": phase, "error": payload["stderr"] or payload["stdout"]})
                self.memory.record_failure(self.run_id, phase, payload["stderr"] or payload["stdout"], payload)
            self.state["last_output"] = payload
            self._timeline("phase_finished", {"phase": phase, "ok": payload["ok"]})
            return payload
        except Exception as exc:
            payload = {"ok": False, "phase": phase, "error": str(exc), "command": command}
            self.state["errors"].append({"phase": phase, "error": str(exc)})
            self.memory.record_failure(self.run_id, phase, str(exc), payload)
            self.state["last_output"] = payload
            self._timeline("phase_finished", {"phase": phase, "ok": False, "error": str(exc)})
            return payload

    def call_tool(self, tool_name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = args or {}
        result = self.router.execute_tool(tool_name, args)
        payload = {"tool": tool_name, "args": args, "result": result}
        self.state.setdefault("tool_results", []).append(payload)
        self.state["last_output"] = payload
        if tool_name == "jira.create_bug" and result.get("ok"):
            self.state.setdefault("bugs_created", []).append(result.get("data", {}))
        self._timeline("tool_called", {"tool": tool_name, "ok": result.get("ok", False)})
        return payload

    def ask_human(self, reason: str, output: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        panel = {
            "reason": reason,
            "output": output or self.state.get("last_output"),
            "editable_fields": ["PRD", "Gherkin", "Steps"],
        }
        self.state["pending_human"] = panel
        self.state["status"] = "waiting_for_human"
        self._timeline("human_requested", {"reason": reason})
        return panel

    def apply_human_feedback(
        self,
        decision: str,
        edited_output: Optional[Dict[str, Any]] = None,
        retry_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        pending = self.state.get("pending_human")
        if not pending:
            return {"ok": False, "error": "No pending human decision"}

        self.state["status"] = "running"
        self.state["pending_human"] = None
        response = {
            "ok": True,
            "decision": decision,
            "edited_output": edited_output or {},
            "retry_reason": retry_reason or "",
        }

        last_output = self.state.get("last_output")
        if decision == "approve":
            self._append_log("Human approved the latest output")
        elif decision == "edit":
            if isinstance(last_output, dict):
                last_output["human_edit"] = edited_output or {}
                artifact_path = last_output.get("artifact_path")
                if artifact_path and os.path.isfile(artifact_path):
                    replacement = self._select_edit_payload(artifact_path, edited_output or {})
                    if replacement:
                        with open(artifact_path, "w", encoding="utf-8") as handle:
                            handle.write(replacement)
                        last_output["artifact_updated"] = artifact_path
                        last_output["preview"] = replacement[:3000]
                self.state["last_output"] = last_output
            self._append_log("Human edited the latest output")
        elif decision == "reject_retry":
            if self.state.get("current_phase") and self.state["current_phase"] in self.state.get("completed", []):
                self.state["completed"].remove(self.state["current_phase"])
            self.state["errors"].append(
                {"phase": self.state.get("current_phase"), "error": retry_reason or "Human requested retry"}
            )
            self._append_log(f"Human requested retry: {retry_reason or 'no reason provided'}", level="warning")
        else:
            return {"ok": False, "error": f"Unknown human decision: {decision}"}

        self.memory.record_decision(
            self.run_id,
            {
                "human_decision": decision,
                "reason": pending.get("reason"),
                "retry_reason": retry_reason,
            },
        )
        self._timeline("human_resolved", {"decision": decision})
        return response

    @staticmethod
    def _select_edit_payload(artifact_path: str, edited_output: Dict[str, Any]) -> str:
        extension = os.path.splitext(artifact_path)[1].lower()
        if extension in {".feature"}:
            return edited_output.get("Gherkin", "")
        if extension in {".ts", ".tsx"}:
            return edited_output.get("Steps", "")
        if extension in {".md", ".txt"}:
            return edited_output.get("PRD", "")
        return next((value for value in edited_output.values() if value), "")

    def _finalize(self) -> Dict[str, Any]:
        status = "completed" if not self.state.get("errors") else "completed_with_warnings"
        if set(ALLOWED_PHASES).difference(self.state.get("completed", [])):
            status = "failed" if self.state.get("errors") else status
        self.state["status"] = status
        self.state["finished_at"] = datetime.utcnow().isoformat()
        self.state["summary"] = {
            "completed_phases": list(self.state.get("completed", [])),
            "error_count": len(self.state.get("errors", [])),
            "tool_calls": len(self.state.get("tool_results", [])),
            "bugs_created": len(self.state.get("bugs_created", [])),
        }
        self.memory.record_run(
            {
                "run_id": self.run_id,
                "project_key": self.project_key,
                "status": "failed" if status == "failed" else "completed",
                "completed_phases": list(self.state.get("completed", [])),
                "errors": self.state.get("errors", []),
                "summary": self.state.get("summary", {}),
                "started_at": self.state.get("started_at"),
                "finished_at": self.state.get("finished_at"),
            }
        )
        return self.export_state()

    def step(self) -> Dict[str, Any]:
        if self.state.get("pending_human"):
            return {
                "status": "waiting_for_human",
                "pending_human": self.state.get("pending_human"),
                "state": self.export_state(),
            }

        decision = self.decide_next_action()
        self.state["last_decision"] = decision
        self.state["iteration_count"] = self.state.get("iteration_count", 0) + 1
        self.memory.record_decision(self.run_id, {"planner_decision": decision})
        self._append_log(f"Planner decision: {json.dumps(decision)}")

        action = decision.get("action")
        if action == "run_phase":
            output = self.run_phase(decision["phase"])
            self.ask_human(decision.get("reason", f"Review output from {decision['phase']}"), output)
            return {"status": "waiting_for_human", "output": output, "state": self.export_state()}
        if action == "call_tool":
            output = self.call_tool(decision["tool"], decision.get("args", {}))
            self.ask_human(decision.get("reason", f"Review tool result for {decision['tool']}"), output)
            return {"status": "waiting_for_human", "output": output, "state": self.export_state()}
        if action == "ask_human":
            panel = self.ask_human(decision.get("reason", "Human review requested"), self.state.get("last_output"))
            return {"status": "waiting_for_human", "pending_human": panel, "state": self.export_state()}

        return {"status": "finished", "state": self._finalize()}

    def run_until_pause(self) -> Dict[str, Any]:
        while self.state.get("iteration_count", 0) < self.max_iterations:
            outcome = self.step()
            if outcome["status"] in {"waiting_for_human", "finished"}:
                return outcome
        return {"status": "finished", "state": self._finalize()}
