"""CLI entry point for Jarvis.

Usage:
    python -m jarvis invoke "your prompt here"
    python -m jarvis check-config
"""

import asyncio
from pathlib import Path

import typer

from jarvis.main import bootstrap

app = typer.Typer(
    help="Jarvis personal agent CLI",
    add_completion=False,
    no_args_is_help=True,
)

_DEFAULT_CONFIG = Path("./config")
_DEFAULT_DB = "sqlite+aiosqlite:///./data/jarvis.db"


@app.command("invoke")
def invoke_command(
    prompt: str = typer.Argument(..., help="What to ask Jarvis"),
    config_dir: Path = typer.Option(
        _DEFAULT_CONFIG, "--config-dir", "-c", help="Directory with jarvis.yaml etc."
    ),
    db_url: str = typer.Option(_DEFAULT_DB, "--db-url", help="SQLAlchemy DB URL"),
    user: str = typer.Option("cli", "--user", "-u", help="User identifier for the run"),
) -> None:
    """Run Jarvis once against a prompt and print the result."""
    asyncio.run(_invoke_async(prompt, config_dir, db_url, user))


async def _invoke_async(prompt: str, config_dir: Path, db_url: str, user: str) -> None:
    ctx = await bootstrap(config_dir=config_dir, db_url=db_url)
    try:
        result = await ctx.dispatcher.dispatch_manual(user=user, prompt=prompt)
        typer.echo(result.final_output)
    finally:
        await ctx.shutdown()


@app.command("check-config")
def check_config_command(
    config_dir: Path = typer.Option(
        _DEFAULT_CONFIG, "--config-dir", "-c", help="Directory with jarvis.yaml etc."
    ),
) -> None:
    """Validate and print a summary of the current config."""
    from jarvis.config.loader import load_config

    cfg = load_config(config_dir)
    typer.echo("=== jarvis config ===")
    typer.echo(f"llm.base_url       = {cfg.jarvis.llm.base_url}")
    typer.echo(f"llm.model          = {cfg.jarvis.llm.model}")
    typer.echo(f"timezone           = {cfg.jarvis.timezone}")
    typer.echo(f"idle_timeout_sec   = {cfg.jarvis.idle_timeout_sec}")
    typer.echo(f"max_concurrent     = {cfg.jarvis.max_concurrent_agents}")
    typer.echo(
        f"discord enabled    = {cfg.channels.discord is not None and cfg.channels.discord.enabled}"
    )
    typer.echo(f"mcp servers        = {len(cfg.mcp_servers.servers)}")
    for s in cfg.mcp_servers.servers:
        typer.echo(f"  - {s.name} ({s.transport}) enabled={s.enabled}")
