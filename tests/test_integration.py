import importlib.util
from pathlib import Path

from agent_utils.core.session import Session
from agent_utils.core.state import Entry
from agent_utils.features.core import RenderPlanInitializer
from agent_zoo.features.modes import default_modes
from agent_zoo.features.projection import entry_to_items
from agent_zoo.pipelines.default import build_default_builder

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("buffer_expansion", ROOT / "src/buffer_expansion.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def build(monkeypatch, *, native=False):
    session = Session(system_prompt="base")
    config = {"llm": {"default": "openai", "models": {"openai": {}}}}
    builder = build_default_builder(
        session,
        config=config,
        skill_paths=[],
        mode_defs=default_modes(),
        include_plugins=native,
        include_llm_drivers=False,
    )
    if not native:
        plugin.register_features(builder, session=session, config=config)
    pipeline = builder.build()
    session.pipeline = pipeline
    session.ensure_initialized()
    return session, pipeline


def test_native_pipeline_registration_and_replay(monkeypatch):
    session, pipeline = build(monkeypatch)
    initializers = [c for c in pipeline if isinstance(c, RenderPlanInitializer)]
    assert len(initializers) == 1
    assert initializers[0].channels == ("model", "user")
    bid, buffer = session.state.buffer_manager.create_scratch_buffer(text="Published draft")
    source = "{{buffer://" + bid + "}}"
    entry = Entry(messages=[{"role": "assistant", "content": source}], index=1, step=1)
    session.state.entries.append(entry)
    session.render()
    assert entry.render(session.state, channel="user")[0]["content"] == "Published draft"
    assert entry.render(session.state)[0]["content"] == source
    buffer.text = "Changed draft"
    session.render()
    assert entry.render(session.state, channel="user")[0]["content"] == "Published draft"


def test_host_projection_uses_user_render_channel(monkeypatch):
    session, _ = build(monkeypatch)
    bid, _ = session.state.buffer_manager.create_scratch_buffer(text="Published draft")
    source = "{{buffer://" + bid + "}}"
    entry = Entry(messages=[{"role": "assistant", "content": source}], index=1, step=1)
    session.state.entries.append(entry)
    session.render()
    items = entry_to_items(entry, state=session.state)
    assert entry.render(session.state, channel="user")[0]["content"] == "Published draft"
    assert items[0].body == "Published draft"


def test_native_loader_discovers_plugin_and_skill(tmp_path, monkeypatch):
    import agent_zoo.plugins.loader as plugin_loader

    root = tmp_path / "plugins"
    default = root / "default"
    default.mkdir(parents=True)
    (default / "buffer_expansion.py").symlink_to(ROOT / "src/buffer_expansion.py")
    monkeypatch.setattr(plugin_loader, "_base_dir", lambda: root)
    monkeypatch.setattr(
        plugin_loader, "_apply_entry_point_plugins", lambda *args, **kwargs: None
    )
    session, pipeline = build(monkeypatch, native=True)
    session.render()
    assert session.state.skill_registry.get("buffer-expansion") is not None
    assert any(
        type(component).__name__ == "BufferExpansionRenderer" for component in pipeline
    )
    bid, _ = session.state.buffer_manager.create_scratch_buffer(text="Discovered plugin")
    session.state.entries.append(
        Entry(
            messages=[{"role": "assistant", "content": "{{buffer://" + bid + "}}"}],
            index=1,
            step=1,
        )
    )
    session.render()
    assert (
        session.state.entries[-1].render(session.state, channel="user")[0]["content"]
        == "Discovered plugin"
    )
