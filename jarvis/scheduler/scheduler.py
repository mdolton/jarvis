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
from datetime import UTC, datetime
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
from jarvis.core.types import ScheduledTrigger
from jarvis.persistence.repositories import ScheduleRepo
from jarvis.scheduler.scheduled_output import ScheduledOutputRouter

_log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditLogger,
        llm_config: LLMConfig,
        mcp_servers: list,
        discord_adapter: ChannelAdapter | None,
        model_override: Any = None,
        idle_timeout_sec: int = 900,
        max_concurrent: int = 3,
        oauth_flow=None,
        mcp_manager=None,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._oauth_flow = oauth_flow
        self._oauth_mcp_manager = mcp_manager

        self._output_router = ScheduledOutputRouter(discord_adapter=discord_adapter)

        self._aps: AsyncScheduler | None = None
        self._jobs: dict[UUID, str] = {}

        # Scheduler owns its own runner + dispatcher so scheduled runs
        # don't share concurrency gates with interactive runs.
        self._runner = AgentRunner(
            session_factory=session_factory,
            audit=audit,
            mcp_servers=mcp_servers,
            llm_config=llm_config,
            model=model_override,
            idle_timeout_sec=idle_timeout_sec,
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
                IntervalTrigger(seconds=60),
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
            await self._register(row.id, row.cron_expr, row.timezone)

        _log.info("scheduler started with %d active jobs", len(self._jobs))

    async def stop(self) -> None:
        if self._aps is not None:
            await self._aps.__aexit__(None, None, None)
            self._aps = None
        self._jobs.clear()

    def active_job_count(self) -> int:
        return len(self._jobs)

    async def fire_now(self, schedule_id: UUID) -> None:
        """Trigger a schedule immediately (for tests / dashboard Run Now)."""
        await self._execute_schedule(schedule_id)

    async def _register(
        self,
        schedule_id: UUID,
        cron_expr: str,
        timezone: str,
    ) -> None:
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

        trigger = ScheduledTrigger(
            schedule_id=str(schedule_id),
            prompt=prompt,
            output_mode=output_mode,
        )

        try:
            result = await self._dispatcher.dispatch_scheduled(trigger)

            discord_user_id = ""
            await self._output_router.route(
                result=result,
                output_mode=output_mode,
                discord_user_id=discord_user_id,
            )

            async with self._session_factory() as session:
                await ScheduleRepo(session).record_run(
                    schedule_id, at=datetime.now(UTC), status="success"
                )
        except Exception:
            _log.exception("scheduled run failed for %s", schedule_id)
            async with self._session_factory() as session:
                await ScheduleRepo(session).record_run(
                    schedule_id, at=datetime.now(UTC), status="error"
                )
