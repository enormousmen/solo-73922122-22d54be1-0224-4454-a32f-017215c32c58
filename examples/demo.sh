#!/usr/bin/env bash
# Demo: two builders agree -> cache hit; CI fetches both confirmed files in
# ONE atomic bundle (ZIP) download; a third uploading in parts does not
# disturb fetches; a diverging commit disables the key and bundle pickup
# answers with the original rebuild hint.
#
# Usage:  ./examples/demo.sh [BASE_URL]      (default http://localhost:8080)
set -euo pipefail

BASE="${1:-http://localhost:8080}"
KEY="demo-fw-1.2.3-$$"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pp() {  # pretty-print JSON if a formatter is available
  if command -v jq >/dev/null; then jq; else cat; fi
}
upload() { # key build kind file
  curl -sS -X PUT "$BASE/v1/keys/$1/builds/$2/files/$3" \
    -H 'Content-Type: application/octet-stream' --data-binary @"$4"
}
commit() { curl -sS -X POST "$BASE/v1/keys/$1/builds/$2/commit"; }
status() { curl -sS "$BASE/v1/keys/$1"; }

echo "### inputs: one image + one debug-symbols file"
head -c 1048576 /dev/urandom > "$WORK/image.bin"
head -c  262144 /dev/urandom > "$WORK/symbols.bin"

echo; echo "### builder-1 uploads both files and commits"
upload "$KEY" builder-1 image   "$WORK/image.bin";   echo
upload "$KEY" builder-1 symbols "$WORK/symbols.bin"; echo
commit "$KEY" builder-1 | pp
echo "--- key status (only 1 of 2 machines in):"
status "$KEY" | pp

echo; echo "### builder-2 uploads the identical files and commits"
upload "$KEY" builder-2 image   "$WORK/image.bin";   echo
upload "$KEY" builder-2 symbols "$WORK/symbols.bin"; echo
commit "$KEY" builder-2 | pp
echo "--- key status (two machines agree):"
status "$KEY" | pp

echo; echo "### CI picks up the confirmed set in ONE bundle download"
code=$(curl -sS -o "$WORK/bundle.zip" -w '%{http_code}' \
  "$BASE/v1/keys/$KEY/bundle")
echo "HTTP $code, $(stat -c %s "$WORK/bundle.zip") bytes"
python3 - "$WORK" "$KEY" <<'PY'
import sys, zipfile, pathlib
work = pathlib.Path(sys.argv[1])
key = sys.argv[2]
with zipfile.ZipFile(work / "bundle.zip") as zf:
    assert zf.testzip() is None
    zf.extractall(work / "unpacked")
for kind in ("image", "symbols"):
    original = work / f"{kind}.bin"
    got = work / "unpacked" / f"{key}-{kind}.bin"
    assert original.read_bytes() == got.read_bytes(), kind
    print(f"{kind} OK")
PY

echo; echo "### builder-3 uploads IN PARTS (staged, not committed)"
head -c 262144 /dev/urandom > "$WORK/other-symbols.bin"
upload "$KEY" builder-3 image "$WORK/image.bin"; echo
echo "--- key status (uncommitted upload must not affect the hit):"
status "$KEY" | pp
curl -sS -o "$WORK/dl2-image.bin" "$BASE/v1/keys/$KEY/files/image"
cmp "$WORK/image.bin" "$WORK/dl2-image.bin" && echo "fetch still works"

echo; echo "### builder-3 commits a DIFFERENT symbols file"
upload "$KEY" builder-3 symbols "$WORK/other-symbols.bin"; echo
commit "$KEY" builder-3 | pp
echo "--- key status (key disabled, rebuild hinted, both digests listed):"
status "$KEY" | pp
echo "--- bundle pickup attempt now (original disabled/rebuild hint):"
code=$(curl -sS -o "$WORK/fetch.json" -w '%{http_code}' \
  "$BASE/v1/keys/$KEY/bundle")
pp < "$WORK/fetch.json"
echo "HTTP $code"
