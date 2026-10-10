"""Snapshot buffer references for the user render channel, never model input."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from agent_utils import Feature
from agent_utils.components import (
    RenderPlanInitializer, RenderTransformRegistrar, SafeRenderMetadataRecorder,
)
from agent_utils.skills import SkillSource
from agent_zoo.projects import resolve_state_root
from agents.system_prompt import SystemPromptSkillList

PLUGIN = "azo-plugin-buffer-expansion"
METADATA_KEY = "azo_plugin_buffer_expansion_v1"
USER_CHANNEL = "user"
DEFAULT_MAX_CHARACTERS = 12_000
REFERENCE = re.compile(r"(?<!\\)\{\{buffer://([^{}\n]*)\}\}")
SELECTOR = re.compile(r"([A-Za-z0-9_.-]+)(?::([0-9]+):([0-9]+))?\Z")
LIMIT_NOTICE = "[buffer expansion omitted: character limit]"


def resource_path(relative: str) -> Path:
    installed = Path(resolve_state_root()) / "plugin-configs" / PLUGIN / relative
    return installed if installed.is_file() else Path(__file__).resolve().parent.parent / relative


def load_limit() -> int | None:
    path = resource_path("config/buffer_expansion.json")
    settings = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or settings.keys() - {"max_characters"}:
        raise ValueError(f"{path}: expected an object containing only max_characters")
    limit = settings.get("max_characters", DEFAULT_MAX_CHARACTERS)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError(f"{path}: max_characters must be a positive integer or null")
    return limit


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def extract(selector: str, state) -> str:
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
    return "".join(lines[start - 1:end])


def quote_continuations(text: str, source: str, offset: int) -> str:
    prefix = source[source.rfind("\n", 0, offset) + 1:offset]
    if not re.fullmatch(r"(?:[ \t]*>[ \t]?)+[ \t]*", prefix):
        return text
    return prefix.join(text.splitlines(keepends=True))


def snapshot_message(source: str, state, limit: int | None) -> dict:
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
                    replacement = quote_continuations(replacement, source, match.start())
            except (KeyError, ValueError, TypeError, OSError, RuntimeError):
                replacement, status = "[buffer expansion unavailable: check buffer and range]", "unavailable"
            if remaining is not None and len(replacement) > remaining:
                replacement, status = LIMIT_NOTICE[:remaining], "limit"
        if remaining is not None:
            remaining -= len(replacement)
        replacements.append({"start": match.start(), "end": match.end(),
                             "text": replacement, "status": status})
    return {"source_sha256": digest(source), "replacements": replacements}


class BufferRenderPlanInitializer(RenderPlanInitializer):
    """Supported singleton override; TUI consumption remains a host contract."""

    def __init__(self):
        super().__init__(("model", USER_CHANNEL))


class BufferSnapshotRecorder(SafeRenderMetadataRecorder):
    reads = {"entries"}
    optional_reads = {"buffer_manager"}
    writes = {"entries"}

    def __init__(self, max_characters=DEFAULT_MAX_CHARACTERS):
        self.max_characters = max_characters

    def __call__(self, state):
        for entry in state.entries:
            if entry.role != "assistant" or METADATA_KEY in entry.metadata:
                continue
            snapshots = {}
            for index, message in enumerate(entry.messages):
                source = message.get("content")
                if message.get("role") == "assistant" and isinstance(source, str) and REFERENCE.search(source):
                    snapshots[str(index)] = snapshot_message(source, state, self.max_characters)
            entry.metadata[METADATA_KEY] = {"messages": snapshots}
        return state


class BufferExpansionRenderer(RenderTransformRegistrar):
    reads = {"entries"}
    writes = {"entries"}

    def __call__(self, state):
        for entry in state.entries:
            snapshots = entry.metadata.get(METADATA_KEY, {}).get("messages", {})
            if not snapshots:
                continue

            def expand(messages, _state, snapshots=snapshots):
                for index, message in enumerate(messages):
                    snapshot = snapshots.get(str(index))
                    source = message.get("content")
                    if (not snapshot or message.get("role") != "assistant"
                            or not isinstance(source, str) or digest(source) != snapshot["source_sha256"]):
                        continue
                    parts, cursor = [], 0
                    for item in snapshot["replacements"]:
                        parts.extend((source[cursor:item["start"]], item["text"]))
                        cursor = item["end"]
                    parts.append(source[cursor:])
                    message["content"] = "".join(parts)
                return messages

            entry.register_render_transform(USER_CHANNEL, expand, name=PLUGIN)
        return state


class BufferExpansionSkill(RenderTransformRegistrar):
    optional_reads = {"skill_registry"}
    writes = {"skill_registry"}

    def __init__(self):
        self.registry = None

    def __call__(self, state):
        registry = getattr(state, "skill_registry", None)
        if registry is not None and registry is not self.registry:
            registry.add_sources(SkillSource(
                resource_path("skills/buffer-expansion/SKILL.md"), kind=PLUGIN,
                scope="bundled", precedence=-5000, native=True, always_list=True,
            ))
            self.registry = registry
        return state


def register_features(builder, *, session, config):
    builder.add(Feature(
        name=PLUGIN,
        components=[BufferRenderPlanInitializer(), BufferSnapshotRecorder(load_limit()),
                    BufferExpansionRenderer(), BufferExpansionSkill()],
        order=[BufferSnapshotRecorder, BufferRenderPlanInitializer,
               BufferExpansionRenderer, BufferExpansionSkill, SystemPromptSkillList],
    ))
