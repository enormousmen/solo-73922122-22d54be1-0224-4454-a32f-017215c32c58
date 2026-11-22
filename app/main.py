"""FastAPI surface for the firmware CI shared cache."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import bundle as bundle_mod
from .config import Settings, load_settings
from .store import (
    REQUIRED_KINDS,
    BuildCommittedError,
    IncompleteBuildError,
    Store,
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _check_name(value: str, what: str) -> str:
    if not _NAME_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"invalid {what}: must match {_NAME_RE.pattern}",
        )
    return value


def _check_kind(kind: str) -> str:
    if kind not in REQUIRED_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown file kind {kind!r}; expected one of {REQUIRED_KINDS}",
        )
    return kind


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.data_dir, quorum=settings.quorum)
    app = FastAPI(title="firmware-cache", version="1.0.0")
    app.state.store = store
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.put("/v1/keys/{key}/builds/{build}/files/{kind}")
    async def upload_file(key: str, build: str, kind: str, request: Request):
        _check_name(key, "input key")
        _check_name(build, "build id")
        _check_kind(kind)
        limit = settings.max_file_bytes

        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds {limit} bytes limit",
            )

        # Stream to a temp file while hashing; no DB transaction is held here,
        # so other keys keep committing/fetching during the upload.
        fd, tmp_path = tempfile.mkstemp(dir=store.tmp_dir, prefix="upload-")
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=f"file exceeds {limit} bytes limit",
                        )
                    hasher.update(chunk)
                    out.write(chunk)
            sha256 = hasher.hexdigest()
            await asyncio.to_thread(store.adopt_blob, tmp_path, sha256)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

        try:
            record = await asyncio.to_thread(
                store.stage_file, key, build, kind, sha256, size
            )
        except BuildCommittedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return record

    @app.post("/v1/keys/{key}/builds/{build}/commit")
    def commit_build(key: str, build: str):
        _check_name(key, "input key")
        _check_name(build, "build id")
        try:
            return store.commit_build(key, build)
        except IncompleteBuildError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "incomplete build",
                    "missing": exc.missing,
                    "required": list(REQUIRED_KINDS),
                },
            ) from exc

    @app.get("/v1/keys/{key}")
    def key_status(key: str):
        _check_name(key, "input key")
        return store.key_status(key)

    @app.get("/v1/keys/{key}/builds/{build}")
    def build_detail(key: str, build: str):
        _check_name(key, "input key")
        _check_name(build, "build id")
        detail = store.build_detail(key, build)
        if detail is None:
            raise HTTPException(status_code=404, detail="unknown build")
        return detail

    @app.get("/v1/keys/{key}/files/{kind}")
    def download_file(key: str, kind: str):
        _check_name(key, "input key")
        _check_kind(kind)
        status, path = store.hit_file(key, kind)
        if status["status"] == "disabled":
            return JSONResponse(status_code=409, content=status)
        if path is None:
            return JSONResponse(status_code=404, content=status)
        sha = status["files"][kind]["sha256"]
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=f"{key}-{kind}.bin",
            headers={"X-SHA256": sha, "ETag": sha},
        )

    @app.get("/v1/keys/{key}/bundle")
    def download_bundle(key: str, background: BackgroundTasks):
        """Atomic pickup: one ZIP with both confirmed files.

        The send decision is made twice around packaging: once before (do not
        even start unless the key is a confirmed hit) and again once the ZIP
        is fully assembled and verified. A key disabled in between still gets
        the original disabled/rebuild hint; once the second check approved the
        ZIP, it is transmitted to completion. No DB transaction is held while
        the ZIP is built, so other keys keep committing/fetching.
        """
        _check_name(key, "input key")

        snapshot = store.bundle_snapshot(key)
        if snapshot is None:
            # Unknown/missed keys answer 404; disabled keys keep their
            # original rebuild hint with 409, mirroring single-file GETs.
            probe = store.key_status(key)
            code = 409 if probe["status"] == "disabled" else 404
            return JSONResponse(status_code=code, content=probe)

        files = snapshot["files"]
        try:
            zip_path, zip_size = bundle_mod.build_bundle(
                store.tmp_dir, store.blob_path, files, key
            )
        except bundle_mod.BundleIntegrityError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "input_key": key,
                    "status": "bundle_unavailable",
                    "action": "rebuild",
                    "reason": "confirmed files failed verification",
                    "problems": exc.problems,
                },
            )

        background.add_task(_safe_remove, zip_path)

        # ZIP is ready: only now decide whether it may be sent.
        changed = store.recheck_bundle(key, files)
        if changed is not None:
            if changed["status"] == "disabled":
                return JSONResponse(status_code=409, content=changed)
            return JSONResponse(
                status_code=409,
                content={
                    "input_key": key,
                    "status": "bundle_unavailable",
                    "action": "rebuild",
                    "reason": "confirmed files changed while packaging",
                },
            )

        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=f"{key}-bundle.zip",
            headers={
                "Content-Length": str(zip_size),
                "X-Image-SHA256": files["image"]["sha256"],
                "X-Symbols-SHA256": files["symbols"]["sha256"],
                "X-Confirmed-By": ",".join(snapshot["confirmed_by"]),
            },
        )

    return app


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


app = create_app()
