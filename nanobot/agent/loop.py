# pylint: disable=protected-access, import-outside-toplevel, unnecessary-dunder-call, trailing-newlines
"""Core processing engine for the agent, responsible for orchestrating the main loop.

This module defines the `AgentLoop` class, which is the central component of the
nanobot agent. It orchestrates the entire process of receiving messages,
interacting with the language model, executing tools, and sending responses.


This module defines the `AgentLoop` class, which is the central component of the
nanobot agent. It orchestrates the entire process of receiving messages,
interacting with the language model, executing tools, and sending responses.

The `AgentLoop` is responsible for:
- Managing the main event loop and gracefully handling shutdown signals.
- Consuming inbound messages from the message bus.
- Dispatching messages to dedicated processing tasks to maintain responsiveness.
- Handling system-level commands like `/stop`, `/restart`, and `/status`.
- Building the context for the language model, including history, memory, and skills.
- Calling the language model and processing its responses.
- Executing tool calls and incorporating the results into the conversation.
- Managing session history and memory consolidation.
- Interfacing with sub-agents and other external services.

The module is designed to be highly asynchronous, leveraging `asyncio` to handle
concurrent operations and I/O-bound tasks efficiently. It follows a modular
and extensible architecture, allowing for the easy integration of new tools,
channels, and providers.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from loguru import logger

from nanobot import __version__
from nanobot.agent.commands import CommandHandler
from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryConsolidator
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.skills import SkillsLoader, BUILTIN_SKILLS_DIR
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from nanobot.agent.tools.mcp import connect_mcp_servers
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ExecToolConfig, WebSearchConfig
from nanobot.providers.base import LLMProvider, ToolCall
from nanobot.session.manager import Session, SessionManager
from nanobot.utils.helpers import build_status_content, build_help_content

if TYPE_CHECKING:
    from nanobot.config.schema import (
        ChannelsConfig,
    )
    from nanobot.cron.service import CronService


class AgentLoop:
    """The core processing engine of the nanobot agent.

    This class orchestrates the main loop of the agent, handling message
    processing, interaction with the language model, tool execution, and
    session management.

    Attributes:
        bus: An instance of `MessageBus` for communication with other components.
        provider: An instance of a `LLMProvider` for interacting with the language model.
        workspace: The root directory of the agent's workspace.
        model: The name of the language model to use.
        max_iterations: The maximum number of tool call iterations per turn.
        context_window_tokens: The context window size of the language model.
        web_search_config: Configuration for the web search tool.
        web_proxy: The proxy to use for web requests.
        exec_config: Configuration for the shell execution tool.
        cron_service: An instance of `CronService` for scheduled tasks.
        restrict_to_workspace: Whether to restrict file system access to the workspace.
        context: An instance of `ContextBuilder` for building the LLM context.
        sessions: An instance of `SessionManager` for managing user sessions.
        tools: An instance of `ToolRegistry` for managing available tools.
        subagents: An instance of `SubagentManager` for managing sub-agents.
        memory_consolidator: An instance of `MemoryConsolidator` for managing memory.
    """

    _TOOL_RESULT_MAX_CHARS: int = 16_000

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: Optional[str] = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: Optional[WebSearchConfig] = None,
        web_proxy: Optional[str] = None,
        exec_config: Optional[ExecToolConfig] = None,
        cron_service: Optional[CronService] = None,
        restrict_to_workspace: bool = False,
        session_manager: Optional[SessionManager] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        channels_config: Optional[ChannelsConfig] = None,
    ) -> None:
        """Initializes the AgentLoop.

        Args:
            bus: The message bus for communication.
            provider: The language model provider.
            workspace: The agent's workspace directory.
            model: The name of the language model to use.
            max_iterations: Max tool call iterations per turn.
            context_window_tokens: The context window size of the LLM.
            web_search_config: Configuration for the web search tool.
            web_proxy: Proxy for web requests.
            exec_config: Configuration for the shell execution tool.
            cron_service: The cron service for scheduled tasks.
            restrict_to_workspace: Restrict file system access to the workspace.
            session_manager: The session manager.
            mcp_servers: Configuration for MCP servers.
            channels_config: Configuration for the channels.
        """
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: Dict[str, int] = {}

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_search_config=self.web_search_config,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=self.restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: Optional[AsyncExitStack] = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: Dict[str, List[asyncio.Task[Any]]] = {}
        self._background_tasks: List[asyncio.Task[Any]] = []
        self._processing_lock = asyncio.Lock()
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
        )

        self.command_handler = CommandHandler(
            bus=self.bus,
            active_tasks=self._active_tasks,
            subagents=self.subagents,
            sessions=self.sessions,
            memory_consolidator=self.memory_consolidator,
            schedule_background=self._schedule_background,
            get_status_params=self._get_status_params,
        )

        self._register_default_tools()

    def _get_status_params(self) -> Dict[str, Any]:
        """Returns a dictionary of parameters for the status message."""
        return {
            "version": __version__,
            "model": self.model,
            "start_time": self._start_time,
            "last_usage": self._last_usage,
            "context_window_tokens": self.context_window_tokens,
        }

    def _register_default_tools(self) -> None:
        """Registers the default set of tools available to the agent."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self.tools.register(
            ReadFileTool(
                workspace=self.workspace,
                allowed_dir=allowed_dir,
                extra_allowed_dirs=extra_read,
            )
        )
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        if self.exec_config.enable:
            self.tools.register(
                ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    path_append=self.exec_config.path_append,
                )
            )
        self.tools.register(
            WebSearchTool(config=self.web_search_config, proxy=self.web_proxy)
        )
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connects to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error(f"Failed to connect MCP servers (will retry on next message): {e}")
            if self._mcp_stack:
                try:
                    await self._mcp_stack.__aexit__(None, None, None)
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self, channel: str, chat_id: str, message_id: Optional[str] = None
    ) -> None:
        """Updates the context for all tools that require routing information."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(
                        channel, chat_id, *([message_id] if name == "message" else [])
                    )

    @staticmethod
    def _strip_think(text: Optional[str]) -> Optional[str]:
        """Removes <think>...</think> blocks from the given text.

        Args:
            text: The text to process.

        Returns:
            The text with the <think> blocks removed, or None if the input is None.
        """
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: List[ToolCall]) -> str:
        """Formats a list of tool calls into a concise hint string.

        For example: 'web_search("query")'.

        Args:
            tool_calls: A list of `ToolCall` objects.

        Returns:
            A string representing a concise hint of the tool calls.
        """

        def _fmt(tc: ToolCall) -> str:
            args = (
                (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments)
                or {}
            )
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}...")' if len(val) > 40 else f'{tc.name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: List[Dict[str, Any]],
        on_progress: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> Tuple[Optional[str], List[str], List[Dict[str, Any]]]:
        """Runs the main agent iteration loop.

        This method repeatedly calls the language model, executes tool calls,
        and builds the conversation history until a final response is generated
        or the maximum number of iterations is reached.

        Args:
            initial_messages: The initial list of messages to start the loop with.
            on_progress: An optional callback to report progress during the loop.

        Returns:
            A tuple containing:
            - The final content of the agent's response.
            - A list of the names of the tools used during the loop.
            - The complete list of messages in the conversation history.
        """
        messages: List[Dict[str, Any]] = initial_messages
        iteration = 0
        final_content: Optional[str] = None
        tools_used: List[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            tool_defs = self.tools.get_definitions()

            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=tool_defs,
                model=self.model,
            )
            usage = response.usage or {}
            self._last_usage = {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            }

            if response.has_tool_calls:
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    tool_hint = self._tool_hint(response.tool_calls)
                    tool_hint = self._strip_think(tool_hint)
                    if tool_hint:
                        await on_progress(tool_hint, tool_hint=True)

                tool_call_dicts = [
                    tc.to_openai_tool_call() for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(
                        tool_call.name, tool_call.arguments
                    )
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history, as they can
                # poison the context and cause permanent 400 loops.
                if response.finish_reason == "error":
                    logger.error(f"LLM returned error: {(clean or '')[:200]}")
                    final_content = (
                        clean or "Sorry, I encountered an error calling the AI model."
                    )
                    break
                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning(f"Max iterations ({self.max_iterations}) reached")
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Runs the main agent loop, dispatching messages as tasks.

        This method continuously consumes messages from the inbound message bus
        and dispatches them to the `_dispatch` method for processing. It also
        handles system-level commands like `/stop` and `/restart`.
        """
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                task = asyncio.current_task()
                # Preserve real task cancellation so shutdown can complete cleanly.
                if not self._running or (task and task.cancelling()):
                    raise
                continue
            except Exception as e:
                logger.warning(f"Error consuming inbound message: {e}, continuing...")
                continue

            if await self.command_handler.handle(msg):
                continue

            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(msg.session_key, []).append(task)
            task.add_done_callback(
                lambda t, k=msg.session_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Dispatches a message to the `_process_message` method under a global lock.

        This ensures that only one message is being processed at a time.

        Args:
            msg: The inbound message to process.
        """
        async with self._processing_lock:
            try:
                if await self.command_handler.handle(msg):
                    return

                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=msg.metadata or {},
                        )
                    )
            except asyncio.CancelledError:
                logger.info(f"Task cancelled for session {msg.session_key}")
                raise
            except Exception:
                logger.exception(
                    f"Error processing message for session {msg.session_key}"
                )
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    )
                )

    async def close_mcp(self) -> None:
        """Drains pending background archives, then closes MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await self._mcp_stack.__aexit__(None, None, None)
            except Exception:
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def _schedule_background(self, coro: Awaitable[Any]) -> None:
        """Schedules a coroutine as a tracked background task.

        The task is drained on shutdown.

        Args:
            coro: The coroutine to schedule.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(lambda _t: self._background_tasks.remove(task))

    def stop(self) -> None:
        """Stops the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: Optional[str] = None,
        on_progress: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> Optional[OutboundMessage]:
        """Processes a single inbound message and returns the response.

        This is the main message processing method, which handles everything
        from slash commands to running the agent loop.

        Args:
            msg: The inbound message to process.
            session_key: The session key to use, if different from the message.
            on_progress: An optional callback to report progress.

        Returns:
            An `OutboundMessage` to be sent back to the user, or None.
        """
        if msg.channel == "system":
            return await self._process_system_message(msg)

        preview = (
            f"{msg.content[:80]}..." if len(msg.content) > 80 else msg.content
        )
        logger.info(
            f"Processing message from {msg.channel}:{msg.sender_id}: {preview}"
        )

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        if await self.command_handler.handle(msg):
            return None

        await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        self._set_tool_context(
            msg.channel, msg.chat_id, msg.metadata.get("message_id")
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)
        self._schedule_background(
            self.memory_consolidator.maybe_consolidate_by_tokens(session)
        )

        if (
            (mt := self.tools.get("message"))
            and isinstance(mt, MessageTool)
            and mt._sent_in_turn
        ):
            return None

        preview = (
            f"{final_content[:120]}..."
            if len(final_content) > 120
            else final_content
        )
        logger.info(
            f"Response to {msg.channel}:{msg.sender_id}: {preview}"
        )
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
        )

    async def _process_system_message(
        self, msg: InboundMessage
    ) -> Optional[OutboundMessage]:
        """Processes a system message.

        System messages are used for internal communication, such as sub-agent
        results or memory consolidation.

        Args:
            msg: The system message to process.

        Returns:
            An `OutboundMessage` to be sent back to the user, or None.
        """
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        logger.info(f"Processing system message from {msg.sender_id}")
        key = f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        await self.memory_consolidator.maybe_consolidate_by_tokens(session)
        self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
        history = session.get_history(max_messages=0)
        # Subagent results should be assistant role, other system messages use user role
        current_role = "assistant" if msg.sender_id == "subagent" else "user"
        messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
        )
        final_content, _, all_msgs = await self._run_agent_loop(messages)
        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)
        self._schedule_background(
            self.memory_consolidator.maybe_consolidate_by_tokens(session)
        )
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=final_content or "Background task completed.",
        )

    @staticmethod
    def _image_placeholder(block: Dict[str, Any]) -> Dict[str, str]:
        """Converts an inline image block into a compact text placeholder.

        Args:
            block: The image block.

        Returns:
            A text block with a placeholder for the image.
        """
        path = (block.get("_meta") or {}).get("path", "")
        return {"type": "text", "text": f"[image: {path}]" if path else "[image]"}

    def _sanitize_persisted_blocks(
        self,
        content: List[Dict[str, Any]],
        *,
        truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> List[Dict[str, Any]]:
        """Strips volatile multimodal payloads before writing session history.

        Args:
            content: A list of message content blocks.
            truncate_text: Whether to truncate long text blocks.
            drop_runtime: Whether to drop runtime context blocks.

        Returns:
            A list of sanitized message content blocks.
        """
        filtered: List[Dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                # This is the problematic line. The function is hinted to return a
                # List[Dict[str, Any]], but this line can append a non-dict.
                # To fix, we will wrap non-dict blocks in a dict.
                if isinstance(block, str):
                    filtered.append({"type": "text", "text": block})
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if (
                block.get("type") == "image_url"
                and block.get("image_url", {}).get("url", "").startswith("data:image/")
            ):
                filtered.append(self._image_placeholder(block))
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if truncate_text and len(text) > self._TOOL_RESULT_MAX_CHARS:
                    text = f"{text[: self._TOOL_RESULT_MAX_CHARS]}\n... (truncated)"
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self, session: Session, messages: List[Dict[str, Any]], skip: int
    ) -> None:
        """Saves the new messages from a turn into the session history.

        Large tool results are truncated to save space.

        Args:
            session: The user session.
            messages: The list of messages from the turn.
            skip: The number of messages to skip from the beginning of the list.
        """
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages
            if role == "tool":
                if (
                    isinstance(content, str)
                    and len(content) > self._TOOL_RESULT_MAX_CHARS
                ):
                    entry["content"] = (
                        f"{content[: self._TOOL_RESULT_MAX_CHARS]}\n... (truncated)"
                    )
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(
                        content, truncate_text=True
                    )
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and content.startswith(
                    ContextBuilder._RUNTIME_CONTEXT_TAG
                ):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(
                        content, drop_runtime=True
                    )
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> Optional[OutboundMessage]:
        """Processes a message directly and returns the outbound payload.

        This method is useful for testing or for integrating with other systems
        that don't use the message bus.

        Args:
            content: The content of the message to process.
            session_key: The session key to use.
            channel: The channel to use.
            chat_id: The chat ID to use.
            on_progress: An optional callback to report progress.

        Returns:
            An `OutboundMessage` to be sent back to the user, or None.
        """
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id, content=content
        )
        return await self._process_message(
            msg, session_key=session_key, on_progress=on_progress
        )
