"""Event bus.

Events represent state transitions that actually happened. Nothing in Redstone
may publish ``build.completed`` because it intends to build, or
``preview.ready`` because it started a server: the publisher must have observed
the transition first. The future frontend renders these directly, so a
fabricated event becomes a lie on screen.

Payloads carry identifiers and small scalars. File contents, prompts, model
output and anything credential-shaped are excluded, because events are logged
and streamed to browsers.

Standard library only, synchronous, in-process. A queue or broker can be
introduced behind this interface when there is more than one process.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ..domain.models import EventType, new_id, utcnow

__all__ = ["Event", "EventBus"]

# Keys never allowed in an event payload, whatever the publisher intends.
_FORBIDDEN_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "token", "secret", "password",
     "credential", "prompt", "content", "source", "response"}
)

MAX_PAYLOAD_VALUE_LENGTH = 500


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    type: EventType
    project_id: str | None = None
    task_id: str | None = None
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


def _sanitise(payload: dict | None) -> dict:
    """Drop forbidden keys and truncate long values.

    Enforced here rather than trusting each publisher, because a single careless
    ``publish(..., {"prompt": ...})`` would otherwise stream a full prompt to
    every subscriber.
    """
    if not payload:
        return {}

    clean: dict = {}
    for key, value in payload.items():
        if not isinstance(key, str) or key.lower() in _FORBIDDEN_KEYS:
            continue
        if isinstance(value, str) and len(value) > MAX_PAYLOAD_VALUE_LENGTH:
            value = value[:MAX_PAYLOAD_VALUE_LENGTH] + "…"
        if isinstance(value, (str, int, float, bool, type(None), list, tuple)):
            clean[key] = list(value) if isinstance(value, tuple) else value
    return clean


class EventBus:
    """In-process publish/subscribe with a bounded history."""

    def __init__(self, history_size: int = 500) -> None:
        self._subscribers: list[Callable[[Event], None]] = []
        self._history: deque[Event] = deque(maxlen=history_size)
        self._lock = threading.Lock()

    def subscribe(self, handler: Callable[[Event], None]) -> Callable[[], None]:
        """Register a handler. Returns a callable that unsubscribes it."""
        with self._lock:
            self._subscribers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._subscribers:
                    self._subscribers.remove(handler)

        return unsubscribe

    def publish(
        self,
        event_type: EventType,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        payload: dict | None = None,
    ) -> Event:
        event = Event(
            id=new_id("evt"),
            type=event_type,
            project_id=project_id,
            task_id=task_id,
            payload=_sanitise(payload),
        )

        with self._lock:
            self._history.append(event)
            handlers = list(self._subscribers)

        for handler in handlers:
            # One broken subscriber must not stop the others or abort the
            # operation that published the event.
            try:
                handler(event)
            except Exception:
                continue

        return event

    def history(self, project_id: str | None = None) -> tuple[Event, ...]:
        with self._lock:
            events: Iterable[Event] = tuple(self._history)
        if project_id is None:
            return tuple(events)
        return tuple(event for event in events if event.project_id == project_id)

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
