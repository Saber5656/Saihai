#!/usr/bin/env python3
"""3944515362: missing parents mean deleted; symlink parents still fail closed."""
import json
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import scoped_worker_executor as executor


def test_deleted_parent():
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        tree = root/'tree'
        tree.mkdir()
        instruction=root/'instruction.json'
        instruction.write_text(json.dumps({'context_refs':[]}))
        instruction.chmod(0o600)
        cap={'allowed_paths':['nested'],'prompt_artifact':{'path':str(instruction)}}
        for path in ('nested/file', 'nested/deeper/file'):
            rows=executor.capture_review_context(cap,tree,[path])
            assert rows==[dict(path=path,size_bytes=0,sha256=executor.sha256_digest('deleted'),content='',deleted=True)]
        outside=root/'outside'
        outside.mkdir()
        (tree/'nested').symlink_to(outside,target_is_directory=True)
        try:
            executor.capture_review_context(cap,tree,['nested/file'])
        except executor.ScopedWorkerError as exc:
            assert exc.reason_class=='review_context_unavailable'
        else:
            raise AssertionError('symlink directory classified as deletion')

if __name__ == '__main__':
    test_deleted_parent()
    print(json.dumps({'result':'pass','cases':1}))
