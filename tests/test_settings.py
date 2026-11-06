import json
import stat

import pytest
from agent_zoo.runtime.settings import Setting, SettingError, SettingsContext, SettingsRegistry
from test_integration import build, plugin


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ZOO_HOME", str(tmp_path))
    path = tmp_path / "plugin-configs" / plugin.PLUGIN / "config/buffer_expansion.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    path.chmod(0o640)
    session, pipeline = build(monkeypatch)
    registry = SettingsRegistry([item for item in pipeline if isinstance(item, Setting)])
    return path, session, pipeline, registry


def edit(registry, session, value):
    return registry.handle({
        "type": "plugin_settings", "request_id": "test", "action": "set",
        "binding": registry.binding, "key": "buffer_expansion.max_characters", "value": value,
    }, SettingsContext(session))


def test_registration_persistence_and_reload(settings, monkeypatch):
    path, session, pipeline, registry = settings
    assert registry.error is None
    row, = registry.catalog(SettingsContext(session))
    assert (row["key"], row["kind"], row["apply"], row["value"]) == (
        "buffer_expansion.max_characters", "int", "reload", 12000)
    assert row["min"] == 0 and row["max"] == 2**63 - 1
    assert path.read_text() == "{}\n"  # Reading must not materialize defaults.
    assert plugin.resource_path("config/buffer_expansion.json") == path
    recorder = next(item for item in pipeline if isinstance(item, plugin.BufferSnapshotRecorder))
    assert edit(registry, session, 0)["ok"]
    assert json.loads(path.read_text()) == {"max_characters": None}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert recorder.max_characters == 12000
    reloaded, pipeline = build(monkeypatch)
    assert next(item for item in pipeline if isinstance(item, plugin.BufferSnapshotRecorder)).max_characters is None
    registry = SettingsRegistry([item for item in pipeline if isinstance(item, Setting)])
    assert registry.catalog(SettingsContext(reloaded))[0]["value"] == 0
    assert edit(registry, reloaded, 1234)["ok"]
    assert plugin.load_limit() == 1234
    assert json.loads(path.read_text()) == {"max_characters": 1234}


@pytest.mark.parametrize("value", [True, "12", -1, 1.5, 2**63])
def test_invalid_input_preserves_file(settings, value):
    path, session, _, registry = settings
    assert not edit(registry, session, value)["ok"]
    assert path.read_text() == "{}\n"


@pytest.mark.parametrize("raw", [
    "{broken", "[]", '{"max_characters": true}',
    '{"max_characters": 12, "unknown": 3, "prompt": "private text"}',
])
def test_invalid_or_unknown_config_is_not_overwritten(settings, raw):
    path, session, _, registry = settings
    path.write_text(raw)
    result = edit(registry, session, 15)
    assert not result["ok"] and result["error"]["code"] == "getter_failed"
    assert "private text" not in json.dumps(result)
    with pytest.raises(SettingError, match="Cannot save"):
        plugin.set_limit_setting(SettingsContext(session), 15)
    assert path.read_text() == raw


def test_unreadable_file_is_not_overwritten(settings, monkeypatch):
    path, session, _, registry = settings
    original = type(path).read_text

    def unreadable(target, *args, **kwargs):
        if target == path:
            raise PermissionError("private path")
        return original(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(path), "read_text", unreadable)
        result = edit(registry, session, 15)
        assert not result["ok"]
        assert "private path" not in json.dumps(result)
    assert path.read_text() == "{}\n"


def test_failed_atomic_replace_preserves_file(settings, monkeypatch):
    path, session, _, registry = settings

    def fail(*args):
        raise OSError("private path")

    monkeypatch.setattr(plugin.os, "replace", fail)
    result = edit(registry, session, 15)
    assert not result["ok"] and result["error"]["code"] == "setter_failed"
    assert "private path" not in json.dumps(result)
    assert path.read_text() == "{}\n"
    assert list(path.parent.iterdir()) == [path]
