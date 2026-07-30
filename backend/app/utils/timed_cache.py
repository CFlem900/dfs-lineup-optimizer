"""Thread-safe, TTL-based in-memory cache.

Replaces the three near-identical cache implementations in
lineup_optimizer_service.py with a single reusable class.
"""
import threading
import time
import copy
from typing import Dict, Generic, Optional, TypeVar, List, Tuple

T = TypeVar("T")

class TimedCache(Generic[T]):
    """Simple TTL cache with thread-safe operations and deep-copy on retrieval."""

    def __init__(self, ttl_seconds: int, name: str = "cache"):
        self._data: Dict[str, Tuple[float, T]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._name = name

    def get(self, key: str, deep_copy: bool = True) -> Optional[T]:
        """Return cached value or None if expired/missing."""
        with self._lock:
            if key in self._data:
                cached_at, value = self._data[key]
                if time.time() - cached_at < self._ttl:
                    return copy.deepcopy(value) if deep_copy else value
                del self._data[key]
            return None

    def set(self, key: str, value: T) -> None:
        """Store a value with the current timestamp."""
        with self._lock:
            self._data[key] = (time.time(), value)

    def delete(self, key: str) -> bool:
        """Remove a key. Returns True if it existed."""
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self) -> int:
        """Remove all entries. Returns count of removed items."""
        with self._lock:
            count = len(self._data)
            self._data.clear()
            return count

    def has(self, key: str) -> bool:
        """Check if a non-expired entry exists for key."""
        with self._lock:
            if key in self._data:
                cached_at, _ = self._data[key]
                if time.time() - cached_at < self._ttl:
                    return True
                del self._data[key]
            return False

    def __len__(self) -> int:
        """Return count of entries (including possibly expired ones)."""
        with self._lock:
            return len(self._data)

    def __contains__(self, key: str) -> bool:
        return self.has(key)
