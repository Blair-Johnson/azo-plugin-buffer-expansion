"""Snapshot buffer references for the user render channel, never model input."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from agent_utils.core.events import Event, RenderRequested
from agent_utils.core.pipeline import EmitFn, Feature
from agent_utils.core.state import State
from agent_utils.features.core import (
    RenderPlanInitializer,
    RenderTransformRegistrar,
    SafeRenderMetadataRecorder,
)
from agent_utils.features.skills import SkillSource
from agent_zoo.features.settings import Setting, SettingError
from agent_zoo.features.system_prompt import SystemPromptSkillList
from agent_zoo.storage.projects import resolve_state_root

PLUGIN = "azo-plugin-buffer-expansion"
METADATA_KEY = "azo_plugin_buffer_expansion_v1"
USER_CHANNEL = "user"
DEFAULT_MAX_CHARACTERS = 12_000
REFERENCE = re.compile(r"(?<!\\)\{\{buffer://([^{}\n]*)\}\}")
SELECTOR = re.compile(r"([A-Za-z0-9_.-]+)(?::([0-9]+):([0-9]+))?\Z")
LIMIT_NOTICE = "[buffer expansion omitted: character limit]"


def resource_path(relative: str) -> Path:
    installed = Path(resolve_state_root()) / "plugin-configs" / PLUGIN / relative
    return (
        installed
        if installed.exists() or installed.is_symlink()
        else Path(__file__).resolve().parent.parent / relative
    )


def _load_settings(path: Path) -> dict[str, Any]:
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or settings.keys() - {"max_characters"}:
        raise ValueError(f"{path}: expected an object containing only max_characters")
    limit = settings.get("max_characters", DEFAULT_MAX_CHARACTERS)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError(f"{path}: max_characters must be a positive integer or null")
    return settings


def load_limit() -> int | None:
    return _load_settings(resource_path("config/buffer_expansion.json")).get(
        "max_characters", DEFAULT_MAX_CHARACTERS
    )


def get_limit_setting(ctx: Any) -> int:
    del ctx
    try:
        limit = load_limit()
    except (OSError, ValueError):
        raise SettingError("Cannot read buffer expansion configuration.") from None
    return 0 if limit is None else limit


def set_limit_setting(ctx: Any, value: int) -> None:
    del ctx
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise SettingError(
            "Character limit must be a nonnegative signed 64-bit integer."
        )
    temporary = None
    try:
        relative = "config/buffer_expansion.json"
        path = Path(resolve_state_root()) / "plugin-configs" / PLUGIN / relative
        if path.exists() or path.is_symlink():
            path = path.resolve(strict=True)
        settings = _load_settings(path if path.exists() else resource_path(relative))
        settings["max_characters"] = None if value == 0 else value
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".buffer-expansion-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(json.dumps(settings, indent=2) + "\n")
        os.replace(temporary, path)
    except (OSError, ValueError):
        raise SettingError("Cannot save buffer expansion configuration.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


class BufferRenderPlanInitializer(RenderPlanInitializer):
    """Supported singleton override ensuring the ``user`` render channel exists."""

    def __init__(self) -> None:
        super().__init__(("model", USER_CHANNEL))


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


class BufferExpansionSkill(RenderTransformRegistrar):
    optional_reads = {"skill_registry"}
    writes = {"skill_registry"}

    def __init__(self, skill_path: Path | None = None) -> None:
        self.registry: Any = None
        self.skill_path = skill_path or resource_path("skills/buffer-expansion/SKILL.md")
        try:
            self.skill_text: str | None = self.skill_path.read_text(encoding="utf-8")
        except OSError:
            self.skill_text = None

    def handle(self, event: Event, state: State, emit: EmitFn) -> None:
        del event, emit
        registry = getattr(state, "skill_registry", None)
        if registry is not None and registry is not self.registry:
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
            self.registry = registry


def register_features(builder: Any, *, session: Any, config: Any) -> None:
    del session, config
    builder.add(
        Feature(
            name=PLUGIN + "-settings",
            components=[
                Setting(
                    "buffer_expansion.max_characters",
                    "Character limit",
                    "int",
                    get_limit_setting,
                    set_limit_setting,
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
                BufferRenderPlanInitializer(),
                BufferSnapshotRecorder(load_limit()),
                BufferExpansionRenderer(),
                BufferExpansionSkill(),
            ],
            order=[
                BufferSnapshotRecorder,
                BufferRenderPlanInitializer,
                BufferExpansionRenderer,
                BufferExpansionSkill,
                SystemPromptSkillList,
            ],
        )
    )
