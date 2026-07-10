"""CLI entry point for Jarvis.

Usage:
    python -m jarvis invoke "your prompt here"
    python -m jarvis check-config
"""

import asyncio
import signal
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


@app.command("ingest")
def ingest_command(
    path: Path = typer.Argument(
        None, help="File or folder to ingest (defaults to memory.documents_folder)"
    ),
    config_dir: Path = typer.Option(
        _DEFAULT_CONFIG, "--config-dir", "-c", help="Directory with jarvis.yaml etc."
    ),
    db_url: str = typer.Option(_DEFAULT_DB, "--db-url", help="SQLAlchemy DB URL"),
) -> None:
    """Index documents (.md/.txt/.pdf) so the agent can answer from them."""
    asyncio.run(_ingest_async(path, config_dir, db_url))


async def _ingest_async(path: Path | None, config_dir: Path, db_url: str) -> None:
    ctx = await bootstrap(config_dir=config_dir, db_url=db_url)
    try:
        if ctx.document_service is None:
            typer.echo("document ingestion unavailable (memory disabled or non-local DB)")
            raise typer.Exit(code=1)
        folder = ctx.config.jarvis.memory.documents_folder
        target = path or (Path(folder) if folder else None)
        if target is None:
            typer.echo("no path given and memory.documents_folder is not configured")
            raise typer.Exit(code=2)
        outcomes = await ctx.document_service.ingest_path(target)
        if not outcomes:
            typer.echo("no supported files found (.md, .markdown, .txt, .pdf)")
        for outcome in outcomes:
            detail = f" ({outcome.error})" if outcome.error else ""
            typer.echo(
                f"{outcome.status:10s} {outcome.source_ref} [{outcome.chunk_count} chunks]{detail}"
            )
        failures = [o for o in outcomes if o.status in ("failed", "unindexed")]
        if failures:
            raise typer.Exit(code=3)
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


@app.command("serve")
def serve_command(
    config_dir: Path = typer.Option(
        _DEFAULT_CONFIG, "--config-dir", "-c", help="Directory with jarvis.yaml etc."
    ),
    db_url: str = typer.Option(_DEFAULT_DB, "--db-url", help="SQLAlchemy DB URL"),
) -> None:
    """Run Jarvis as a long-lived process (Discord, scheduler, etc.)."""
    asyncio.run(_serve_async(config_dir=config_dir, db_url=db_url))


async def _serve_async(
    *,
    config_dir: Path,
    db_url: str,
    stop_event: asyncio.Event | None = None,
) -> None:
    import uvicorn

    ctx = await bootstrap(config_dir=config_dir, db_url=db_url)
    try:
        # Start uvicorn in the background.
        uvi_config = uvicorn.Config(
            ctx.web_app,
            host="0.0.0.0",
            port=8080,
            log_level="warning",
        )
        uvi_server = uvicorn.Server(uvi_config)
        uvi_task = asyncio.create_task(uvi_server.serve(), name="uvicorn")

        if stop_event is None:
            stop_event = asyncio.Event()
            _install_signal_handlers(stop_event)
        typer.echo("jarvis serving on http://0.0.0.0:8080 (Ctrl-C to stop)")
        await stop_event.wait()
        typer.echo("shutting down...")

        uvi_server.should_exit = True
        await uvi_task
    finally:
        await ctx.shutdown()


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler.
            signal.signal(sig, lambda *_: stop_event.set())
