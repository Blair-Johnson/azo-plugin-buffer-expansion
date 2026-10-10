import importlib.util
from pathlib import Path

from agent_utils import Entry, Session
from agent_utils.components import RenderPlanInitializer, run_render_phase
from agent_zoo.pipelines import build_default_pipeline
from agent_zoo.modes import default_modes
from agent_zoo.runtime.projection import entry_to_items

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("buffer_expansion", ROOT / "src/buffer_expansion.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def build(monkeypatch, *, native=False):
    import agent_zoo.pipelines as pipelines
    if not native:
        monkeypatch.setattr(pipelines, "apply_pipeline_plugins",
                            lambda builder, *, pipeline_name, session, config:
                            plugin.register_features(builder, session=session, config=config))
    session = Session(system_prompt="base")
    config = {"llm": {"default": "openai", "models": {"openai": {}}}}
    pipeline = build_default_pipeline(session, config=config, skill_paths=[], mode_defs=default_modes())
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
    run_render_phase(session.state, pipeline)
    assert entry.render(session.state, channel="user")[0]["content"] == "Published draft"
    assert entry.render(session.state)[0]["content"] == source
    buffer.text = "Changed draft"
    run_render_phase(session.state, pipeline)
    assert entry.render(session.state, channel="user")[0]["content"] == "Published draft"


@__import__("pytest").mark.xfail(
    strict=True, reason="Agent Zoo currently projects raw messages, not the user render channel",
)
def test_current_host_projection_requires_user_channel_hook(monkeypatch):
    """A documented host gap, not a claim that direct-render tests cover the TUI."""
    session, pipeline = build(monkeypatch)
    bid, _ = session.state.buffer_manager.create_scratch_buffer(text="Published draft")
    source = "{{buffer://" + bid + "}}"
    entry = Entry(messages=[{"role": "assistant", "content": source}], index=1, step=1)
    session.state.entries.append(entry)
    run_render_phase(session.state, pipeline)
    items = entry_to_items(entry, state=session.state)
    assert entry.render(session.state, channel="user")[0]["content"] == "Published draft"
    assert items[0].body == "Published draft"


def test_native_loader_discovers_plugin_and_skill(tmp_path, monkeypatch):
    import agent_zoo.plugins as plugins
    root = tmp_path / "plugins"
    default = root / "default"
    default.mkdir(parents=True)
    (default / "buffer_expansion.py").symlink_to(ROOT / "src/buffer_expansion.py")
    monkeypatch.setattr(plugins, "_base_dir", lambda: root)
    monkeypatch.setattr(plugins, "_apply_entry_point_plugins", lambda *args, **kwargs: None)
    session, pipeline = build(monkeypatch, native=True)
    session.rerender()
    assert session.state.skill_registry.get("buffer-expansion") is not None
    assert any(type(component).__name__ == "BufferExpansionRenderer" for component in pipeline)
    bid, _ = session.state.buffer_manager.create_scratch_buffer(text="Discovered plugin")
    session.state.entries.append(Entry(
        messages=[{"role": "assistant", "content": "{{buffer://" + bid + "}}"}], index=1, step=1,
    ))
    session.rerender()
    assert session.state.entries[-1].render(session.state, channel="user")[0]["content"] == "Discovered plugin"
