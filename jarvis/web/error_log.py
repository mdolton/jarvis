"""Shared error-log helpers for dashboard routes."""

from jarvis.core.types import AuditEventType

ERROR_AUDIT_TYPES = [
    AuditEventType.SCHEDULE_ERROR,
    AuditEventType.LLM_ERROR,
    AuditEventType.TOOL_ERROR,
    AuditEventType.CONFIG_RELOAD_FAILED,
    AuditEventType.OAUTH_DISCOVERY_FAILED,
    AuditEventType.OAUTH_REFRESH_TRANSIENT_FAILURE,
    AuditEventType.OAUTH_REFRESH_PERMANENTLY_FAILED,
    AuditEventType.ACTION_FAILED,
    AuditEventType.MEMORY_FAILED,
]
