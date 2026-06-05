"""Built-in digest templates and idempotent database seeding."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.persistence.repositories import DigestTemplateRepo


@dataclass(frozen=True, slots=True)
class DigestTemplateSeed:
    key: str
    name: str
    description: str
    category: str
    prompt: str
    default_cron_expr: str
    default_timezone: str = "UTC"
    default_output_mode: str = "discord"


BUILT_IN_DIGEST_TEMPLATES: tuple[DigestTemplateSeed, ...] = (
    DigestTemplateSeed(
        key="action-inbox-review",
        name="Action Inbox Review",
        description="Review pending approvals and unresolved agent work.",
        category="operations",
        default_cron_expr="0 16 * * 1-5",
        prompt=(
            "Review pending Action Inbox items if that information is available. "
            "Summarize what each pending action is waiting on, group stale or "
            "risky items first, and suggest approve or reject follow-up where "
            "the context is clear. Stay read-only unless an action is explicitly "
            "approved through the Action Inbox flow."
        ),
    ),
    DigestTemplateSeed(
        key="calendar-brief",
        name="Calendar Brief",
        description="Summarize schedule awareness and meeting preparation.",
        category="brief",
        default_cron_expr="30 7 * * *",
        prompt=(
            "Summarize today's calendar. Identify preparation tasks, travel "
            "buffers, and conflicts. Note tomorrow morning's first commitment "
            "when useful. Keep the response short and suitable for Discord."
        ),
    ),
    DigestTemplateSeed(
        key="daily-brief",
        name="Daily Brief",
        description="Morning summary across calendar, email, actions, and time-sensitive items.",
        category="brief",
        default_cron_expr="0 8 * * *",
        prompt=(
            "Prepare my daily brief for today. Summarize today's calendar, flag "
            "schedule conflicts and preparation items, summarize important unread "
            "or recent email if mail tools are available, include pending Action "
            "Inbox items if available, and end with a short prioritized action "
            "list. Keep it concise and suitable for Discord."
        ),
    ),
    DigestTemplateSeed(
        key="email-digest",
        name="Email Digest",
        description="Summarize important recent email activity.",
        category="digest",
        default_cron_expr="0 9 * * 1-5",
        prompt=(
            "Review recent unread and important messages. Group findings by "
            "sender or topic, identify messages needing a reply, call out "
            "receipts, travel, bills, or operational alerts, and avoid listing "
            "low-value notification noise."
        ),
    ),
)


async def seed_built_in_digest_templates(session: AsyncSession) -> None:
    repo = DigestTemplateRepo(session)
    for seed in BUILT_IN_DIGEST_TEMPLATES:
        existing = await repo.get_by_key(seed.key)
        if existing is not None:
            continue
        await repo.create(
            key=seed.key,
            name=seed.name,
            description=seed.description,
            category=seed.category,
            prompt=seed.prompt,
            default_cron_expr=seed.default_cron_expr,
            default_timezone=seed.default_timezone,
            default_output_mode=seed.default_output_mode,
            default_model=None,
            default_discord_user_id=None,
            built_in=True,
            enabled=True,
        )
