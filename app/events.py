"""Per-run event bus — asyncio.Queue → SSE stream to the UI.

Each ScoutDesk analysis run gets its own ``asyncio.Queue``.  Agent coroutines
call ``bus.emit(event)``; the FastAPI SSE endpoint calls ``bus.subscribe(run_id)``
which is an async generator that yields events until the run finishes.

A sentinel value (``None``) is placed on the queue by ``finish()`` to signal
that no more events will arrive.  All emitted events are also stored in an
ordered list per run so that a late-joining client (GET /api/runs/{id}) can
replay the full history.

Thread-safety: only ``asyncio`` primitives are used — no ``threading`` locks.
All callers are expected to run in the same event loop.

Module-level singleton
----------------------
``bus = EventBus()`` is created at import time.  Import it wherever you need
to emit or subscribe::

    from app.events import bus
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from app.models import Event

# Sentinel that signals the end of a run's event stream.
_SENTINEL: None = None


class EventBus:
    """Asyncio-native, per-run event bus for SSE streaming.

    Lifecycle per run
    -----------------
    1. ``create_run(run_id)``   — called by the orchestrator before agents start.
    2. ``emit(event)``          — called by agents as they produce events.
    3. ``subscribe(run_id)``    — called by the SSE endpoint; yields events.
    4. ``finish(run_id)``       — called by the orchestrator when the run ends;
                                  causes ``subscribe`` to return naturally.

    Replay
    ------
    ``get_events(run_id)`` returns the full ordered list of events that were
    emitted for a run (excluding the sentinel).
    """

    def __init__(self) -> None:
        # run_id → asyncio.Queue[Event | None]
        self._queues: dict[str, asyncio.Queue[Event | None]] = {}
        # run_id → ordered list of all emitted events (for replay)
        self._history: dict[str, list[Event]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def create_run(self, run_id: str) -> None:
        """Register a new run.

        Must be called before any ``emit`` or ``subscribe`` calls for *run_id*.
        Calling it twice for the same id resets the queue and history, which
        is useful in tests but should not happen in production.
        """
        self._queues[run_id] = asyncio.Queue()
        self._history[run_id] = []

    # ------------------------------------------------------------------
    # Emit
    # ------------------------------------------------------------------

    async def emit(self, event: Event) -> None:
        """Put *event* onto the run's queue and append it to the history list.

        Raises ``KeyError`` if ``create_run`` was never called for the run.
        """
        run_id = event.run_id
        if run_id not in self._queues:
            raise KeyError(
                f"Unknown run_id '{run_id}'. Call create_run() before emit()."
            )
        self._history[run_id].append(event)
        await self._queues[run_id].put(event)

    # ------------------------------------------------------------------
    # Subscribe (async generator for SSE)
    # ------------------------------------------------------------------

    async def subscribe(self, run_id: str) -> AsyncIterator[Event]:
        """Async generator that yields :class:`~app.models.Event` objects.

        The generator blocks on the queue until each event arrives and stops
        naturally when ``finish(run_id)`` places the sentinel ``None`` on the
        queue.  The sentinel itself is **not** yielded.

        Usage::

            async for event in bus.subscribe(run_id):
                yield f"data: {event.model_dump_json()}\\n\\n"

        Raises ``KeyError`` if ``create_run`` was never called for *run_id*.
        """
        if run_id not in self._queues:
            raise KeyError(
                f"Unknown run_id '{run_id}'. Call create_run() before subscribe()."
            )
        queue: asyncio.Queue[Event | None] = self._queues[run_id]
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                # Put the sentinel back so additional subscribers (e.g. test
                # assertions) also receive the termination signal.
                await queue.put(_SENTINEL)
                return
            yield item

    # ------------------------------------------------------------------
    # Finish
    # ------------------------------------------------------------------

    def finish(self, run_id: str) -> None:
        """Signal that no more events will be emitted for *run_id*.

        Places the sentinel ``None`` on the queue.  ``subscribe`` will yield
        all remaining events already in the queue and then return.

        Safe to call from a synchronous context (no ``await`` needed).
        Raises ``KeyError`` if the run was never registered.
        """
        if run_id not in self._queues:
            raise KeyError(
                f"Unknown run_id '{run_id}'. Call create_run() before finish()."
            )
        # put_nowait is safe here — the sentinel is tiny and the queue is
        # unbounded by default, so this never blocks.
        self._queues[run_id].put_nowait(_SENTINEL)

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def get_events(self, run_id: str) -> list[Event]:
        """Return the full ordered list of events emitted for *run_id*.

        Returns a shallow copy so callers cannot mutate the internal list.
        Raises ``KeyError`` if the run was never registered.
        """
        if run_id not in self._history:
            raise KeyError(f"Unknown run_id '{run_id}'.")
        return list(self._history[run_id])

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def active_runs(self) -> list[str]:
        """Return sorted list of currently registered run ids."""
        return sorted(self._queues.keys())

    def event_count(self, run_id: str) -> int:
        """Return the number of events emitted for *run_id* so far."""
        return len(self._history.get(run_id, []))


# ---------------------------------------------------------------------------
# Module-level singleton — import and use everywhere
# ---------------------------------------------------------------------------

bus: EventBus = EventBus()
