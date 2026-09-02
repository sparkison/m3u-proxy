import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from models import EventType
from stream_manager import StreamManager


class CapturingEventManager:
    def __init__(self):
        self.events = []

    async def emit_event(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_emit_event_resolves_connection_idle_types():
    """The connection-idle leak detector emits these two names; they must resolve
    to real EventType members so the event actually reaches subscribers."""
    manager = StreamManager()
    em = CapturingEventManager()
    manager.event_manager = em

    await manager._emit_event("CONNECTION_IDLE_ERROR", "stream-1", {"client_id": "c1"})
    await manager._emit_event(
        "CONNECTION_IDLE_WARNING", "stream-1", {"client_id": "c1"}
    )

    assert [e.event_type for e in em.events] == [
        EventType.CONNECTION_IDLE_ERROR,
        EventType.CONNECTION_IDLE_WARNING,
    ]


@pytest.mark.asyncio
async def test_emit_event_drops_unknown_type_without_emitting(caplog):
    """An unknown event name is logged once and never emitted, rather than being
    swallowed by the generic except in the middle of building the event."""
    manager = StreamManager()
    em = CapturingEventManager()
    manager.event_manager = em

    with caplog.at_level("ERROR"):
        await manager._emit_event("NOT_A_REAL_EVENT", "stream-1", {})

    assert em.events == []
    assert any(
        "Unknown event type: NOT_A_REAL_EVENT" in r.message for r in caplog.records
    )
