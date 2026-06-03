"""Discord `/model` command: pure text handlers, an auth gate, and registration.

The text-producing handlers are split out (no discord types) so they're unit
testable without a live gateway. `register_model_commands` wires them onto a
CommandTree with the allow-list gate and autocomplete.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord
from discord import app_commands

from jarvis.agents.model_catalog import Catalog

_log = logging.getLogger(__name__)

_DEFAULT_SENTINEL = "default"


@dataclass(slots=True)
class ModelCommandDeps:
    list_models: Callable[[], Awaitable[Catalog]]
    get_active_model: Callable[[], tuple[str, bool]]  # (model, is_override)
    set_active_model: Callable[[str | None], Awaitable[None]]


def is_authorized(user_id: str, allowed: set[str]) -> bool:
    return user_id in allowed


async def model_current_text(deps: ModelCommandDeps) -> str:
    model, is_override = deps.get_active_model()
    suffix = "override" if is_override else "default from config"
    return f"Active interactive model: `{model}` ({suffix})."


async def model_list_text(deps: ModelCommandDeps) -> str:
    cat = await deps.list_models()
    if not cat.ok:
        return (
            "⚠ Couldn't load models from the endpoint. "
            "Try again, or set one manually with `/model set`."
        )
    if not cat.models:
        return "No models reported by the endpoint."
    return "Available models:\n" + "\n".join(f"- `{m}`" for m in cat.models)


async def model_set_text(deps: ModelCommandDeps, name: str) -> str:
    cleaned = name.strip()
    sel = None if cleaned == "" or cleaned.lower() == _DEFAULT_SENTINEL else cleaned
    await deps.set_active_model(sel)
    if sel is None:
        return "Interactive model reset to the config default."
    return f"Interactive model set to `{sel}`. Takes effect on the next message."


def register_model_commands(
    tree: app_commands.CommandTree,
    *,
    allowed: set[str],
    deps: ModelCommandDeps,
) -> None:
    """Attach the `/model` group to `tree`."""

    group = app_commands.Group(name="model", description="Inspect or change the LLM model")
    # Make the group usable in DMs for user-installed apps.
    group.allowed_installs = app_commands.AppInstallationType(guild=True, user=True)
    group.allowed_contexts = app_commands.AppCommandContext(
        guild=True, dm_channel=True, private_channel=True
    )

    async def _guard(interaction: discord.Interaction) -> bool:
        if not is_authorized(str(interaction.user.id), allowed):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return False
        return True

    @group.command(name="current", description="Show the active interactive model")
    async def current_cmd(interaction: discord.Interaction) -> None:
        if not await _guard(interaction):
            return
        await interaction.response.send_message(await model_current_text(deps), ephemeral=True)

    @group.command(name="list", description="List available models")
    async def list_cmd(interaction: discord.Interaction) -> None:
        if not await _guard(interaction):
            return
        await interaction.response.send_message(await model_list_text(deps), ephemeral=True)

    @group.command(name="set", description="Set the interactive model")
    @app_commands.describe(name="Model id, or 'default' for the config model")
    async def set_cmd(interaction: discord.Interaction, name: str) -> None:
        if not await _guard(interaction):
            return
        await interaction.response.send_message(await model_set_text(deps, name), ephemeral=True)

    @set_cmd.autocomplete("name")
    async def _set_autocomplete(interaction: discord.Interaction, current: str):
        cat = await deps.list_models()
        choices = [app_commands.Choice(name="default (config model)", value=_DEFAULT_SENTINEL)]
        for m in cat.models:
            if current.lower() in m.lower():
                choices.append(app_commands.Choice(name=m, value=m))
        return choices[:25]

    tree.add_command(group)
