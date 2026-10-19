import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from weakref import WeakValueDictionary

from app.core.errors import AppError
from app.schemas.dictation import DictationResponse

_Key = tuple[str, str]  # (author_id, idempotency key)


class IdempotencyConflictError(AppError):
    status_code = 409
    code = "idempotency_conflict"


@dataclass(frozen=True)
class _Entry:
    fingerprint: Hashable
    response: DictationResponse
    expires_at: float


class IdempotencyCache:
    """Remembers successful dictation responses so a mobile retry doesn't append twice.

    In-memory and single-process by design: retries arrive within seconds, so losing the
    cache on restart is acceptable. Only successes are stored; failures may be retried.
    """

    def __init__(
        self,
        ttl_s: float,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[_Key, _Entry] = OrderedDict()
        self._locks: WeakValueDictionary[_Key, asyncio.Lock] = WeakValueDictionary()

    def lock(self, author_id: str, key: str) -> asyncio.Lock:
        """Per-key lock: a retry that arrives mid-flight waits for the first attempt's result."""
        k = (author_id, key)
        lock = self._locks.get(k)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[k] = lock
        return lock

    def get(self, author_id: str, key: str, fingerprint: Hashable) -> DictationResponse | None:
        k = (author_id, key)
        entry = self._entries.get(k)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[k]
            return None
        if entry.fingerprint != fingerprint:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used for a different dictation"
            )
        return entry.response

    def put(
        self, author_id: str, key: str, fingerprint: Hashable, response: DictationResponse
    ) -> None:
        k = (author_id, key)
        self._entries[k] = _Entry(fingerprint, response, self._clock() + self._ttl_s)
        self._entries.move_to_end(k)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
