"""Scheduler — wraps APScheduler's AsyncScheduler for cron-based agent runs.

On start():
  1. Load enabled schedules from ScheduleRepo.
  2. For each, register an APScheduler cron job.
  3. Start the APScheduler background loop.

Each job fire:
  1. Build a ScheduledTrigger from the schedule row.
  2. Call TriggerDispatcher.dispatch_scheduled().
  3. Route the output via ScheduledOutputRouter.
  4. Record last_run_at / last_run_status.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from apscheduler import AsyncScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.channels.base import ChannelAdapter
from jarvis.config.schema import LLMConfig
from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.core.types import AuditEvent, AuditEventType, ScheduledTrigger
from jarvis.persistence.models import ScheduleRow
from jarvis.persistence.repositories import ScheduleRepo
from jarvis.scheduler.scheduled_output import ScheduledOutputRouter

_log = logging.getLogger(__name__)


def validate_schedule_timing(cron_expr: str, timezone: str) -> None:
    """Raise ValueError if `cron_expr`/`timezone` cannot build a CronTrigger.

    Called at schedule-create time so bad input is rejected at the HTTP
    boundary instead of crashing the next boot (Scheduler.start registers
    every enabled row with CronTrigger.from_crontab).
    """
    try:
        CronTrigger.from_crontab(cron_expr, timezone=timezone)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


class Scheduler:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditLogger,
        llm_config: LLMConfig,
        mcp_servers_provider: Callable[[], list],
        discord_adapter: ChannelAdapter | None,
        mcp_context_provider: Callable[[], str] | None = None,
        model_override: Any = None,
        model_catalog=None,
        idle_timeout_sec: int = 900,
        run_timeout_sec: float | None = None,
        max_concurrent: int = 3,
        oauth_flow=None,
        mcp_manager=None,
        memory_service: Any = None,
        base_url: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._llm_config = llm_config
        self._model_catalog = model_catalog
        self._oauth_flow = oauth_flow
        self._oauth_mcp_manager = mcp_manager
        self._base_url = base_url.rstrip("/") if base_url else None

        self._output_router = ScheduledOutputRouter(discord_adapter=discord_adapter)

        self._aps: AsyncScheduler | None = None
        self._jobs: dict[UUID, str] = {}

        # Scheduler owns its own runner + dispatcher so scheduled runs
        # don't share concurrency gates with interactive runs.
        self._runner = AgentRunner(
            session_factory=session_factory,
            audit=audit,
            mcp_servers_provider=mcp_servers_provider,
            mcp_context_provider=mcp_context_provider,
            llm_config=llm_config,
            model=model_override,
            idle_timeout_sec=idle_timeout_sec,
            run_timeout_sec=run_timeout_sec if run_timeout_sec is not None else idle_timeout_sec,
            memory_service=memory_service,
        )
        self._dispatcher = TriggerDispatcher(
            runner=self._runner,
            audit=audit,
            max_concurrent=max_concurrent,
        )

    async def start(self) -> None:
        self._aps = AsyncScheduler()
        await self._aps.__aenter__()
        await self._aps.start_in_background()

        if self._oauth_flow is not None and self._oauth_mcp_manager is not None:
            from apscheduler.triggers.interval import IntervalTrigger

            from jarvis.scheduler.oauth_jobs import oauth_pending_sweep, oauth_token_refresh

            await self._aps.add_schedule(
                oauth_token_refresh,
                # start_time one interval out: an immediate first fire is redundant
                # (bootstrap just attached fresh tokens) and races short-lived
                # bootstrap+shutdown cycles — cancelling the in-flight job
                # mid-DB-connect orphans an aiosqlite connection inside an
                # unprotected SQLAlchemy pool-connect window (see
                # docs/superpowers/specs/2026-07-08-low-severity-fixes-design.md,
                # Group 6).
                IntervalTrigger(
                    seconds=60, start_time=datetime.now(UTC) + timedelta(seconds=60)
                ),
                kwargs={
                    "flow": self._oauth_flow,
                    "mcp_manager": self._oauth_mcp_manager,
                    "session_factory": self._session_factory,
                },
                id="oauth_token_refresh",
            )
            await self._aps.add_schedule(
                oauth_pending_sweep,
                CronTrigger(hour=3, minute=0),
                kwargs={"session_factory": self._session_factory},
                id="oauth_pending_sweep",
            )

        async with self._session_factory() as session:
            rows = await ScheduleRepo(session).list_enabled()

        for row in rows:
            try:
                await self._register(row.id, row.cron_expr, row.timezone)
            except Exception as exc:
                # One bad row (e.g. legacy invalid cron) must degrade to one
                # dead schedule, never a dead app.
                _log.exception("failed to register schedule %s (%s)", row.id, row.name)
                await self._audit.emit(
                    AuditEvent(
                        type=AuditEventType.SCHEDULE_ERROR,
                        payload={
                            "schedule_id": str(row.id),
                            "schedule_name": row.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "stage": "register",
                        },
                    )
                )

        _log.info("scheduler started with %d active jobs", len(self._jobs))

    async def stop(self) -> None:
        if self._aps is not None:
            await self._aps.__aexit__(None, None, None)
            self._aps = None
        self._jobs.clear()
        await self._runner.drain_memory_tasks()

    def active_job_count(self) -> int:
        return len(self._jobs)

    async def fire_now(self, schedule_id: UUID) -> None:
        """Trigger a schedule immediately (for tests / dashboard Run Now)."""
        await self._execute_schedule(schedule_id)

    async def on_created(self, row: ScheduleRow) -> None:
        """Register a schedule created while the app is running."""
        if row.enabled:
            await self._register(row.id, row.cron_expr, row.timezone)

    async def on_toggled(self, row: ScheduleRow) -> None:
        """Sync APScheduler registration with the row's new enabled state."""
        if row.enabled:
            if row.id not in self._jobs:
                await self._register(row.id, row.cron_expr, row.timezone)
        else:
            await self._unregister(row.id)

    async def on_deleted(self, schedule_id: UUID) -> None:
        """Drop the APScheduler job for a deleted schedule, if registered."""
        await self._unregister(schedule_id)

    async def _unregister(self, schedule_id: UUID) -> None:
        job_id = self._jobs.pop(schedule_id, None)
        if job_id is not None and self._aps is not None:
            await self._aps.remove_schedule(job_id)

    async def _register(
        self,
        schedule_id: UUID,
        cron_expr: str,
        timezone: str,
    ) -> None:
        if self._aps is None:
            return

        trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)

        job_id = await self._aps.add_schedule(
            self._execute_schedule,
            trigger,
            id=str(schedule_id),
            kwargs={"schedule_id": schedule_id},
        )
        self._jobs[schedule_id] = job_id

    async def _execute_schedule(self, schedule_id: UUID) -> None:
        async with self._session_factory() as session:
            repo = ScheduleRepo(session)
            row = await repo.get(schedule_id)
            if row is None:
                _log.warning("schedule %s not found; skipping", schedule_id)
                return
            if not row.enabled:
                _log.info("schedule %s is disabled; skipping", row.name)
                return

            prompt = row.prompt
            output_mode = row.output_mode
            model = row.model
            timezone = row.timezone
            schedule_name = row.name
            notify_on_error = row.notify_on_error
            discord_user_id = row.discord_user_id

        if model is not None and self._model_catalog is not None:
            catalog = await self._model_catalog.list_models()
            if catalog.ok and model not in catalog.models:
                await self._audit.emit(
                    AuditEvent(
                        type=AuditEventType.MODEL_FALLBACK,
                        payload={
                            "schedule_id": str(schedule_id),
                            "requested": model,
                            "substituted": self._llm_config.model,
                        },
                    )
                )
                model = None  # None -> runner falls back to the config default

        fired_at = datetime.now(UTC)
        trigger = ScheduledTrigger(
            schedule_id=str(schedule_id),
            prompt=prompt,
            output_mode=output_mode,
            model=model,
            timezone=timezone,
            fired_at=fired_at,
        )

        try:
            result = await self._dispatcher.dispatch_scheduled(trigger)

            await self._output_router.route(
                result=result,
                output_mode=output_mode,
                discord_user_id=discord_user_id or "",
            )

            async with self._session_factory() as session:
                await ScheduleRepo(session).record_run(schedule_id, at=fired_at, status="success")
        except Exception as exc:
            _log.exception("scheduled run failed for %s", schedule_id)
            error_event = AuditEvent(
                type=AuditEventType.SCHEDULE_ERROR,
                payload={
                    "schedule_id": str(schedule_id),
                    "schedule_name": schedule_name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            await self._audit.emit(error_event)
            async with self._session_factory() as session:
                await ScheduleRepo(session).record_run(
                    schedule_id, at=datetime.now(UTC), status="error"
                )
            if notify_on_error and discord_user_id:
                details_url = self._error_log_url(error_event.id)
                text = f"Scheduled task `{schedule_name}` failed."
                if details_url:
                    text = f"{text} Error: {details_url}"
                else:
                    text = f"{text} Check the error log for details."
                await self._output_router.send_error(
                    text=text,
                    discord_user_id=discord_user_id,
                )

    def _error_log_url(self, event_id: UUID) -> str | None:
        if not self._base_url:
            return None
        return f"{self._base_url}/errors#event-{event_id}"
