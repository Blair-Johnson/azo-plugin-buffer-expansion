"""Snapshot buffer references for the user render channel, never model input."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from agent_utils.core.events import Event, RenderRequested, SessionRestored, SessionStarted
from agent_utils.core.pipeline import EmitFn, Feature
from agent_utils.core.state import State
from agent_utils.features.core import (
    RenderTransformRegistrar,
    SafeRenderMetadataRecorder,
)
from agent_utils.features.skills import SkillSource
from agent_zoo.durability import PluginStorage, StoreUpdate
from agent_zoo.features.settings import Setting, SettingError
from agent_zoo.features.system_prompt import SystemPromptSkillList
from agent_zoo.transport.render_channels import USER_RENDER_CHANNEL

PLUGIN = "azo-plugin-buffer-expansion"
METADATA_KEY = "azo_plugin_buffer_expansion_v1"
USER_CHANNEL = USER_RENDER_CHANNEL
CONFIG_RELATIVE = "config/buffer_expansion.json"
DEFAULT_MAX_CHARACTERS = 12_000
REFERENCE = re.compile(r"(?<!\\)\{\{buffer://([^{}\n]*)\}\}")
SELECTOR = re.compile(r"([A-Za-z0-9_.-]+)(?::([0-9]+):([0-9]+))?\Z")
LIMIT_NOTICE = "[buffer expansion omitted: character limit]"


def _plugin_storage() -> PluginStorage:
    return PluginStorage(PLUGIN, config_filename=CONFIG_RELATIVE)


def resource_path(relative: str) -> Path:
    installed = _plugin_storage().config_dir / relative
    return (
        installed
        if installed.exists() or installed.is_symlink()
        else Path(__file__).resolve().parent.parent / relative
    )


def _validate_settings_dict(settings: Any, *, source: str = CONFIG_RELATIVE) -> dict[str, Any]:
    if not isinstance(settings, dict) or settings.keys() - {"max_characters"}:
        raise ValueError(f"{source}: expected an object containing only max_characters")
    limit = settings.get("max_characters", DEFAULT_MAX_CHARACTERS)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError(f"{source}: max_characters must be a positive integer or null")
    return dict(settings)


def _load_settings(path: Path) -> dict[str, Any]:
    settings = json.loads(path.read_text(encoding="utf-8"))
    return _validate_settings_dict(settings, source=str(path))


def load_limit() -> int | None:
    return _load_settings(resource_path(CONFIG_RELATIVE)).get(
        "max_characters", DEFAULT_MAX_CHARACTERS
    )


class _SettingsBox:
    """In-memory snapshot of plugin config loaded at build time; persisted via PluginStoreDriver."""

    def __init__(self, settings: dict[str, Any] | None = None, load_error: bool = False) -> None:
        self.settings = settings
        self.load_error = load_error


def _load_settings_box() -> _SettingsBox:
    try:
        return _SettingsBox(settings=_load_settings(resource_path(CONFIG_RELATIVE)))
    except (OSError, ValueError):
        return _SettingsBox(settings=None, load_error=True)


def _make_limit_callbacks(box: _SettingsBox):
    def get_limit_setting(ctx: Any) -> int:
        del ctx
        if box.load_error or box.settings is None:
            raise SettingError("Cannot read buffer expansion configuration.")
        limit = box.settings.get("max_characters", DEFAULT_MAX_CHARACTERS)
        return 0 if limit is None else limit

    def set_limit_setting(ctx: Any, value: int) -> None:
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise SettingError(
                "Character limit must be a nonnegative signed 64-bit integer."
            )
        if box.load_error or box.settings is None:
            raise SettingError("Cannot save buffer expansion configuration.")
        previous = dict(box.settings)
        updated = dict(box.settings)
        updated["max_characters"] = None if value == 0 else value
        box.settings = updated

        def _rollback() -> None:
            box.settings = previous

        req = StoreUpdate(
            plugin=PLUGIN,
            scope="config",
            config_filename=CONFIG_RELATIVE,
            value=updated,
        )
        submit_persist = getattr(ctx, "submit_persist_io", None)
        if callable(submit_persist) and getattr(ctx, "state", None) is not None:
            submit_persist(req, rollback=_rollback)
        else:
            box.settings = previous
            raise SettingError("Cannot save buffer expansion configuration without session IO.")

    return get_limit_setting, set_limit_setting


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def extract(selector: str, state: Any) -> str:
    match = SELECTOR.fullmatch(selector)
    if match is None:
        raise ValueError("invalid reference syntax")
    buffer_id, first, last = match.groups()
    manager = getattr(state, "buffer_manager", None)
    if manager is None:
        raise ValueError("buffer manager unavailable")
    text = manager.resolve_for_read(state, buffer_id).text
    if first is None:
        return text
    start, end = int(first), int(last)
    lines = text.splitlines(keepends=True)
    if start < 1 or end < start or end > len(lines):
        raise ValueError("line range outside buffer")
    return "".join(lines[start - 1 : end])


def quote_continuations(text: str, source: str, offset: int) -> str:
    prefix = source[source.rfind("\n", 0, offset) + 1 : offset]
    if not re.fullmatch(r"(?:[ \t]*>[ \t]?)+[ \t]*", prefix):
        return text
    return prefix.join(text.splitlines(keepends=True))


def snapshot_message(source: str, state: Any, limit: int | None) -> dict[str, Any]:
    remaining = limit
    replacements = []
    for match in REFERENCE.finditer(source):
        if remaining == 0:
            replacement, status = "", "limit"
        else:
            try:
                replacement = extract(match.group(1), state)
                status = "expanded"
                # Check before adding quote prefixes to a potentially huge buffer.
                if remaining is None or len(replacement) <= remaining:
                    replacement = quote_continuations(
                        replacement, source, match.start()
                    )
            except (KeyError, ValueError, TypeError, OSError, RuntimeError):
                replacement, status = (
                    "[buffer expansion unavailable: check buffer and range]",
                    "unavailable",
                )
            if remaining is not None and len(replacement) > remaining:
                replacement, status = LIMIT_NOTICE[:remaining], "limit"
        if remaining is not None:
            remaining -= len(replacement)
        replacements.append(
            {
                "start": match.start(),
                "end": match.end(),
                "text": replacement,
                "status": status,
            }
        )
    return {"source_sha256": digest(source), "replacements": replacements}


class BufferSnapshotRecorder(SafeRenderMetadataRecorder):
    reads = {"entries"}
    optional_reads = {"buffer_manager"}
    writes = {"entries"}

    def __init__(self, max_characters: int | None = DEFAULT_MAX_CHARACTERS) -> None:
        self.max_characters = max_characters

    def handle(self, event: Event, state: State, emit: EmitFn) -> None:
        del event, emit
        for entry in state.entries:
            if entry.role != "assistant" or METADATA_KEY in entry.metadata:
                continue
            snapshots: dict[str, Any] = {}
            for index, message in enumerate(entry.messages):
                source = message.get("content")
                if (
                    message.get("role") == "assistant"
                    and isinstance(source, str)
                    and REFERENCE.search(source)
                ):
                    snapshots[str(index)] = snapshot_message(
                        source, state, self.max_characters
                    )
            entry.metadata[METADATA_KEY] = {"messages": snapshots}


class BufferExpansionRenderer(RenderTransformRegistrar):
    reads = {"entries"}
    writes = {"entries"}

    def handle(self, event: Event, state: State, emit: EmitFn) -> None:
        del emit
        assert isinstance(event, RenderRequested)
        if event.channels is not None and USER_CHANNEL not in event.channels:
            return
        for entry in state.entries:
            snapshots = entry.metadata.get(METADATA_KEY, {}).get("messages", {})
            if not snapshots:
                continue

            def expand(messages: list[dict[str, Any]], _state: State, snapshots=snapshots):
                for index, message in enumerate(messages):
                    snapshot = snapshots.get(str(index))
                    source = message.get("content")
                    if (
                        not snapshot
                        or message.get("role") != "assistant"
                        or not isinstance(source, str)
                        or digest(source) != snapshot["source_sha256"]
                    ):
                        continue
                    parts, cursor = [], 0
                    for item in snapshot["replacements"]:
                        parts.extend((source[cursor : item["start"]], item["text"]))
                        cursor = item["end"]
                    parts.append(source[cursor:])
                    message["content"] = "".join(parts)
                return messages

            entry.append_render_transform(USER_CHANNEL, expand, name=PLUGIN)


class _SkillRuntime:
    def __init__(self, registered_registry_id: int | None = None) -> None:
        self.registered_registry_id = registered_registry_id


class BufferExpansionSkill(RenderTransformRegistrar):
    handles = (SessionStarted, SessionRestored, RenderRequested)
    optional_reads = {"skill_registry"}
    writes = {"skill_registry"}

    def __init__(self, skill_path: Path | None = None) -> None:
        self.skill_path = skill_path or resource_path("skills/buffer-expansion/SKILL.md")
        try:
            self.skill_text: str | None = self.skill_path.read_text(encoding="utf-8")
        except OSError:
            self.skill_text = None

    def handle(self, event: Event, state: State, emit: EmitFn) -> None:
        del event, emit
        registry = getattr(state, "skill_registry", None)
        if registry is None:
            return
        runtime_fn = getattr(state, "runtime", None)
        rt = runtime_fn("buffer_expansion_skill", _SkillRuntime) if callable(runtime_fn) else None
        reg_id = id(registry)
        if rt is not None and rt.registered_registry_id == reg_id:
            return
        source = SkillSource(
            path=str(self.skill_path.parent),
            kind=PLUGIN,
            scope="bundled",
            precedence=-5000,
            native=True,
            always_list=True,
        )
        registry.add_sources((source,))
        if self.skill_text is not None:
            registry.register_skill_text(
                str(self.skill_path),
                self.skill_text,
                source=source,
            )
        if rt is not None:
            rt.registered_registry_id = reg_id


def register_features(builder: Any, *, session: Any, config: Any) -> None:
    del session, config
    box = _load_settings_box()
    get_limit, set_limit = _make_limit_callbacks(box)
    initial_limit = (
        box.settings.get("max_characters", DEFAULT_MAX_CHARACTERS)
        if box.settings is not None
        else DEFAULT_MAX_CHARACTERS
    )
    builder.add(
        Feature(
            name=PLUGIN + "-settings",
            components=[
                Setting(
                    "buffer_expansion.max_characters",
                    "Character limit",
                    "int",
                    get_limit,
                    set_limit,
                    group="Buffer expansion",
                    min=0,
                    max=2**63 - 1,
                    apply="reload",
                    help=(
                        "Configured characters per message; 0 means unlimited. "
                        "Reload to apply to new snapshots."
                    ),
                )
            ],
            order=[Setting],
        )
    )
    builder.add(
        Feature(
            name=PLUGIN,
            components=[
                BufferSnapshotRecorder(initial_limit),
                BufferExpansionRenderer(),
                BufferExpansionSkill(),
            ],
            order=[
                BufferSnapshotRecorder,
                BufferExpansionRenderer,
                BufferExpansionSkill,
                SystemPromptSkillList,
            ],
        )
    )
