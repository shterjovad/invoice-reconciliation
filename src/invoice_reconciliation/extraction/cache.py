"""The response cache: saved raw Bedrock responses, and credential-free replay.

Each invoice gets one file, named by its ``file_id``, under
``tests/fixtures/bedrock_responses/``:

.. code-block::

    tests/fixtures/bedrock_responses/
    ├── clean.json
    ├── wrong-price.json
    ├── duplicate.json
    └── missing-reference.json

A cache entry is a metadata envelope plus the unmodified ``raw_response``
from the SDK::

    {
        "file_id": "clean",
        "image_sha256": "<sha256 of the image bytes>",
        "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "captured_at": "2026-10-06T00:00:00+00:00",
        "raw_response": { ... }
    }

``parser.parse`` reads only ``raw_response``, so a replayed entry is
indistinguishable from a live one to every downstream caller. These files
are **committed fixture data**, not local state: a reviewer with no AWS
access depends on them to run the whole flow.

**Self-verification, not deduplication.** The filename is ``file_id``,
never a content hash, so the directory stays readable — a reviewer can see
at a glance that every invoice has a saved response. The recorded
``image_sha256`` is what makes each entry self-verifying: ``cache.get``
compares it against the hash of the image bytes in hand before returning
anything. ``clean.png`` and ``duplicate.png`` are byte-identical (the same
invoice, received twice) and legitimately share one ``image_sha256`` across
two different cache files — this is not a collision to resolve, since the
cache keys by ``file_id``, not by hash.

**On a hash mismatch** — someone regenerated the image and the saved
response no longer describes it — ``cache.get`` logs a warning naming both
hashes and raises ``CacheMissError``. It never returns the stale response.
A silent stale read is the one failure replay must never hide.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "CacheEntry",
    "CacheMissError",
    "ResponseCache",
    "DEFAULT_CACHE_DIR",
    "image_sha256",
]

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("tests/fixtures/bedrock_responses")


class CacheMissError(Exception):
    """No usable cache entry exists for a ``file_id``.

    Raised when no file is saved for that ``file_id``, when the saved file
    cannot be parsed, or when the stored ``image_sha256`` does not match
    the hash of the image bytes in hand. The three cases are distinguished
    in the message; the caller always treats them as "replay cannot
    proceed for this invoice", not as a value to fall back from.
    """


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One saved response, with the metadata envelope that verifies it.

    ``raw_response`` is the unmodified SDK response body — the same shape
    ``extraction.parser.parse`` already accepts from a live call.
    """

    file_id: str
    image_sha256: str
    model_id: str
    captured_at: str
    raw_response: dict


def image_sha256(image_bytes: bytes) -> str:
    """Return the SHA-256 hex digest of ``image_bytes``."""
    return hashlib.sha256(image_bytes).hexdigest()


class ResponseCache:
    """Reads and writes one JSON file per invoice, under ``cache_dir``."""

    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
        self.cache_dir = Path(cache_dir)

    def _path(self, file_id: str) -> Path:
        return self.cache_dir / f"{file_id}.json"

    def get(self, file_id: str, image_bytes: bytes) -> CacheEntry:
        """Return the saved entry for ``file_id``, verified against ``image_bytes``.

        Raises ``CacheMissError`` when:

        - no file exists for this ``file_id``;
        - the file exists but is not valid JSON, or is missing a required
          envelope key;
        - the stored ``image_sha256`` does not match the hash of
          ``image_bytes`` — the saved response no longer describes the
          image in hand. This case is **logged as a warning, naming both
          hashes**, before the error is raised. It never falls back to
          returning the stale entry.
        """
        path = self._path(file_id)
        if not path.exists():
            raise CacheMissError(f"no cache entry for file_id {file_id!r} at {path}")

        try:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as exc:
            raise CacheMissError(
                f"cache entry for file_id {file_id!r} at {path} is not valid JSON: {exc}"
            ) from exc

        required_keys = ("file_id", "image_sha256", "model_id", "captured_at", "raw_response")
        missing = [key for key in required_keys if key not in raw]
        if missing:
            raise CacheMissError(
                f"cache entry for file_id {file_id!r} at {path} is missing key(s): {missing}"
            )

        stored_hash = raw["image_sha256"]
        actual_hash = image_sha256(image_bytes)
        if stored_hash != actual_hash:
            logger.warning(
                "cache entry for file_id %r is stale: stored image_sha256=%s "
                "does not match the image in hand (sha256=%s); failing this "
                "invoice rather than returning a response for different content",
                file_id,
                stored_hash,
                actual_hash,
            )
            raise CacheMissError(
                f"cache entry for file_id {file_id!r} is stale: stored "
                f"image_sha256={stored_hash} != actual {actual_hash}"
            )

        return CacheEntry(
            file_id=raw["file_id"],
            image_sha256=raw["image_sha256"],
            model_id=raw["model_id"],
            captured_at=raw["captured_at"],
            raw_response=raw["raw_response"],
        )

    def put(
        self,
        file_id: str,
        image_bytes: bytes,
        model_id: str,
        raw_response: dict,
    ) -> CacheEntry:
        """Save ``raw_response`` (unmodified) for ``file_id``, keyed by the image hash.

        Overwrites any existing file for this ``file_id`` — this is the
        write side of ``--refresh-cache``. ``captured_at`` is recorded as
        the current UTC time in ISO-8601 form.
        """
        entry = CacheEntry(
            file_id=file_id,
            image_sha256=image_sha256(image_bytes),
            model_id=model_id,
            captured_at=datetime.now(timezone.utc).isoformat(),
            raw_response=raw_response,
        )

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(file_id)
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "file_id": entry.file_id,
                    "image_sha256": entry.image_sha256,
                    "model_id": entry.model_id,
                    "captured_at": entry.captured_at,
                    "raw_response": entry.raw_response,
                },
                f,
                indent=2,
                sort_keys=False,
            )
            f.write("\n")

        return entry
