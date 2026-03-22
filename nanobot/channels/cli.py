"""CLI channel for nanobot."""

import asyncio
from typing import Any, Dict

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


class CLIConfig(Base):
    """CLI channel configuration."""

    enabled: bool = True


class CLIChannel(BaseChannel):
    """CLI channel for local terminal interaction."""

    name = "cli"
    display_name = "CLI"

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        """Return the default configuration for the CLI channel."""
        return CLIConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus) -> None:
        """Initialize the CLI channel."""
        if isinstance(config, dict):
            config = CLIConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: CLIConfig = config

    async def start(self) -> None:
        """Start the CLI channel and listen for user input."""
        self._running = True
        print("Starting CLI channel. Press Ctrl+C to exit.")
        while self._running:
            try:
                text = await self.get_input()
                if text.strip():
                    await self._handle_message(
                        sender_id="cli_user",
                        chat_id="cli_chat",
                        content=text,
                        metadata={"cli": {"user": "local"}},
                    )
            except (KeyboardInterrupt, EOFError):
                self._running = False
                print("\nExiting CLI channel.")

    async def stop(self) -> None:
        """Stop the CLI channel."""
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message to the CLI."""
        if msg.content:
            print(f"nanobot: {msg.content}")
        if msg.media:
            for path in msg.media:
                print(f"nanobot: [attachment: {path}]")

    async def get_input(self) -> str:
        """
        Get input from the user in a non-blocking way.
        This uses asyncio.to_thread to avoid blocking the event loop.
        """
        return await asyncio.to_thread(input, ">>> ")
