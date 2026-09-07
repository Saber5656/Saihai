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
    def test_fifo_is_rejected_without_hanging_and_normal_queue_read_continues(self):
        code = """
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fifo_builder', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
root = Path(sys.argv[2]); (root/'tasks/probe').mkdir(parents=True)
os.mkfifo(root/'tasks/probe/existing.yaml')
try:
    m.queue_file_exists(root, 'tasks/probe/existing.yaml', 'task')
except ValueError:
    pass
else:
    raise AssertionError('FIFO accepted')
(root/'tasks/probe/normal.yaml').write_text('normal')
assert m.queue_file_exists(root, 'tasks/probe/normal.yaml', 'task')
"""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, "-c", code, str(BUILDER), tmp], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

if __name__ == "__main__":
    unittest.main()
