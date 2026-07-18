"""Storage abstraction for raw inputs (feed XML, LLM responses, scraped MR HTML).

Blobs live on the local filesystem today. The Protocol below lets us swap the
backend for S3/GCS without touching call sites — matters if we ever host this or
need a persistent audit trail beyond GitHub Actions' 90-day artifact retention.

Keys are slash-delimited paths, e.g. `2026-07-12/feeds/slowboring.xml` or
`2026-07-12/predictions/claude-opus-4-7.json`. Backends interpret them as
filesystem paths or object keys as appropriate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class RawStoreBackend(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalFSBackend:
    """Store raw blobs as files under a root directory."""

    def __init__(self, root: Path | str = "data/raw") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put(self, key: str, data: bytes) -> None:
        self._path(key).write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
