#!/usr/bin/env python3
"""Persistent run memory for the agentic orchestrator."""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional


class AgentMemory:
    """JSON-backed memory store for past runs, failures, and decisions."""

    def __init__(self, memory_dir: str = "docs/agent-memory") -> None:
        self.memory_dir = memory_dir
        self.memory_file = os.path.join(memory_dir, "memory.json")
        os.makedirs(memory_dir, exist_ok=True)
        if not os.path.exists(self.memory_file):
            self._write(
                {
                    "runs": [],
                    "decisions": [],
                    "failures": [],
                    "stats": {"total_runs": 0, "failed_runs": 0},
                }
            )

    def _read(self) -> Dict[str, Any]:
        with open(self.memory_file, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, data: Dict[str, Any]) -> None:
        with open(self.memory_file, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def snapshot(self) -> Dict[str, Any]:
        return self._read()

    def recent_runs(self, limit: int = 5) -> List[Dict[str, Any]]:
        return self._read().get("runs", [])[-limit:]

    def recent_failures(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self._read().get("failures", [])[-limit:]

    def recent_decisions(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self._read().get("decisions", [])[-limit:]

    def record_run(self, run_record: Dict[str, Any]) -> None:
        data = self._read()
        runs = data.setdefault("runs", [])
        runs.append(run_record)
        stats = data.setdefault("stats", {"total_runs": 0, "failed_runs": 0})
        stats["total_runs"] = len(runs)
        if run_record.get("status") == "failed":
            stats["failed_runs"] = stats.get("failed_runs", 0) + 1
        self._write(data)

    def record_decision(self, run_id: str, decision: Dict[str, Any]) -> None:
        data = self._read()
        decisions = data.setdefault("decisions", [])
        decisions.append(
            {
                "run_id": run_id,
                "timestamp": datetime.utcnow().isoformat(),
                **decision,
            }
        )
        self._write(data)

    def record_failure(
        self,
        run_id: str,
        phase: str,
        error: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        data = self._read()
        failures = data.setdefault("failures", [])
        failures.append(
            {
                "run_id": run_id,
                "phase": phase,
                "error": error,
                "context": context or {},
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        self._write(data)

    def summarize_for_llm(self) -> Dict[str, Any]:
        data = self._read()
        return {
            "stats": data.get("stats", {}),
            "recent_runs": self.recent_runs(3),
            "recent_failures": self.recent_failures(5),
            "recent_decisions": self.recent_decisions(5),
        }

