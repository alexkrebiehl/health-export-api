"""Serialising and caching expensive coverage renders.

Editing a dashboard card's URL fires one request per keystroke, so a burst of
near-identical requests arrives back to back. Two mechanisms handle that:

* A **gate** that runs one coverage computation at a time. Coverage rendering
  is pure Python and therefore GIL-bound, so running several at once does not
  go faster — it goes dramatically slower. Measured against a 4km box: eight
  requests took 1.96s run one after another and 70.9s run together, burning
  208s of CPU for about 2s of actual work. Past a queue depth the gate sheds
  load instead of letting the pile-up grow without bound.

* A **TTL cache** keyed on the coverage filters *only*. Presentation options
  (line weight, zoom control, attribution) are not part of the key, so
  re-rendering the same area with a different line weight is instant, and a
  dashboard open on several screens computes once.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from contextlib import contextmanager
from time import monotonic
from typing import Any, Callable, Iterator

DEFAULT_CACHE_TTL_SECONDS = 300.0
DEFAULT_MAX_QUEUE = 10

# Typing in a URL bar generates a distinct key per keystroke, so the cache has
# to be bounded or it would grow with every edit.
_MAX_ENTRIES = 32


class QueueFull(Exception):
    """Too many requests already waiting — shed this one rather than queue it."""


class RequestGate:
    """Runs one guarded operation at a time, with a bounded waiting room."""

    def __init__(self, *, max_queue: int = DEFAULT_MAX_QUEUE) -> None:
        self._max_queue = max_queue
        self._turnstile = threading.Lock()  # only one computation at a time
        self._counter_lock = threading.Lock()  # protects _pending
        self._pending = 0

    @property
    def pending(self) -> int:
        """Requests currently running or waiting to run."""
        with self._counter_lock:
            return self._pending

    @contextmanager
    def enter(self) -> Iterator[None]:
        """Wait for a turn, or raise :class:`QueueFull` if the queue is full."""
        with self._counter_lock:
            if self._pending >= self._max_queue:
                raise QueueFull
            self._pending += 1
        try:
            with self._turnstile:
                yield
        finally:
            with self._counter_lock:
                self._pending -= 1


class TTLCache:
    """A small, thread-safe, time-bounded LRU cache.

    A ``ttl`` of zero or less disables caching entirely, which keeps the
    "switch it off" path a configuration value rather than a code branch at
    every call site.
    """

    def __init__(
        self,
        *,
        ttl: float,
        max_entries: int = _MAX_ENTRIES,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, key: Any) -> Any | None:
        if not self.enabled:
            return None
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: Any, value: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
