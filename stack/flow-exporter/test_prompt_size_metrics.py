"""Unit tests for prompt_size_metrics.py — plain unittest, no external deps.

Run from flow-exporter/:  python3 -m unittest test_prompt_size_metrics -v
"""
import json
import os
import tempfile
import unittest

import prompt_size_metrics as psm

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "testdata", "prompt-size-orchestrator.json")

SAMPLE_CONFIG = """\
model:
  default: deepseek/deepseek-v4-flash-0731
skills:
  external_dirs: []
  template_vars: true
  disabled:
    - airtable
    - claude-design
    - codex
    - design-md
  platform_disabled:
    telegram:
      - gif-search
keepalive: true
"""


class TestParsePromptSize(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE) as f:
            cls.raw = json.load(f)
        cls.rows = psm.parse_prompt_size(cls.raw)

    def test_real_fixture_parses(self):
        self.assertTrue(self.rows)

    def test_system_prompt_block(self):
        row = next(r for r in self.rows if r[0] == "hermes_prompt_size_bytes"
                   and r[1]["block"] == "system_prompt")
        self.assertEqual(row[2], self.raw["system_prompt"]["bytes"])

    def test_tool_schemas_block(self):
        row = next(r for r in self.rows if r[0] == "hermes_prompt_size_bytes"
                   and r[1]["block"] == "tool_schemas")
        self.assertEqual(row[2], self.raw["tools"]["json_bytes"])

    def test_tool_count(self):
        row = next(r for r in self.rows if r[0] == "hermes_prompt_tools_total")
        self.assertEqual(row[2], self.raw["tools"]["count"])

    def test_sections_map_to_slugs(self):
        slugs = {r[1]["section"] for r in self.rows
                 if r[0] == "hermes_prompt_section_bytes"}
        self.assertEqual(slugs, {"stable", "context", "volatile"})

    def test_names_are_dot_free(self):
        for name, _labels, _val in self.rows:
            self.assertNotIn(".", name, f"{name} would be dropped by Prometheus")

    def test_empty_payload_is_safe(self):
        rows = psm.parse_prompt_size({})
        self.assertTrue(rows)
        self.assertEqual(rows[0][2], 0.0)


class TestReadDisabledSkills(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(SAMPLE_CONFIG)

    def tearDown(self):
        os.unlink(self.path)

    def test_global_list(self):
        names = psm.read_disabled_skills(self.path)
        self.assertIn("codex", names)
        self.assertIn("design-md", names)
        self.assertNotIn("gif-search", names)  # platform-scoped, not global

    def test_missing_file_is_empty(self):
        self.assertEqual(psm.read_disabled_skills("/nonexistent/x.yaml"), set())

    def test_empty_disabled(self):
        with open(self.path, "w") as f:
            f.write("skills:\n  disabled: []\n")
        self.assertEqual(psm.read_disabled_skills(self.path), set())

    def test_inline_json_array_form(self):
        # `hermes config set skills.disabled` writes a quoted JSON/Python list.
        with open(self.path, "w") as f:
            f.write('skills:\n  disabled: \'["codex", "airtable"]\'\n')
        self.assertEqual(psm.read_disabled_skills(self.path), {"codex", "airtable"})


if __name__ == "__main__":
    unittest.main()