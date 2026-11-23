#!/bin/sh
set -eu
/opt/brickwright/bin/pip install --disable-pip-version-check --no-index --no-deps \
    "$MAESTRO_ATTACHMENT_DIR"/wheels/*.whl
install -m 0644 "$MAESTRO_ATTACHMENT_DIR/BRICKWRIGHT.md" /app/BRICKWRIGHT.md
/opt/brickwright/bin/brickwright external instructions --flow >/dev/null
cat "$MAESTRO_ATTACHMENT_DIR/flow.md" >> /app/AGENTS.md
