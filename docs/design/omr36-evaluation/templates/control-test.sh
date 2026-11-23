#!/bin/sh
set -eu
mkdir -p /logs/verifier
if /opt/brickwright/bin/python - <<'PY'
import json
from pathlib import Path
result = json.loads(Path('/logs/artifacts/review-dispatch.json').read_bytes())
assert result['view'] in {'end', 'face'}
left, top, right, bottom = result['box']
assert 0 <= left < right <= 1100 and 0 <= top < bottom <= 800
for key in ['observation', 'cross_view', 'preserve']:
    assert isinstance(result[key], str) and result[key].strip()
PY
then
    printf '1\n' > /logs/verifier/reward.txt
else
    printf '0\n' > /logs/verifier/reward.txt
fi
