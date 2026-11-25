import json
import stat

import pytest
from agent_zoo.durability import PluginStoreDriver, StoreUpdate
from agent_zoo.features.settings import (
    Setting,
    SettingError,
    SettingsContext,
    SettingsRegistry,
)
from agent_zoo.testing import assert_pipeline_sans_io

from test_integration import build, plugin


def _ctx(session):
    return SettingsContext(state=session.state, emit=lambda _ev: None)


def _flush_store_updates(session, state_root) -> list[StoreUpdate]:
    driver = PluginStoreDriver(state_root=state_root)
    reqs = [
        eff.request
        for eff in session.state.io.take_outbox()
        if isinstance(eff.request, StoreUpdate)
    ]
    for req in reqs:
        driver.execute(req)
    return reqs


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ZOO_HOME", str(tmp_path))
    path = tmp_path / "plugin-configs" / plugin.PLUGIN / "config/buffer_expansion.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    path.chmod(0o640)
    session, pipeline = build(monkeypatch)
    assert_pipeline_sans_io(pipeline)
    registry = SettingsRegistry(
        [
            item
            for item in pipeline
            if isinstance(item, Setting)
            and item.key == "buffer_expansion.max_characters"
        ]
    )
    return path, session, pipeline, registry, tmp_path


def edit(registry, session, value, *, state_root=None):
    ctx = _ctx(session)
    res = registry.handle(
        {
            "type": "plugin_settings",
            "request_id": "test",
            "action": "set",
            "binding": registry.binding,
            "key": "buffer_expansion.max_characters",
            "value": value,
        },
        ctx,
    )
    if res.get("ok") and state_root is not None:
        _flush_store_updates(session, state_root)
    return res


def test_registration_persistence_and_reload(settings, monkeypatch):
    path, session, pipeline, registry, tmp_path = settings
    assert registry.error is None
    (row,) = registry.catalog(_ctx(session))
    assert (row["key"], row["kind"], row["apply"], row["value"]) == (
        "buffer_expansion.max_characters",
        "int",
        "reload",
        12000,
    )
    assert row["min"] == 0 and row["max"] == 2**63 - 1
    assert path.read_text() == "{}\n"  # Reading must not materialize defaults.
    assert plugin.resource_path("config/buffer_expansion.json") == path
    recorder = next(
        item for item in pipeline if isinstance(item, plugin.BufferSnapshotRecorder)
    )
    assert edit(registry, session, 0, state_root=tmp_path)["ok"]
    assert json.loads(path.read_text()) == {"max_characters": None}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert recorder.max_characters == 12000
    reloaded, pipeline = build(monkeypatch)
    assert (
        next(
            item for item in pipeline if isinstance(item, plugin.BufferSnapshotRecorder)
        ).max_characters
        is None
    )
    registry = SettingsRegistry(
        [
            item
            for item in pipeline
            if isinstance(item, Setting)
            and item.key == "buffer_expansion.max_characters"
        ]
    )
    assert registry.catalog(_ctx(reloaded))[0]["value"] == 0
    assert edit(registry, reloaded, 1234, state_root=tmp_path)["ok"]
    assert plugin.load_limit() == 1234
    assert json.loads(path.read_text()) == {"max_characters": 1234}


@pytest.mark.parametrize("value", [True, "12", -1, 1.5, 2**63])
def test_invalid_input_preserves_file(settings, value):
    path, session, _, registry, tmp_path = settings
    assert not edit(registry, session, value, state_root=tmp_path)["ok"]
    assert path.read_text() == "{}\n"


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        "[]",
        '{"max_characters": true}',
        '{"max_characters": 12, "unknown": 3, "prompt": "private text"}',
    ],
)
def test_invalid_or_unknown_config_is_not_overwritten(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("AGENT_ZOO_HOME", str(tmp_path))
    path = tmp_path / "plugin-configs" / plugin.PLUGIN / "config/buffer_expansion.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    session, pipeline = build(monkeypatch)
    registry = SettingsRegistry(
        [
            item
            for item in pipeline
            if isinstance(item, Setting)
            and item.key == "buffer_expansion.max_characters"
        ]
    )
    result = edit(registry, session, 15, state_root=tmp_path)
    assert not result["ok"] and result["error"]["code"] == "getter_failed"
    assert "private text" not in json.dumps(result)
    assert path.read_text() == raw


def test_first_save_creates_user_copy_without_editing_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ZOO_HOME", str(tmp_path))
    path = tmp_path / "plugin-configs" / plugin.PLUGIN / "config/buffer_expansion.json"
    source = plugin.resource_path("config/buffer_expansion.json")
    original = source.read_bytes()
    session, pipeline = build(monkeypatch)
    registry = SettingsRegistry(
        [
            item
            for item in pipeline
            if isinstance(item, Setting)
            and item.key == "buffer_expansion.max_characters"
        ]
    )
    assert registry.catalog(_ctx(session))[0]["value"] == 12000
    assert not path.exists()
    assert edit(registry, session, 321, state_root=tmp_path)["ok"]
    assert json.loads(path.read_text()) == {"max_characters": 321}
    assert source.read_bytes() == original
