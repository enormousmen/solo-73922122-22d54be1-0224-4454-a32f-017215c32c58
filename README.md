# firmware-cache

Firmware CI shared cache. CI reuses a build only after **two build machines
(quorum) commit identical image + debug-symbols** for the same input key.
Any divergence between committed results disables the key permanently and
fetches answer with a rebuild hint.

## Run

```bash
docker compose up --build        # serves on http://localhost:8080
```

State lives in the `cache-data` volume (`/data`: SQLite DB + content-addressed
blobs), so disabled keys and artifacts survive restarts. Configuration via env:

| Variable               | Default   | Meaning                              |
|------------------------|-----------|--------------------------------------|
| `CACHE_DATA_DIR`       | `/data`   | where DB and blobs are stored        |
| `CACHE_MAX_FILE_BYTES` | `33554432`| per-file limit (32 MiB)              |
| `CACHE_QUORUM`         | `2`       | machines that must agree for a hit   |

## API

Build machines (one distinct `build_id` per machine):

```
PUT  /v1/keys/{key}/builds/{build}/files/image     # raw bytes, <= 32 MiB
PUT  /v1/keys/{key}/builds/{build}/files/symbols
POST /v1/keys/{key}/builds/{build}/commit          # needs both files
```

CI:

```
GET  /v1/keys/{key}                # hit | miss | disabled (+ rebuild details)
GET  /v1/keys/{key}/bundle         # one ZIP with both confirmed files
GET  /v1/keys/{key}/files/image    # single-file pickup (still supported)
GET  /v1/keys/{key}/files/symbols
```

The bundle endpoint is the recommended pickup path: it answers one request
with a single ZIP containing **both** confirmed files, so a conflicting
commit landing between two separate downloads can never leave CI with only
half of the artifact set.

Bundle responses:

| Code | Meaning                                                                 |
|------|-------------------------------------------------------------------------|
| 200  | `application/zip` with `{key}-image.bin` and `{key}-symbols.bin`; `X-Image-SHA256`, `X-Symbols-SHA256`, `X-Confirmed-By` headers |
| 404  | key unknown or still a miss (body is the key status)                   |
| 409  | key disabled (the persisted rebuild hint with both digests), or the confirmed files failed verification/changed while packaging (`status=bundle_unavailable`, `action=rebuild`, per-file `problems`) |

Interactive docs at `/docs`. See `examples/demo.sh` for a full walkthrough.

## Semantics

- **Staged uploads**: files land per `(key, build_id, kind)`; re-uploading
  from the same machine replaces the staged bytes and still counts as one
  machine. Uncommitted uploads never influence hits.
- **Commit** requires both files; afterwards the build is immutable (uploads
  → `409`, recommit is idempotent). The server hashes the actual received
  bytes (SHA-256) while streaming; hashing holds no DB transaction, so other
  keys keep committing/fetching.
- **Hit**: ≥ `quorum` committed builds with identical digests for both files.
  Fetches are evaluated in one transaction, so CI either downloads the
  confirmed artifact set or gets a miss — never a partial/torn state.
- **Atomic bundle pickup**: `GET /v1/keys/{key}/bundle` snapshots the
  confirmed digests in one transaction, builds a ZIP outside any DB
  transaction (other keys keep committing/fetching meanwhile; each file may
  still be up to 32 MiB), verifies every entry's length and SHA-256 against
  the snapshot, and only then re-checks the key in a fresh transaction.
  Missing blobs, unreadable/truncated bytes, or length/digest mismatches
  produce a `409` rebuild hint with per-file problems. If the key was
  disabled during packaging, the response is the original disabled/rebuild
  hint; once the second check approves the ZIP, it is transmitted to
  completion even if the key is disabled afterwards.
- **Mismatch**: if any two committed builds differ, the key is disabled.
  `GET /v1/keys/{key}` then returns `status=disabled`, `action=rebuild`, and
  the differing files with both sides' digests; downloads return `409`.
  The record is persisted in SQLite.

## Develop & test

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q     # includes live-server concurrency tests
CACHE_DATA_DIR=$(mktemp -d) .venv/bin/uvicorn app.main:app --port 8080 &
./examples/demo.sh http://localhost:8080
```
