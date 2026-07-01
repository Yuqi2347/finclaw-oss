from __future__ import annotations

import inspect
import threading

from backend.services.sessions import chat_session_store


class AgentContinuationService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False

    def kick(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _run(self) -> None:
        try:
            while True:
                event = chat_session_store.next_pending_event()
                if event is None:
                    return
                try:
                    if str(event["event_type"] or "") == "memory.profile_compress":
                        from backend.services.long_term_memory import long_term_memory_service

                        result = long_term_memory_service.compress_profile_window(session_id=event["session_id"])
                        status = "done" if result.get("success") else "failed"
                        chat_session_store.mark_event(event["event_id"], status)
                        continue

                    if str(event["event_type"] or "").startswith("memory."):
                        from backend.services.memory_hook import trigger_memory_hook

                        payload = event["payload"] or {}
                        result = trigger_memory_hook(
                            event["session_id"],
                            after_message_id=payload.get("after_message_id"),
                            trigger_reason=str(event["event_type"] or "memory_hook"),
                        )
                        status = "done" if result.get("success") else "failed"
                        chat_session_store.mark_event(event["event_id"], status)
                        continue

                    from backend.core.agent_loop import agent_loop

                    result = agent_loop.run_continuation(
                        event["session_id"],
                        event["event_type"],
                        event["payload"],
                    )
                    if inspect.isgenerator(result):
                        raise RuntimeError("run_continuation returned a generator; continuation did not execute")
                    if result == "busy":
                        chat_session_store.mark_event(event["event_id"], "pending")
                        return
                    chat_session_store.mark_event(event["event_id"], "done")
                except Exception:
                    chat_session_store.mark_event(event["event_id"], "failed")
        finally:
            with self._lock:
                self._running = False


continuation_service = AgentContinuationService()
