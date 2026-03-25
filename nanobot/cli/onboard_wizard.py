"""Interactive onboarding wizard for nanobot."""
from __future__ import annotations

import platform
import re
from collections import namedtuple
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import questionary
import typer
from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table

from nanobot import __logo__
from nanobot.cli.model_info import (
    format_token_count,
    get_model_context_limit,
    get_model_provider,
    get_provider_models,
)
from nanobot.config.schema import (
    AgentDefaults,
    AzureOpenAIProvider,
    Channels,
    Config,
    CustomProvider,
    ExecToolConfig,
    Gateway,
    GoogleProvider,
    Heartbeat,
    LiteLLMProvider,
    OpenAICodexProvider,
    Providers,
    WebSearchConfig,
    WebTools,
)

# ----------------------------------------------------------------------------
# Types
# ----------------------------------------------------------------------------
OnboardResult = namedtuple("OnboardResult", ["config", "should_save"])

console = Console()

FieldType = namedtuple("FieldType", ["type_name", "type_obj", "is_nested"])


# ----------------------------------------------------------------------------
# Main Onboarding Logic
# ----------------------------------------------------------------------------

# Main menu choices
EXIT_CHOICE = "Exit (and discard changes)"
SAVE_EXIT_CHOICE = "Save and Exit"
SAVE_CONTINUE_CHOICE = "Save and Continue"

# Models for which provider-specific menus exist
_STRUCTURED_PROVIDER_MODELS = {
    "providers": Providers,
    "azure_openai": AzureOpenAIProvider,
    "google": GoogleProvider,
    "custom": CustomProvider,
    "openai_codex": OpenAICodexProvider,
}


def run_onboard(initial_config: Config) -> OnboardResult:
    """Run the interactive onboarding wizard.

    Args:
        initial_config: The initial configuration to start with.

    Returns:
        A tuple containing the final config and a boolean indicating if it should be saved.
    """
    working_config = initial_config.model_copy(deep=True)
    provider_name = _get_current_provider_name(working_config)
    provider_model = _STRUCTURED_PROVIDER_MODELS.get(provider_name)

    while True:
        _show_main_menu_header(working_config)
        choices = _build_main_menu_choices(provider_name)
        action = _ask_main_menu(choices)

        if action == EXIT_CHOICE:
            return OnboardResult(initial_config, should_save=False)
        if action in (SAVE_EXIT_CHOICE, SAVE_CONTINUE_CHOICE):
            return OnboardResult(working_config, should_save=True)

        section = action.split(">", 1)[-1].strip()

        if section == "Agent Settings":
            _configure_general_settings(working_config.agents.defaults, section)
        elif section == "Tool Settings":
            _configure_tool_settings(working_config)
        elif provider_model:
            _configure_general_settings(
                _get_provider_config(working_config, provider_name),
                section,
                is_provider=True,
            )
        else:
            _configure_unstructured_provider(working_config, provider_name, section)


def _ask_main_menu(choices: List[str]) -> str:
    """Ask the user to select a main menu item."""
    return questionary.select(
        "What would you like to configure?", choices=choices
    ).ask()


def _get_current_provider_name(config: Config) -> str:
    """Get the provider name for the currently configured model."""
    return get_model_provider(config.agents.defaults.model) or "litellm"


def _build_main_menu_choices(provider_name: str) -> List[str]:
    """Build the list of choices for the main menu."""
    choices = [
        "Section > Agent Settings",
        "Section > Tool Settings",
    ]
    if provider_name in _STRUCTURED_PROVIDER_MODELS:
        provider_label = _get_provider_label(provider_name)
        choices.append(f"Section > {provider_label} Settings")

    return choices + [
        questionary.Separator(),
        SAVE_CONTINUE_CHOICE,
        SAVE_EXIT_CHOICE,
        EXIT_CHOICE,
    ]


def _show_main_menu_header(config: Config) -> None:
    """Display the main menu header with the current configuration."""
    console.print()
    console.print(
        Panel(
            f"{__logo__} Interactive Setup",
            title="[bold blue]nanobot[/bold blue]",
            border_style="blue",
        )
    )

    summary_table = Table.grid(padding=(0, 2))
    summary_table.add_column(style="bold cyan")
    summary_table.add_column()
    summary_table.add_row("Model:", config.agents.defaults.model)
    summary_table.add_row(
        "Context Window:",
        f"{format_token_count(config.agents.defaults.context_window_tokens)} tokens",
    )
    console.print(summary_table)
    console.print()


# ----------------------------------------------------------------------------
# General Settings Configuration
# ----------------------------------------------------------------------------


def _configure_general_settings(
    model: BaseModel,
    section_name: str,
    parent_model: Optional[BaseModel] = None,
    is_provider: bool = False,
) -> None:
    """Configure settings for a given Pydantic model.

    This function dynamically generates a menu of fields for the given model
    and prompts the user to edit them.

    Args:
        model: The Pydantic model to configure.
        section_name: The name of the configuration section.
        parent_model: The parent model, if `model` is a nested model.
        is_provider: True if we are editing a provider-specific model.
    """
    while True:
        _show_section_header(section_name)
        field_name, field_type = _select_field_to_edit(model, is_provider=is_provider)
        if field_name == "Back":
            break

        original_model = model.model_copy(deep=True)

        if field_type.is_nested:
            _configure_general_settings(
                getattr(model, field_name), field_name, parent_model=model
            )
        else:
            _edit_field_value(model, field_name, field_type)

        # Handle special cases on model change
        if is_provider and field_name == "model" and model.model != original_model.model:
            _on_provider_model_change(model, original_model, parent_model)


def _on_provider_model_change(
    model: BaseModel, original_model: BaseModel, parent_model: Optional[BaseModel]
) -> None:
    """Handle side effects when the main agent model is changed.

    This function is responsible for:
    - Trying to auto-fill the context window size.
    - Re-evaluating the current provider, as it may have changed.

    Args:
        model: The model that was changed.
        original_model: The model before the change.
        parent_model: The parent of `model`.
    """
    if parent_model:  # Should always be true if is_provider is true
        new_model_name = getattr(model, "model", "")
        _try_auto_fill_context_window(parent_model, new_model_name)


def _show_section_header(section_name: str) -> None:
    """Display a header for a configuration section."""
    console.print()
    console.print(f"[bold underline]{section_name}[/bold underline]")


def _get_field_type(field_info: Any) -> FieldType:
    """Get a simplified type representation for a Pydantic model field.

    This function inspects a Pydantic field's type annotations to determine
    if it's a simple type (str, int, bool), a nested Pydantic model, or
    something else.

    Args:
        field_info: The field information from a Pydantic model.

    Returns:
        A FieldType tuple with type information.
    """
    type_name = "str"
    type_obj = str
    is_nested = False

    outer_type = field_info.annotation
    if hasattr(outer_type, "__origin__"):  # Handle Union, Optional, etc.
        # Get the first non-None type from a Union (e.g., Optional[str])
        type_args = [
            arg for arg in getattr(outer_type, "__args__", []) if arg is not type(None)
        ]
        if type_args:
            main_type = type_args[0]
        else:
            main_type = outer_type
    else:
        main_type = outer_type

    if issubclass_safe(main_type, BaseModel):
        is_nested = True
        type_name = "nested"
        type_obj = main_type
    elif issubclass_safe(main_type, bool):
        type_name = "bool"
        type_obj = bool
    elif issubclass_safe(main_type, int):
        type_name = "int"
        type_obj = int
    elif issubclass_safe(main_type, (list, dict)):
        type_name = "json"
        type_obj = main_type

    return FieldType(type_name, type_obj, is_nested)


def _select_field_to_edit(
    model: BaseModel, is_provider: bool = False
) -> Tuple[str, FieldType]:
    """Prompt the user to select a field to edit from a Pydantic model.

    Args:
        model: The model to inspect for fields.
        is_provider: Whether the model is a provider configuration.

    Returns:
        A tuple of the selected field name and its type information.
    """
    from nanobot.config.schema import AgentDefaults

    choices = []
    field_types: Dict[str, FieldType] = {}

    for name, field_info in model.model_fields.items():
        if name in ("type",):  # Skip reserved/internal fields
            continue

        field_type = _get_field_type(field_info)
        field_types[name] = field_type

        # For provider models, we want to edit the agent's default model
        if is_provider and name == "model":
            # This is a bit of a hack to grab the main model field
            agent_model_field = AgentDefaults().model_fields["model"]
            current_value = getattr(model, name, agent_model_field.default)
            field_types["model"] = _get_field_type(agent_model_field)
        else:
            current_value = getattr(model, name, field_info.default)

        display_value = (
            "********"
            if "key" in name.lower() or "token" in name.lower()
            else _format_value_for_display(current_value)
        )
        choices.append(f"{name}: {display_value}")

    # Special case for model selection
    if is_provider and "model" not in model.model_fields:
        agent_model_field = AgentDefaults().model_fields["model"]
        current_value = getattr(model, "model", agent_model_field.default)
        field_types["model"] = _get_field_type(agent_model_field)
        display_value = _format_value_for_display(current_value)
        choices.insert(0, f"model: {display_value}")

    choices.sort()
    choices.append(questionary.Separator())
    choices.append("Back")

    selected = questionary.select(
        "Which setting would you like to change?", choices=choices
    ).ask()

    if selected == "Back":
        return "Back", FieldType("", None, False)

    field_name = selected.split(":")[0]
    return field_name, field_types[field_name]


def _format_value_for_display(value: Any) -> str:
    """Format a value for display in the menu."""
    if isinstance(value, str) and len(value) > 40:
        return f'"{value[:37]}..."'
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return f"{{{len(value)} keys}}"
    if isinstance(value, bool):
        return str(value)
    if value is None:
        return "[not set]"
    return str(value)


def _input_with_existing(prompt_text: str, existing_value: Any, ftype: str) -> Any:
    """Prompt the user for input, showing the existing value.

    Args:
        prompt_text: The text to display for the prompt.
        existing_value: The current value of the field.
        ftype: The string name of the field's type.

    Returns:
        The new value, or None if the input was invalid.
    """
    if existing_value is None:
        return Prompt.ask(prompt_text)

    if ftype == "json":
        import json

        json_str = json.dumps(existing_value, indent=2)
        edited = questionary.text(
            prompt_text,
            default=json_str,
            multiline=True,
            instruction="Enter value as a JSON string.",
        ).ask()
        try:
            return json.loads(edited)
        except json.JSONDecodeError:
            console.print("[red]Invalid JSON. Please try again.[/red]")
            return None
    else:
        new_value_str = Prompt.ask(
            prompt_text, default=str(existing_value), show_default=True
        )

        if ftype == "int":
            try:
                return int(new_value_str)
            except ValueError:
                console.print("[red]Invalid integer. Please try again.[/red]")
                return None
        return new_value_str


def _input_bool(prompt_text: str, existing_value: bool) -> bool:
    """Prompt the user for a boolean value."""
    return Confirm.ask(prompt_text, default=existing_value)


def _input_model(
    current_value: str, is_provider_model: bool
) -> Tuple[Optional[str], Optional[str]]:
    """Prompt the user to select a model.

    Args:
        current_value: The current model name.
        is_provider_model: Whether this is for a specific provider.

    Returns:
        A tuple of (new model name, new provider name), or (None, None).
    """
    provider_name = get_model_provider(current_value) or "litellm"
    if is_provider_model:
        # If we are in a provider-specific menu, don't allow changing provider
        choices = ["(custom model)", *get_provider_models(provider_name)]
        action = questionary.select(
            "Select a model", choices=choices, default=current_value
        ).ask()
        if action == "(custom model)":
            custom_model = Prompt.ask("Enter custom model name")
            return custom_model, provider_name
        return action, provider_name

    # Top-level model change, allow changing provider
    all_providers = _get_all_providers()
    selected_provider = questionary.select(
        "Select a provider", choices=all_providers, default=provider_name
    ).ask()
    if not selected_provider:
        return None, None

    model_choices = get_provider_models(selected_provider)
    if not model_choices:
        custom_model = Prompt.ask(
            f"Enter model name for provider '{selected_provider}'"
        )
        return custom_model, selected_provider

    full_choices = ["(custom model)", *model_choices]
    selected_model = questionary.select(
        f"Select a model from {selected_provider}", choices=full_choices
    ).ask()
    if not selected_model:
        return None, None
    if selected_model == "(custom model)":
        custom_model = Prompt.ask("Enter custom model name")
        return custom_model, selected_provider

    return selected_model, selected_provider


def _get_provider_config(config: Config, provider_name: str) -> BaseModel:
    """Get the configuration model for a given provider."""
    return getattr(config.providers, provider_name, LiteLLMProvider())


def _get_provider_label(provider_name: str) -> str:
    """Get a display-friendly label for a provider."""
    return provider_name.replace("_", " ").title()


def _get_all_providers() -> List[str]:
    """Get a list of all available provider names."""
    return sorted(_STRUCTURED_PROVIDER_MODELS.keys())


def _edit_field_value(
    working_model: BaseModel, field_name: str, ftype: FieldType
) -> None:
    """Edit the value of a single field on a model.

    Args:
        working_model: The model instance to modify.
        field_name: The name of the field to edit.
        ftype: The type information for the field.
    """
    from nanobot.config.schema import AgentDefaults
    field_display = f"Enter new value for [bold]{field_name}[/bold]"

    # Special handling for model selection
    if field_name == "model":
        current_model_val = getattr(
            working_model, "model", AgentDefaults().model
        )
        is_provider_specific = not isinstance(working_model, AgentDefaults)
        new_model, new_provider = _input_model(current_model_val, is_provider_specific)

        if new_model:
            # If we are editing the main agent model, update it
            if isinstance(working_model, AgentDefaults):
                working_model.model = new_model
                _try_auto_fill_context_window(working_model, new_model)
            else:
                # We are in a provider-specific menu, so we need to
                # update the main agent model instead.
                # This is a bit of a violation of encapsulation, but it's the
                # most intuitive UX.
                agent_defaults = _find_agent_defaults_from_child(working_model)
                if agent_defaults:
                    agent_defaults.model = new_model
                    _try_auto_fill_context_window(agent_defaults, new_model)
                else:
                    console.print(
                        "[red]Could not find main agent settings to update.[/red]"
                    )
    else:
        current_value = getattr(working_model, field_name, None)

        # Generic field input
        if ftype.type_name == "bool":
            new_value = _input_bool(field_display, current_value)
        else:
            new_value = _input_with_existing(
                field_display, current_value, ftype.type_name
            )
        if new_value is not None:
            setattr(working_model, field_name, new_value)


def _try_auto_fill_context_window(model: BaseModel, new_model_name: str) -> None:
    """Try to auto-fill context_window_tokens if it's at default value.

    Note:
        This function imports AgentDefaults from nanobot.config.schema to get
        the default context_window_tokens value. If the schema changes, this
        coupling needs to be updated accordingly.
    """
    # Check if context_window_tokens field exists
    if not hasattr(model, "context_window_tokens"):
        return

    current_context = getattr(model, "context_window_tokens", None)

    # Check if current value is the default (65536)
    # We only auto-fill if the user hasn't changed it from default
    from nanobot.config.schema import AgentDefaults
    default_context = AgentDefaults().context_window_tokens

    if current_context != default_context:
        return  # User has customized it, don't override

    provider = _get_current_provider(model)
    context_limit = get_model_context_limit(new_model_name, provider)

    if context_limit:
        setattr(model, "context_window_tokens", context_limit)
        console.print(
            f"[green]+ Auto-filled context window: {format_token_count(context_limit)} tokens[/green]"
        )
    else:
        console.print(
            "[dim](i) Could not auto-fill context window (model not in database)[/dim]"
        )


# --- Provider Configuration ---


def _configure_unstructured_provider(
    config: Config, provider_name: str, section_name: str
) -> None:
    """Configure a provider that doesn't have a dedicated Pydantic model.

    This is for providers managed by LiteLLM that only require an API key.

    Args:
        config: The main Config object.
        provider_name: The name of the provider.
        section_name: The display name of the section.
    """
    _show_section_header(section_name)
    provider_config = _get_provider_config(config, provider_name)
    api_key = getattr(provider_config, "api_key", "")

    new_api_key = Prompt.ask(
        "Enter API Key", default=api_key if api_key else "********"
    )
    if new_api_key != "********":
        setattr(provider_config, "api_key", new_api_key)
        setattr(config.providers, provider_name, provider_config)


def _get_current_provider(model: BaseModel) -> Optional[str]:
    """Determine the current provider based on the model name."""
    if hasattr(model, "model"):
        return get_model_provider(getattr(model, "model", ""))
    return None


def _find_agent_defaults_from_child(child_model: BaseModel) -> Optional[AgentDefaults]:
    """Find the AgentDefaults instance from a nested provider model.

    This is a workaround to allow updating the main agent model from a
    provider-specific settings menu.

    Args:
        child_model: The nested provider model.

    Returns:
        The AgentDefaults instance, or None if not found.
    """
    # This relies on the convention that `_configure_general_settings` is called
    # with the root `Config` object's `agents.defaults` when editing the main
    # agent settings. We can't get this directly, so we have to guess.
    # This is a fragile part of the wizard design.
    # A better design might use a context object to pass around state.
    if hasattr(child_model, "__pydantic_parent__"):
        parent = child_model.__pydantic_parent__
        if hasattr(parent, "__pydantic_parent__"):
            grandparent = parent.__pydantic_parent__
            if hasattr(grandparent, "agents") and hasattr(
                grandparent.agents, "defaults"
            ):
                return grandparent.agents.defaults
    return None


# ----------------------------------------------------------------------------
# Tool Settings Configuration
# ----------------------------------------------------------------------------


def _configure_tool_settings(config: Config) -> None:
    """Configure tool-related settings."""
    while True:
        _show_section_header("Tool Settings")
        choices = [
            "Web Search",
            "Shell Command Execution",
            "General",
            questionary.Separator(),
            "Back",
        ]
        action = questionary.select("Which tool area to configure?", choices=choices).ask()

        if action == "Back":
            break
        if action == "Web Search":
            _configure_general_settings(config.tools.web.search, "Web Search")
        elif action == "Shell Command Execution":
            _configure_general_settings(config.tools.exec, "Shell Command Execution")
        elif action == "General":
            _configure_general_tool_settings(config.tools)


def _configure_general_tool_settings(tools_config: WebTools) -> None:
    """Configure general tool settings that don't fit into a sub-category."""
    # This is a bit of a hack because `restrict_to_workspace` is at the top
    # level of the `tools` config. We create a temporary model to edit it.
    class GeneralToolSettings(BaseModel):
        restrict_to_workspace: bool

    temp_model = GeneralToolSettings(
        restrict_to_workspace=tools_config.restrict_to_workspace
    )
    _configure_general_settings(temp_model, "General Tool Settings")
    tools_config.restrict_to_workspace = temp_model.restrict_to_workspace


# ----------------------------------------------------------------------------
# Utilities
# ----------------------------------------------------------------------------


def issubclass_safe(
    cls: Any, base: Union[type, Tuple[type, ...]]
) -> bool:
    """Safely check if a class is a subclass of a base class."""
    try:
        return issubclass(cls, base)
    except TypeError:
        return False
