"""Explicit test-only temporary Vault preparation; never used by production."""
import json
from pathlib import Path
import vault_task_records as vault


def prepare(state_root: Path) -> Path:
    state_root = Path(state_root).resolve()
    root = state_root / 'fixture-vault'
    for path in (state_root / 'requests').glob('*.json'):
        try: record = json.loads(path.read_text())
        except (ValueError, OSError): continue
        task = record.get('task_id', '')
        if not vault.discovery.TASK_ID_RE.fullmatch(task): continue
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = root / '01-Projects' / 'Fixtures' / task / 'task.md'
        if not target.exists():
            vault.scaffold(root, task, project='Fixtures', brief=dict(objective='Fixture execution',
                scope='Temporary fixture task only', acceptance_criteria='Expected test outcome'))
            target.chmod(0o600)
            for parent in target.parents:
                parent.chmod(0o700)
                if parent == root: break
    vault.canonical_root = lambda: root
    return root
