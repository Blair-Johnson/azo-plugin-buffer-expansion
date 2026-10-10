import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_utils import Entry
from agent_utils.components import RenderPlanInitializer, RenderPlanFinalizer
from agent_utils.files.buffer_manager import BufferManager

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("buffer_expansion", ROOT / "src/buffer_expansion.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def setup(text="one\ntwo\nthree\n", template="{{buffer://ID}}", role="assistant"):
    manager = BufferManager()
    bid, buffer = manager.create_scratch_buffer(text=text)
    source = template.replace("ID", bid)
    entry = Entry(messages=[{"role": role, "content": source}], index=0, step=0)
    return SimpleNamespace(entries=[entry], buffer_manager=manager), buffer, source


def prepare(state, limit=12000):
    plugin.BufferSnapshotRecorder(limit)(state)
    RenderPlanInitializer(("model", "user"))(state)
    plugin.BufferExpansionRenderer()(state)
    RenderPlanFinalizer()(state)
    return state.entries[0].render(state, channel="user")[0]["content"]


def test_whole_buffer_and_model_isolation():
    state, _, source = setup()
    assert prepare(state) == "one\ntwo\nthree\n"
    assert state.entries[0].render(state)[0]["content"] == source
    assert state.entries[0].messages[0]["content"] == source


@pytest.mark.parametrize("selector,expected", [(":2:3", "two\nthree\n"), (":1:1", "one\n"), ("", "one\ntwo\nthree\n")])
def test_ranges(selector, expected):
    state, _, _ = setup(template="{{buffer://ID" + selector + "}}")
    assert prepare(state) == expected


def test_json_reload_freezes_snapshot_after_mutation_and_closing():
    state, buffer, _ = setup()
    original = prepare(state)
    buffer.text = "changed"
    assert prepare(state) == original
    saved = json.loads(json.dumps(state.entries[0].to_dict()))
    state.entries = [Entry.from_dict(saved)]
    state.buffer_manager = BufferManager()
    assert prepare(state, limit=1) == original


@pytest.mark.parametrize("role", ["user", "system", "tool", "developer"])
def test_non_assistant_messages_are_untouched(role):
    state, _, source = setup(role=role)
    assert prepare(state) == source
    assert plugin.METADATA_KEY not in state.entries[0].metadata


def test_missing_buffer_is_frozen():
    state, _, _ = setup(template="{{buffer://buf_deadbeef}}")
    original = prepare(state)
    assert "unavailable" in original
    state.buffer_manager = None
    assert prepare(state) == original


@pytest.mark.parametrize("selector", [":0:1", ":3:2", ":1:99", ":1", ":-1:2", ":x:2", ":1:2:3"])
def test_invalid_range_is_visible_not_fatal(selector):
    state, _, _ = setup(template="{{buffer://ID" + selector + "}}")
    assert "unavailable" in prepare(state)


def test_limit_and_disabled_limit():
    state, _, _ = setup("x" * 12001)
    assert prepare(state) == plugin.LIMIT_NOTICE
    state, _, _ = setup("x" * 12001)
    assert prepare(state, limit=None) == "x" * 12001


def test_shared_budget_includes_notices():
    state, _, _ = setup("x" * 60, "{{buffer://ID}} {{buffer://ID}} {{buffer://ID}}")
    rendered = prepare(state, limit=100)
    replacements = state.entries[0].metadata[plugin.METADATA_KEY]["messages"]["0"]["replacements"]
    assert sum(len(item["text"]) for item in replacements) <= 100
    assert len(rendered) <= 102
    assert rendered.startswith("x" * 60)


@pytest.mark.parametrize("text", ["", "αβγ", "first\r\nsecond\r\n", "last line"])
def test_exact_text_preservation(text):
    state, _, _ = setup(text)
    assert prepare(state) == text


def test_fenced_code_and_blockquotes():
    state, _, _ = setup("print(1)\nprint(2)", "```python\n{{buffer://ID}}\n```")
    assert prepare(state) == "```python\nprint(1)\nprint(2)\n```"
    state, _, _ = setup("a\nb\nc", "> {{buffer://ID}}")
    assert prepare(state) == "> a\n> b\n> c"
    state, _, _ = setup("a\nb\n", "> > {{buffer://ID}}")
    assert prepare(state) == "> > a\n> > b\n"


def test_escape_and_no_recursive_expansion():
    state, _, source = setup(template=r"\{{buffer://ID}}")
    assert prepare(state) == source
    state, _, _ = setup("{{buffer://buf_deadbeef}}")
    assert prepare(state) == "{{buffer://buf_deadbeef}}"


def test_repeated_render_and_multiple_entries():
    state, _, source = setup("hello")
    state.entries.append(Entry(messages=[{"role": "assistant", "content": "other " + source}], index=1, step=1))
    for _ in range(3):
        assert prepare(state) == "hello"
        assert state.entries[1].render(state, channel="user")[0]["content"] == "other hello"
        assert state.entries[0].render_transform_names("user") == (plugin.PLUGIN,)


def test_reserved_buffer_is_unavailable():
    state, _, _ = setup()
    bid, _ = state.buffer_manager.reserve_buffer()
    state.entries[0].messages[0]["content"] = "{{buffer://" + bid + "}}"
    assert "unavailable" in prepare(state)


def test_changed_message_does_not_apply_stale_offsets():
    state, _, _ = setup()
    prepare(state)
    state.entries[0].messages[0]["content"] = "new {{buffer://buf_deadbeef}}"
    assert prepare(state) == "new {{buffer://buf_deadbeef}}"


@pytest.mark.parametrize("limit", [None, 1, 12000])
def test_valid_config(tmp_path, monkeypatch, limit):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"max_characters": limit}))
    monkeypatch.setattr(plugin, "resource_path", lambda _: config)
    assert plugin.load_limit() == limit


@pytest.mark.parametrize("settings", [{"max_characters": 0}, {"max_characters": -1}, {"max_characters": True}, {"max_characters": "off"}, {"max_characters": 1.5}, {"typo": 12}, [], None])
def test_invalid_config(tmp_path, monkeypatch, settings):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(settings))
    monkeypatch.setattr(plugin, "resource_path", lambda _: config)
    with pytest.raises(ValueError):
        plugin.load_limit()


def test_tiny_budget_and_quote_prefixes_are_bounded():
    state, _, _ = setup("a\nb\nc", "> {{buffer://ID}} {{buffer://ID}}")
    assert len(prepare(state, limit=1)) <= 4


def test_non_text_assistant_content_is_not_modified():
    state, _, _ = setup()
    content = [{"type": "text", "text": "{{buffer://buf_deadbeef}}"}]
    state.entries[0].messages[0]["content"] = content
    assert prepare(state) == content
