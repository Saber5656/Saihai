#!/usr/bin/env python3
"""3944515366: completion context obeys the actual provider JSON byte bound."""
import json
import tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import scoped_worker_executor as executor
import provider_adapters


def test_serialized_context():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw)
        tree=root/'tree'
        tree.mkdir()
        paths=[str(i)+'.txt' for i in range(4)]
        instruction=root/'instruction.json'
        instruction.write_text(json.dumps({'context_refs':[]}))
        instruction.chmod(0o600)
        cap={'allowed_paths':['.'],'prompt_artifact':{'path':str(instruction)}}
        for content in ('x'*262144, '\x00'*50000):
            for name in paths: (tree/name).write_text(content)
            try:
                executor.capture_review_context(cap,tree,paths)
            except executor.ScopedWorkerError as exc:
                assert exc.reason_class=='review_context_serialized_limit',exc.reason_class
            else:
                raise AssertionError('oversized serialized context accepted')
        for name in paths: (tree/name).write_text('x'*260000)
        rows=executor.capture_review_context(cap,tree,paths)
        encoded=json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
        assert len(encoded)<=provider_adapters.MAX_CONTEXT_BYTES
        assert sum(r['size_bytes'] for r in rows)==1040000

if __name__ == '__main__':
    test_serialized_context()
    print(json.dumps({'result':'pass','cases':1}))
