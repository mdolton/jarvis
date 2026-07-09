import asyncio

from jarvis.core.run_scope import current_trigger_source, trigger_scope
from jarvis.core.types import TriggerSource


def test_default_source_is_user():
    assert current_trigger_source.get() == TriggerSource.USER


def test_trigger_scope_sets_and_restores():
    with trigger_scope(TriggerSource.EVENT):
        assert current_trigger_source.get() == TriggerSource.EVENT
        with trigger_scope(TriggerSource.SCHEDULED):
            assert current_trigger_source.get() == TriggerSource.SCHEDULED
        assert current_trigger_source.get() == TriggerSource.EVENT
    assert current_trigger_source.get() == TriggerSource.USER


def test_trigger_scope_restores_on_exception():
    try:
        with trigger_scope(TriggerSource.EVENT):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert current_trigger_source.get() == TriggerSource.USER


async def test_trigger_scope_propagates_to_child_tasks():
    async def child() -> TriggerSource:
        return current_trigger_source.get()

    with trigger_scope(TriggerSource.SCHEDULED):
        seen = await asyncio.create_task(child())
    assert seen == TriggerSource.SCHEDULED
