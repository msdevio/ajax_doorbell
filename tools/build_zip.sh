#!/usr/bin/env bash
# Build zip/ajax_doorbell_ha.zip for a manual install: unzip it into Home
# Assistant's config/ folder (custom_components/ and blueprints/ land in the
# right places; the integration serves the intercom card itself).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
OUT="zip/ajax_doorbell_ha.zip"
mkdir -p zip
rm -f "$OUT"
python3 - "$OUT" <<'PY'
import pathlib, sys, zipfile
out = sys.argv[1]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for path in sorted(pathlib.Path("custom_components/ajax_doorbell").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            z.write(path, path.as_posix())
    for path in sorted(pathlib.Path("blueprints").rglob("*.yaml")):
        z.write(path, path.as_posix())
print(out, sum(i.file_size for i in zipfile.ZipFile(out).infolist()), "bytes unpacked")
PY
