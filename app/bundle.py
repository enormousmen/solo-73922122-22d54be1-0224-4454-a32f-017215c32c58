"""Atomic bundle packaging: both confirmed files into one ZIP.

The packaging reads *only* from the content-addressed blob store and never
holds a database transaction, so while a (potentially up to 64 MiB) ZIP is
being assembled, every key keeps accepting uploads/commits and serving
fetches. Every entry is verified against the digests recorded in the
metadata snapshot before the ZIP is advertised as ready; any problem is
surfaced as :class:`BundleIntegrityError` carrying a rebuild hint.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile

# Chosen to stay small relative to the 32 MiB per-file limit while keeping
# read()/write() syscall counts reasonable.
_COPY_CHUNK = 1024 * 1024


class BundleIntegrityError(Exception):
    """A bundle could not be assembled from intact, confirmed files."""

    def __init__(self, problems: list[dict]):
        super().__init__(f"bundle integrity check failed: {problems}")
        self.problems = problems


def build_bundle(
    tmp_dir: str,
    blob_path,
    files: dict,
    key: str,
) -> tuple[str, int]:
    """Build a ZIP containing every required file of one confirmed set.

    ``files`` maps kind -> {"sha256", "size"} (the snapshot taken inside the
    metadata transaction). ``blob_path(sha256)`` resolves a digest to a path
    in the content-addressed store.

    Returns ``(zip_tmp_path, zip_size)``. On any failure the partial temp
    file is removed and :class:`BundleIntegrityError` is raised with one
    problem record per offending file.
    """
    problems: list[dict] = []
    fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="bundle-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for kind, meta in files.items():
                expected_sha = meta["sha256"]
                expected_size = meta["size"]
                entry = f"{key}-{kind}.bin"
                src = blob_path(expected_sha)
                if not os.path.isfile(src):
                    problems.append(
                        {
                            "file": kind,
                            "issue": "missing",
                            "expected_sha256": expected_sha,
                        }
                    )
                    continue
                actual_size = 0
                hasher = hashlib.sha256()
                read_failed = None
                try:
                    info = zipfile.ZipInfo(entry)
                    info.compress_type = zipfile.ZIP_STORED
                    with zf.open(info, "w", force_zip64=True) as out, open(
                        src, "rb"
                    ) as inp:
                        while True:
                            chunk = inp.read(_COPY_CHUNK)
                            if not chunk:
                                break
                            actual_size += len(chunk)
                            hasher.update(chunk)
                            out.write(chunk)
                except OSError as exc:
                    read_failed = str(exc)

                if read_failed is not None:
                    problems.append(
                        {
                            "file": kind,
                            "issue": "unreadable",
                            "detail": read_failed,
                            "expected_sha256": expected_sha,
                        }
                    )
                    continue
                actual_sha = hasher.hexdigest()
                if actual_size != expected_size:
                    problems.append(
                        {
                            "file": kind,
                            "issue": "size_mismatch",
                            "expected_size": expected_size,
                            "actual_size": actual_size,
                            "expected_sha256": expected_sha,
                        }
                    )
                elif actual_sha != expected_sha:
                    problems.append(
                        {
                            "file": kind,
                            "issue": "digest_mismatch",
                            "expected_sha256": expected_sha,
                            "actual_sha256": actual_sha,
                        }
                    )
        if problems:
            raise BundleIntegrityError(problems)
        return tmp_path, os.path.getsize(tmp_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
