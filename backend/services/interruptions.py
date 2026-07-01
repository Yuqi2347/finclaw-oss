from __future__ import annotations

from backend.services.cancellation import cancellation_store
from backend.services.continuation import continuation_service
from backend.services.observability import trace_store
from backend.services.sessions import chat_session_store


class InterruptionService:
    def interrupt(self, session_id: str, message: str, run_id: str | None = None) -> dict[str, str]:
        clean = message.strip()
        if not clean:
            raise ValueError("interrupt message is empty")

        cancellation_store.request_cancel(session_id, run_id)
        event_id = chat_session_store.add_event(
            session_id,
            "user.interrupt",
            {
                "message": clean,
                "cancelled_run_id": run_id,
            },
            priority=5,
        )
        trace_id = trace_store.start_trace(
            session_id,
            run_id or f"interrupt-{event_id}",
            "user_interrupt",
            {"message": clean, "event_id": event_id},
        )
        trace_store.event(trace_id, "interrupt.queued", {"event_id": event_id, "message": clean})
        trace_store.finish_trace(trace_id)
        continuation_service.kick()
        return {"status": "interrupt_queued", "event_id": str(event_id)}


interruption_service = InterruptionService()
