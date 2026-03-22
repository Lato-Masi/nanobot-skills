# Nanobot Codebase Documentation

This document provides a detailed overview of the Nanobot codebase, designed to help developers understand the application's architecture and maintain it effectively.

## Project Structure

The project is organized into several modules, each with a specific responsibility:

- **`nanobot/agent/`**: Core agent logic, including the main loop, memory, and skills.
- **`nanobot/bus/`**: Event bus for inter-module communication.
- **`nanobot/channels/`**: Handles communication with various messaging platforms (e.g., Slack, Telegram).
- **`nanobot/cli/`**: Command-line interface for interacting with the application.
- **`nanobot/config/`**: Manages application configuration.
- **`nanobot/cron/`**: Service for scheduling tasks.
- **`nanobot/heartbeat/`**: Heartbeat service for monitoring application health.
- **`nanobot/providers/`**: Integrates with different AI model providers (e.g., OpenAI, LiteLLM).
- **`nanobot/security/`**: Implements security features.
- **`nanobot/session/`**: Manages user sessions.
- **`nanobot/skills/`**: Contains the various skills the agent can perform.
- **`nanobot/templates/`**: Stores templates for prompts and messages.
- **`nanobot/utils/`**: Provides utility functions and helper classes.

## Modules in Detail

### `nanobot/agent/`

The `agent` module is the heart of Nanobot. It's responsible for processing user input, making decisions, and generating responses.

- **`loop.py`**: Contains the main agent loop, which continuously processes events and interacts with other modules.
- **`memory.py`**: Manages the agent's short-term and long-term memory.
- **`skills.py`**: Loads and manages the agent's skills.
- **`subagent.py`**: Logic for creating and managing sub-agents.

### `nanobot/bus/`

The `bus` module implements an event-driven architecture, allowing different parts of the application to communicate with each other without being tightly coupled.

- **`events.py`**: Defines the different types of events that can be published to the bus.
- **`queue.py`**: Implements the event queue.

### `nanobot/channels/`

The `channels` module enables Nanobot to connect to various messaging platforms. Each channel is implemented as a separate class that inherits from a base channel.

- **`base.py`**: Defines the interface for all channels.
- **`slack.py`**, **`telegram.py`**, etc.: Platform-specific implementations.

### `nanobot/cli/`

The `cli` module provides a command-line interface for managing the application.

- **`commands.py`**: Defines the available CLI commands.

### `nanobot/config/`

The `config` module is responsible for loading and managing the application's configuration from a file.

- **`loader.py`**: Loads the configuration from a file.
- **`schema.py`**: Defines the configuration schema using Pydantic.

### `nanobot/cron/`

The `cron` module allows for scheduling tasks to be executed at specific times.

- **`service.py`**: The main cron service that manages scheduled jobs.

### `nanobot/heartbeat/`

The `heartbeat` module provides a simple way to monitor the health of the application.

- **`service.py`**: The heartbeat service that periodically sends health checks.

### `nanobot/providers/`

The `providers` module allows Nanobot to use different AI models. Each provider is implemented as a separate class.

- **`base.py`**: Defines the interface for all providers.
- **`openai_codex_provider.py`**, **`litellm_provider.py`**, etc.: Provider-specific implementations.

### `nanobot/security/`

The `security` module implements security-related features, such as network restrictions.

- **`network.py`**: Functions for checking network access rules.

### `nanobot/session/`

The `session` module manages user sessions and conversation history.

- **`manager.py`**: The session manager that handles creating, updating, and deleting sessions.

### `nanobot/skills/`

The `skills` module contains the individual skills that the agent can perform. Each skill is defined in a separate directory and includes a `SKILL.md` file that describes the skill.

### `nanobot/templates/`

The `templates` module stores templates for various prompts and messages used by the agent.

### `nanobot/utils/`

The `utils` module contains various helper functions and utility classes used throughout the application.

## Call Tree and Execution Flow

The following call tree illustrates the typical execution flow and dependencies between the major components of the Nanobot application.

```
nanobot (CLI entry point)
└── start command
    ├── Config.load()
    │   └── nanobot/config/loader.py
    ├── SessionManager(config)
    │   ├── nanobot/session/manager.py
    │   └── EventBus()
    │       └── nanobot/bus/queue.py
    ├── AgentLoop(config, session_manager)
    │   ├── nanobot/agent/loop.py
    │   ├── Subscribes to EventBus
    │   └── on_event(event):
    │       ├── Receives Message event from a channel
    │       ├── get_provider(channel_id) -> AI Provider
    │       │   └── nanobot/providers/registry.py
    │       ├── provider.generate_response(session, user_input)
    │       │   ├── nanobot/providers/base.py
    │       │   ├── Prepares prompts using templates
    │       │   │   └── nanobot/templates/
    │       │   ├── Interacts with AI model (e.g., OpenAI, LiteLLM)
    │       │   ├── Executes skills/tools if necessary
    │       │   │   └── nanobot/agent/skills.py
    │       │   └── Returns response
    │       └── Publishes response to EventBus
    ├── CronService(config, session_manager)
    │   ├── nanobot/cron/service.py
    │   └── Schedules jobs based on config
    │       └── Executes cron jobs at specified times
    └── HeartbeatService(config)
        ├── nanobot/heartbeat/service.py
        └── Sends periodic heartbeat events

Dynamic Execution Flow:

1.  **Initialization:**
    - The `nanobot start` command initializes the main application components.
    - The `AgentLoop`, `CronService`, and `HeartbeatService` run in the background.

2.  **User Interaction:**
    - A user sends a message to Nanobot through a configured channel (e.g., Slack, Telegram).
    - The corresponding channel handler in `nanobot/channels/` receives the message and publishes a `Message` event to the `EventBus`.

3.  **Agent Processing:**
    - The `AgentLoop` receives the `Message` event.
    - It retrieves the appropriate AI provider and generates a response by calling the provider's `generate_response` method.
    - During response generation, the agent might access its memory, use available skills, and interact with external APIs.

4.  **Response Delivery:**
    - The `AgentLoop` publishes the generated response to the `EventBus`.
    - The channel that originated the request (or another designated channel) receives the response and delivers it to the user.

5.  **Scheduled Tasks:**
    - The `CronService` executes scheduled tasks at their designated times, which can trigger events and interact with the agent.
```
