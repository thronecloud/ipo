"""
Pluggable analysis backend.

The engine calls one interface; the implementation is chosen by ANALYSIS_BACKEND:
  - "cli": shells out to the Max-plan `claude` CLI (default today)
  - "api": Anthropic API + Fable 5 (wired later — one flag flip, no engine change)

Each backend returns (result_dict, meta_dict) on success or (None, error_str) on failure.
"""

import json
import os
import subprocess

from src.personas import ANALYSIS_JSON_SCHEMA


class AnalysisBackend:
    name = "base"

    def analyze(self, system_prompt: str, user_prompt: str, model: str):
        raise NotImplementedError


class CliBackend(AnalysisBackend):
    """Wraps `claude -p ... --json-schema` on the Max plan (no API key)."""

    name = "cli"

    def __init__(self, timeout: int = 300):
        self.timeout = timeout
        self._schema = json.dumps(ANALYSIS_JSON_SCHEMA)

    def analyze(self, system_prompt, user_prompt, model):
        cmd = [
            "claude", "-p",
            "--no-session-persistence",
            "--model", model,
            "--tools", "",
            "--system-prompt", system_prompt,
            "--output-format", "json",
            "--json-schema", self._schema,
            user_prompt,
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.timeout, start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return None, f"CLI timeout ({self.timeout}s)"
        except Exception as e:  # pragma: no cover
            return None, f"CLI spawn error: {e}"

        if r.returncode != 0:
            return None, f"CLI exit {r.returncode}: {r.stderr[:200]}"
        if not r.stdout.strip():
            return None, "empty stdout from CLI"
        try:
            resp = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            return None, f"JSON parse error: {e}"

        if resp.get("is_error"):
            return None, f"claude error: {resp.get('result', 'unknown')}"
        result = resp.get("structured_output")
        if not result:
            return None, "no structured_output in response"

        # Detect the model that actually ran (Fable can reroute refusals to Opus 4.8).
        model_usage = resp.get("modelUsage") or {}
        model_used = next(iter(model_usage), model) if model_usage else model
        meta = {
            "duration_ms": resp.get("duration_ms"),
            "total_cost_usd": resp.get("total_cost_usd"),
            "usage": resp.get("usage"),
            "model_requested": model,
            "model_used": model_used,
        }
        return result, meta


class ApiBackend(AnalysisBackend):
    """Anthropic API + Fable 5 — not wired yet (see roadmap Phase 1/deploy)."""

    name = "api"

    def analyze(self, system_prompt, user_prompt, model):
        raise NotImplementedError(
            "API backend not implemented yet. Set ANALYSIS_BACKEND=cli to use the Max-plan CLI."
        )


def get_backend() -> AnalysisBackend:
    choice = os.environ.get("ANALYSIS_BACKEND", "cli").lower()
    if choice == "api":
        return ApiBackend()
    return CliBackend()


def default_model() -> str:
    return os.environ.get("ANALYSIS_MODEL", "claude-fable-5")
