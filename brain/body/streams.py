"""The stream type every Body stream method returns, and fan-out of one source to many subscribers.

Every body stream is a `Subscription`: a bounded queue filled by the adapter (from the link, or from a
producer task started with `spawn_stream`) and emptied by one consumer. It is not a Python generator,
because a generator cannot be closed while another task is waiting on it, and "switch the microphone
off" comes from another task.

Drop policy, the same for every stream: when the queue is full the **oldest** item is dropped and
`dropped` goes up by one. Fresh sensor data is worth more than stale (as in the protocol's body-side
queue, protocol/v0.md section 7), so a slow consumer never blocks the adapter and never receives a
stale backlog. Queue sizes: events 256, poses 16, microphone chunks 64 (about 5 s). For audio a drop
also shows as a gap in `seq`, which the body keeps counting.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from .. import obs
from ..limits import AUDIO_QUEUE as AUDIO_QUEUE  # re-exported: the queue bounds live in brain/limits.py (H-11)
from ..limits import EVENT_QUEUE as EVENT_QUEUE
from ..limits import POSE_QUEUE as POSE_QUEUE
from ..obs.supervisor import run_with_restarts

log = logging.getLogger("brain.body.streams")
_DROP_REPORTED_AT = (1, 10, 100, 1000, 10_000, 100_000)  # a queue.drop event at these drop counts only


class BodyStream[T](Protocol):
    """What every stream method of a Body returns: an async iterator that must be closed.

    Iterate with `async for`; call `aclose()` when done (or use `contextlib.aclosing`). Closing is how
    the body learns nobody is listening any more (for example, to switch the microphone off), and it
    may be called from any task: a consumer waiting in `async for` then simply ends.
    The iteration ends by itself only when the body is closed. `dropped` counts the items this
    consumer lost by being too slow (the oldest are dropped first).
    """

    dropped: int

    def __aiter__(self) -> BodyStream[T]: ...

    async def __anext__(self) -> T: ...

    async def aclose(self) -> None: ...


class Subscription[T]:
    """One consumer's view of a Fanout."""

    def __init__(self, maxsize: int, on_close: Callable[[Subscription[T]], None] | None = None, *,
                 name: str = "stream"):
        self.name = name
        self._reported_drops = 0
        if isinstance(maxsize, bool) or not isinstance(maxsize, int):
            raise TypeError(f"a stream queue size must be a whole number, not {type(maxsize).__name__}")
        if maxsize < 1:
            raise ValueError("a stream queue holds at least one item")
        self._items: deque[T] = deque()
        self._maxsize = maxsize
        self._on_close = on_close
        self._wake = asyncio.Event()
        self._ended = False
        self.dropped = 0  # items lost because this consumer was too slow

    def _push(self, item: T) -> None:
        if self._ended:
            return
        if len(self._items) >= self._maxsize:
            self._items.popleft()
            self.dropped += 1
            if self.dropped in _DROP_REPORTED_AT:
                obs.emit("queue.drop", f"{self.name}: a slow consumer lost its oldest items", stream=self.name,
                         dropped=self.dropped, new_drops=self.dropped - self._reported_drops, bound=self._maxsize)
                self._reported_drops = self.dropped
        self._items.append(item)
        self._wake.set()

    def _end(self) -> None:
        self._ended = True
        self._wake.set()

    def __aiter__(self) -> Subscription[T]:
        return self

    async def __anext__(self) -> T:
        while not self._items:
            if self._ended:
                raise StopAsyncIteration
            self._wake.clear()
            await self._wake.wait()
        return self._items.popleft()

    async def aclose(self) -> None:
        if not self._ended and self._on_close is not None:
            self._on_close(self)
        self._items.clear()
        self._end()

    # What an adapter's producer uses.

    @property
    def closed(self) -> bool:
        return self._ended

    def push(self, item: T) -> None:
        """Queue one item for the consumer, dropping the oldest if the queue is full."""
        self._push(item)


def spawn_stream[T](maxsize: int, produce: Callable[[Subscription[T]], Coroutine[Any, Any, None]],
                    tasks: set[asyncio.Task[None]] | None = None, *, name: str = "stream") -> Subscription[T]:
    """A stream fed by its own producer task. `produce(stream)` pushes items until it returns (the
    stream then ends) or the consumer closes the stream (the task is cancelled). Must be called with
    an event loop running. `tasks`, if given, holds the task while it runs so the body can cancel
    every producer when it closes.

    The producer is supervised (brain/obs/supervisor.py): if it raises, the failure is reported and it
    is restarted with a bounded backoff (the consumer sees a gap, not an end); after too many failures in
    the window the stream ends and the failure is reported."""
    task: asyncio.Task[None] | None = None

    def on_close(_: Subscription[T]) -> None:
        if task is not None:
            task.cancel()

    stream: Subscription[T] = Subscription(maxsize, on_close, name=name)

    def finished(done: asyncio.Task[None]) -> None:
        stream._end()  # also when the task was cancelled before it ever ran
        obs.emit("body.stream", f"{name} stream closed", stream=name, open=False, dropped=stream.dropped)
        if not done.cancelled() and done.exception() is not None:
            log.error("a stream producer failed: %r", done.exception())

    async def supervised() -> None:
        await run_with_restarts(f"stream:{name}", lambda: produce(stream), quiet=True)

    obs.emit("body.stream", f"{name} stream opened", stream=name, open=True, bound=maxsize)
    task = asyncio.create_task(supervised())
    task.add_done_callback(finished)
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return stream


class Fanout[T]:
    """Publishes each item to every current subscriber. Items published before a subscription began
    are not replayed to it."""

    def __init__(self, name: str = "events") -> None:
        self.name = name
        self._subs: list[Subscription[T]] = []
        self._closed = False

    def __len__(self) -> int:
        return len(self._subs)

    def subscribe(self, maxsize: int = EVENT_QUEUE) -> Subscription[T]:
        sub: Subscription[T] = Subscription(maxsize, self._remove, name=self.name)
        if self._closed:
            sub._end()
        else:
            self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription[T]) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, item: T) -> None:
        for sub in list(self._subs):
            sub._push(item)

    def close(self) -> None:
        """End every subscription (after it has delivered what is already queued) and refuse new ones."""
        self._closed = True
        for sub in self._subs:
            sub._end()
        self._subs.clear()
