from jarvis.agents.model_catalog import Catalog
from jarvis.channels.discord_commands import (
    ModelCommandDeps,
    is_authorized,
    model_current_text,
    model_list_text,
    model_set_text,
)


def _deps(*, models=("a", "b"), ok=True, active=("a", False), captured=None):
    async def list_models():
        return Catalog(models=list(models), ok=ok)

    async def set_active(sel):
        if captured is not None:
            captured.append(sel)

    return ModelCommandDeps(
        list_models=list_models,
        get_active_model=lambda: active,
        set_active_model=set_active,
    )


def test_is_authorized():
    assert is_authorized("111", {"111", "222"})
    assert not is_authorized("999", {"111"})


async def test_current_reports_override_vs_default():
    text = await model_current_text(_deps(active=("gpt-4o", True)))
    assert "gpt-4o" in text and "override" in text.lower()
    text2 = await model_current_text(_deps(active=("cfg", False)))
    assert "cfg" in text2 and "default" in text2.lower()


async def test_list_ok_and_failure():
    text = await model_list_text(_deps(models=["m1", "m2"], ok=True))
    assert "m1" in text and "m2" in text
    bad = await model_list_text(_deps(models=[], ok=False))
    assert "couldn't" in bad.lower() or "could not" in bad.lower()


async def test_list_ok_but_empty():
    text = await model_list_text(_deps(models=[], ok=True))
    assert "no models" in text.lower()


async def test_set_specific_and_default_sentinel():
    captured = []
    text = await model_set_text(_deps(captured=captured), "gpt-4o")
    assert captured == ["gpt-4o"]
    assert "gpt-4o" in text

    captured2 = []
    text2 = await model_set_text(_deps(captured=captured2), "default")
    assert captured2 == [None]
    assert "default" in text2.lower()
