"""Handles slash commands for the agent."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, List

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.utils.helpers import build_help_content, build_status_content

if TYPE_CHECKING:
    from nanobot.agent.memory import MemoryConsolidator
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus
    from nanobot.session.manager import Session, SessionManager


class CommandHandler:
    """Handles slash commands for the agent."""

    def __init__(
        self,
        bus: MessageBus,
        active_tasks: Dict[str, List[asyncio.Task[Any]]],
        subagents: SubagentManager,
        sessions: SessionManager,
        memory_consolidator: MemoryConsolidator,
        schedule_background: Callable[[Coroutine[Any, Any, Any]], None],
        get_status_params: Callable[[], Dict[str, Any]],
    ):
        self.bus = bus
        self._active_tasks = active_tasks
        self.subagents = subagents
        self.sessions = sessions
        self.memory_consolidator = memory_consolidator
        self._schedule_background = schedule_background
        self._get_status_params = get_status_params

    async def handle_queued(self, msg: InboundMessage) -> bool:
        """Handles a slash command that should be run within the processing lock.

        Returns:
            True if a command was handled, False otherwise.
        """
        cmd = msg.content.strip().lower()
        if not cmd.startswith("/") or cmd in ("/stop", "/restart"):
            return False

        if cmd == "/new":
            session = self.sessions.get_or_create(msg.session_key)
            await self.handle_new(msg, session)
            return True
        if cmd == "/status":
            session = self.sessions.get_or_create(msg.session_key)
            await self.bus.publish_outbound(await self._status_response(msg, session))
            return True
        if cmd == "/help":
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=build_help_content(),
                    metadata={"render_as": "text"},
                )
            )
            return True

        return False  # Unhandled command

    async def handle_stop(self, msg: InboundMessage) -> None:
        """Handles the /stop command by canceling active tasks for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(
            OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)
        )

    async def handle_restart(self, msg: InboundMessage) -> None:
        """Handles the /restart command by restarting the process in-place."""
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content="Restarting..."
            )
        )

        async def _do_restart() -> None:
            await asyncio.sleep(1)
            # Use -m nanobot instead of sys.argv[0] for Windows compatibility.
            os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

        asyncio.create_task(_do_restart())

    async def handle_new(self, msg: InboundMessage, session: "Session") -> None:
        """Handles the /new command by clearing the session."""
        snapshot = session.messages[session.last_consolidated :]
        session.clear()
        self.sessions.save(session)
        self.sessions.invalidate(session.key)

        if snapshot:
            self._schedule_background(self.memory_consolidator.archive_messages(snapshot))

        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="New session started.",
            )
        )

    async def _status_response(self, msg: InboundMessage, session: "Session") -> OutboundMessage:
        """Builds an outbound status message for a given session."""
        ctx_est = 0
        try:
            ctx_est, _ = await self.memory_consolidator.estimate_session_prompt_tokens(
                session
            )
        except Exception:
            pass

        status_params = self._get_status_params()
        if ctx_est <= 0:
            ctx_est = status_params.get("last_usage", {}).get("prompt_tokens", 0)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=build_status_content(
                **status_params,
                session_msg_count=len(session.get_history(max_messages=0)),
                context_tokens_estimate=ctx_est,
            ),
            metadata={"render_as": "text"},
        )
