#!/usr/bin/env python3
"""3944542983: short reads never seal truncated review content."""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import scoped_worker_executor as executor


def test_short_read():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw)
        tree=root/'tree'
        tree.mkdir()
        instruction=root/'instruction.json'
        instruction.write_text(json.dumps({'context_refs':[]}))
        instruction.chmod(0o600)
        cap={'allowed_paths':['.'],'prompt_artifact':{'path':str(instruction)}}
        data=('日本語-review-full-content'*11).encode('utf-8')
        (tree/'file').write_bytes(data)
        real_read=os.read
        with patch.object(executor.os,'read',side_effect=lambda fd,n:real_read(fd,min(n,3))):
            rows=executor.capture_review_context(cap,tree,['file'])
        assert rows[0]['content'].encode('utf-8')==data
        assert rows[0]['size_bytes']==len(data)
        assert rows[0]['sha256']=='sha256:'+hashlib.sha256(data).hexdigest()
        for size in (262144,262145):
            (tree/'file').write_bytes(b'x'*size)
            if size==262144:
                assert executor.capture_review_context(cap,tree,['file'])[0]['size_bytes']==size
            else:
                try: executor.capture_review_context(cap,tree,['file'])
                except executor.ScopedWorkerError as exc: assert exc.reason_class=='review_context_byte_limit'
                else: raise AssertionError('oversized file accepted')

if __name__ == '__main__':
    test_short_read()
    print(json.dumps({'result':'pass','cases':1}))
