"""Original PR132 findings: historical display and nonblocking queue reads."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py"
spec = importlib.util.spec_from_file_location("pr132_builder", BUILDER)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

class OriginalFindingsTests(unittest.TestCase):
    def test_historical_models_survive_registry_change_without_authorizing_execution(self):
        old = dict(role_id="gate-prompt-formatter", event_type="agent_dispatch",
                   result="provider_response_ready", provider="anthropic",
                   intended_model="claude-sonnet-4-6", effective_model="claude-sonnet-4-6",
                   usage_source="claude_cli_json", duration_sec=2, input_tokens=10,
                   output_tokens=5, total_tokens=15, task_id="historical-task",
                   ts="2026-08-30T00:00:00Z")
        new = dict(old, provider="openai", intended_model="gpt-5.6-luna",
                   effective_model="gpt-5.6-luna", usage_source="codex_exec_json")
        with mock.patch.object(builder, "role_agent_row_for", side_effect=AssertionError("history must not read live routing")):
            rows = builder.gate_latency_summary_rows([old, new])
            self.assertEqual(sum(r["sample_count"] for r in rows), 2)
            self.assertEqual(sum(r["total_tokens_total"] for r in rows), 30)
            self.assertEqual(builder.metric_effective_model(old), "claude-sonnet-4-6")
        _, errors = builder.bind_canonical_provider_evidence(
            dict(agent_id="gate-prompt-formatter", provider="openai", primary_model="gpt-5.6-luna"), old)
        self.assertTrue(errors, "current activation must still reject mismatched routing")
        self.assertEqual(builder.metric_effective_model(dict(old, effective_model="gpt-5.6-luna")), "")


if __name__ == "__main__":
    unittest.main()
