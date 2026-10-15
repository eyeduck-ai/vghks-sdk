"""Bounded, borrowed JSON snapshots for one immutable ZIP analysis."""

from __future__ import annotations

import sys
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, BinaryIO

from .bundle import BundleReader, safe_member

_MAX_CACHE_BYTES = 32 * 1024 * 1024
_MAX_ENTRY_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_ENTRIES = 64


def _size(value: Any, limit: int) -> int:
    """Count retained JSON containers/scalars, stopping once the entry is too big."""
    pending = [value]
    seen = set()
    total = 0
    while pending:
        item = pending.pop()
        total += sys.getsizeof(item)
        if total > limit:
            return total
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            if isinstance(item, dict):
                # Counting shared scalars more than once is conservative and
                # avoids retaining an ID for every manifest key/value.
                total += sum(map(sys.getsizeof, item))
                if total > limit:
                    return total
                pending.extend(item.values())
            else:
                pending.extend(item)
    return total


class _AnalysisReader(BundleReader):
    """Internal read-only view; callers retain the original fresh-value API.

    Owns only the cache, never the borrowed ZIP handle. Directory/custom readers
    bypass caching so file changes and path checks retain their normal behavior.
    """

    def __init__(
        self, source: BundleReader, *, max_bytes: int = _MAX_CACHE_BYTES,
        max_entry_bytes: int = _MAX_ENTRY_BYTES, max_entries: int = _MAX_ENTRIES,
    ) -> None:
        self._source = source
        self._archive = getattr(source, "archive", None)
        self._zip_snapshot = type(source) is BundleReader and isinstance(self._archive, zipfile.ZipFile)
        self._max_bytes = max(0, min(max_bytes, _MAX_CACHE_BYTES))
        self._max_entry_bytes = max(0, min(max_entry_bytes, _MAX_ENTRY_BYTES))
        self._max_entries = max(0, min(max_entries, _MAX_ENTRIES))
        self._cache: OrderedDict[tuple[str, str], tuple[Any, int]] = OrderedDict()
        self._cache_bytes = 0

    @property
    def names(self) -> set[str]:
        return self._source.names

    @property
    def path(self) -> Path:
        return self._source.path

    @property
    def archive(self) -> zipfile.ZipFile | None:
        return self._archive

    def open(self, name: str) -> BinaryIO:
        return self._source.open(name)

    def read(self, name: str) -> bytes:
        return self._source.read(name)

    def json(self, name: str) -> Any:
        return self._load("json", name)

    def jsonl(self, name: str) -> list[dict[str, Any]]:
        if name not in self.names:
            return []
        return self._load("jsonl", name)

    def _load(self, kind: str, name: str) -> Any:
        # Only a normal, already validated ZIP has immutable member snapshots.
        # Delegate all failures and all custom/directory-reader behavior.
        if not self._zip_snapshot or not self._max_bytes or not self._max_entries:
            return getattr(self._source, kind)(name)
        name = safe_member(name)
        key = (kind, name)
        if key in self._cache:
            value, _ = self._cache[key]
            self._cache.move_to_end(key)
            return value
        value = getattr(self._source, kind)(name)
        if self._archive.getinfo(name).file_size > _MAX_SOURCE_BYTES:
            return value
        size = _size(value, min(self._max_bytes, self._max_entry_bytes))
        size += sys.getsizeof(key) + sys.getsizeof(name)
        if size > min(self._max_bytes, self._max_entry_bytes):
            return value
        while self._cache and (
            self._cache_bytes + size > self._max_bytes or len(self._cache) >= self._max_entries
        ):
            _, (_, removed) = self._cache.popitem(last=False)
            self._cache_bytes -= removed
        self._cache[key] = (value, size)
        self._cache_bytes += size
        return value

    def close(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0
