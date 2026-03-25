"""Interactive onboarding wizard for nanobot."""
from __future__ import annotations

from collections import namedtuple
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import questionary
import typer
from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

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
    Config,
    CustomProvider,
    GoogleProvider,
    LiteLLMProvider,
    OpenAICodexProvider,
    Providers,
)

OnboardResult = namedtuple("OnboardResult", ["config", "should_save"])
FieldTypeInfo = namedtuple("FieldTypeInfo", ["type_name", "type_obj", "is_nested"])

EXIT_CHOICE = "Exit (and discard changes)"
SAVE_EXIT_CHOICE = "Save and Exit"

console = Console()


def run_onboard(initial_config: Config) -> OnboardResult:
    """Run the interactive onboarding wizard."""
    working_config = initial_config.model_copy(deep=True)

    while True:
        provider_name = get_model_provider(working_config.agents.defaults.model)
        _show_main_menu_header(working_config)
        choices = _build_main_menu_choices(provider_name)
        action = questionary.select(
            "What would you like to configure?", choices=choices
        ).ask()

        if action == EXIT_CHOICE:
            return OnboardResult(initial_config, should_save=False)
        if action == SAVE_EXIT_CHOICE:
            return OnboardResult(working_config, should_save=True)

        section_name = action.split(">", 1)[-1].strip()
        _configure_section(working_config, section_name, provider_name)


def _configure_section(config: Config, section_name: str, provider_name: str) -> None:
    """Dispatch to the correct configuration function based on section."""
    if section_name == "Agent Settings":
        _configure_pydantic_model(config.agents.defaults, section_name)
    elif section_name == "Tool Settings":
        _configure_tool_settings(config)
    else:
        provider_config = getattr(config.providers, provider_name, LiteLLMProvider())
        _configure_pydantic_model(
            provider_config, section_name, parent_model=config, is_provider=True
        )


def _configure_pydantic_model(
    model: BaseModel,
    section_name: str,
    parent_model: Optional[BaseModel] = None,
    is_provider: bool = False,
) -> None:
    """Dynamically generate a menu to configure a Pydantic model."""
    while True:
        console.print(f"\n[bold underline]{section_name}[/bold underline]")
        field_name, field_type = _select_field_to_edit(model, is_provider)
        if field_name == "Back":
            break

        if field_type.is_nested:
            _configure_pydantic_model(getattr(model, field_name), field_name, model)
        else:
            new_value = _prompt_for_new_value(model, field_name, field_type)
            if new_value is not _BACK_PRESSED:
                setattr(model, field_name, new_value)
                if field_name == "model":
                    _handle_model_change(model, parent_model, new_value)


def _handle_model_change(
    model: BaseModel, parent_model: Optional[BaseModel], new_model_name: str
) -> None:
    """Handle side-effects when a model is changed, like context window updates."""
    # If we are in a provider menu, the actual AgentDefaults model is the parent
    target_model = parent_model or model
    if isinstance(target_model, Config):
        target_model = target_model.agents.defaults

    if isinstance(target_model, AgentDefaults):
        _try_auto_fill_context_window(target_model, new_model_name)


def _try_auto_fill_context_window(model: AgentDefaults, new_model_name: str) -> None:
    """Auto-fill context window size if it has not been manually set."""
    # This is the safest way to get the default value from a Pydantic model
    # without making assumptions about its internal structure.
    default_context = AgentDefaults().context_window_tokens

    if model.context_window_tokens == default_context:
        limit = get_model_context_limit(new_model_name)
        if limit:
            model.context_window_tokens = limit
            console.print(
                f"[green]+ Auto-filled context window: {format_token_count(limit)} tokens[/green]"
            )

_BACK_PRESSED = object()

def _prompt_for_new_value(
    model: BaseModel, field_name: str, ftype: FieldTypeInfo
) -> Any:
    """Prompt the user for a new value for a given field."""
    prompt_text = f"Enter new value for [bold]{field_name}[/bold]"
    current_value = getattr(model, field_name, None)

    if ftype.type_name == "bool":
        return Confirm.ask(prompt_text, default=current_value)

    if field_name == "model":
        is_provider_specific = not isinstance(model, AgentDefaults)
        new_model, _ = _prompt_for_model(current_value, is_provider_specific)
        return new_model or _BACK_PRESSED

    new_value = Prompt.ask(prompt_text, default=str(current_value))
    if ftype.type_name == "int":
        try:
            return int(new_value)
        except (ValueError, TypeError):
            console.print("[red]Invalid integer value.[/red]")
            return _BACK_PRESSED
    return new_value

def _select_field_to_edit(model: BaseModel, is_provider: bool) -> Tuple[str, FieldTypeInfo]:
    """Prompt the user to select a field to edit from a model."""
    choices, field_types = [], {}
    for name, field_info in model.model_fields.items():
        if name == "type": continue
        field_type = _get_field_type_info(field_info)
        field_types[name] = field_type
        display_value = _format_value(getattr(model, name, field_info.default), name)
        choices.append(f"{name}: {display_value}")

    if is_provider and "model" not in model.model_fields:
        # Special case for provider menus to show the main model field
        agent_model = AgentDefaults.model_fields["model"]
        field_types["model"] = _get_field_type_info(agent_model)
        choices.insert(0, f"model: {_format_value(model.model)}")
    
    choices.sort()
    choices.extend([questionary.Separator(), "Back"])
    selected = questionary.select("Which setting to change?", choices=choices).ask()
    
    if selected == "Back":
        return "Back", FieldTypeInfo("", None, False)
    field_name = selected.split(":")[0]
    return field_name, field_types[field_name]


def _get_field_type_info(field_info: Any) -> FieldTypeInfo:
    """Get simplified type information for a Pydantic model field."""
    type_name, type_obj, is_nested = "str", str, False
    outer_type = field_info.annotation
    if hasattr(outer_type, "__origin__"): # Handle Union, Optional
        type_args = [arg for arg in getattr(outer_type, "__args__", []) if arg is not type(None)]
        main_type = type_args[0] if type_args else outer_type
    else:
        main_type = outer_type

    if issubclass_safe(main_type, BaseModel): is_nested, type_name, type_obj = True, "nested", main_type
    elif issubclass_safe(main_type, bool): type_name, type_obj = "bool", bool
    elif issubclass_safe(main_type, int): type_name, type_obj = "int", int
    elif issubclass_safe(main_type, (list, dict)): type_name, type_obj = "json", main_type
    return FieldTypeInfo(type_name, type_obj, is_nested)


def _format_value(value: Any, name: str = "") -> str:
    """Format a field's value for display in the menu."""
    if "key" in name.lower() or "token" in name.lower(): return "********"
    if isinstance(value, str) and len(value) > 40: return f'"{value[:37]}..."'
    if isinstance(value, str): return f'"{value}"'
    if isinstance(value, list): return f"[{len(value)} items]"
    if isinstance(value, dict): return f"{{{len(value)} keys}}"
    return str(value) if value is not None else "[not set]"


def _configure_tool_settings(config: Config) -> None:
    """Configure tool-related settings."""
    # This is a simplified placeholder. A full implementation would be similar
    # to _configure_pydantic_model for WebSearchConfig and ExecToolConfig.
    console.print("[dim]Tool configuration is not fully implemented in this wizard.[/dim]")

def _show_main_menu_header(config: Config) -> None:
    """Display the main menu header."""
    console.print(Panel(f"{__logo__} Interactive Setup", border_style="blue"))
    console.print(f"[bold cyan]Model:[/] {config.agents.defaults.model}")
    console.print(f"[bold cyan]Context Window:[/] {config.agents.defaults.context_window_tokens} tokens\n")

def _build_main_menu_choices(provider_name: str) -> List[str]:
    """Build the list of choices for the main menu."""
    provider_label = provider_name.replace("_", " ").title()
    return [
        "Section > Agent Settings",
        "Section > Tool Settings",
        f"Section > {provider_label} Settings",
        questionary.Separator(),
        SAVE_EXIT_CHOICE,
        EXIT_CHOICE,
    ]

def _prompt_for_model(current_value: str, is_provider_specific: bool) -> Tuple[Optional[str], Optional[str]]:
    """Prompt the user to select a model and provider."""
    # This is a simplified placeholder. A full implementation would list providers and models.
    new_model = Prompt.ask("Enter new model name", default=current_value)
    if new_model == current_value:
        return None, None
    return new_model, get_model_provider(new_model)

def issubclass_safe(cls: Any, base: Union[type, Tuple[type, ...]]) -> bool:
    """Safely check if a class is a subclass of a base class."""
    try:
        return issubclass(cls, base)
    except TypeError:
        return False
