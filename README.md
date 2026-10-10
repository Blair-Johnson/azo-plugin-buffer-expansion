# azo-plugin-buffer-expansion

Publish buffer excerpts in assistant replies without copying their text back through the model. `{{buffer://buf_abcdef}}` selects a whole buffer; `{{buffer://buf_abcdef:10:25}}` selects 1-based inclusive lines. Only assistant string content is expanded, only on the `user` render channel. Stored messages and model rendering retain the original references. Tool arguments and user-authored messages are untouched.

## Host integration status

The plugin uses native `SafeRenderMetadataRecorder`, `RenderTransformRegistrar`, and `Entry.metadata` contracts. A supported initializer subclass enables `model` and `user` channels. The Agent Zoo installation inspected on October 10, 2026 still projects ordinary TUI cards from raw stored messages, so this repository alone does not yet make expanded text appear in the TUI. The host must consume `entry.render(state, channel="user")` for user-facing content and cache signatures, including the immediate-response path. No core files are modified or monkeypatched by this plugin.

## Installation

From an Agent Zoo host environment, install the repository with:

```sh
pixi run azo-plugin install /path/to/azo-plugin-buffer-expansion
```

Then reload the TUI. The manifest installs the default-pipeline plugin, its configuration, and a short `buffer-expansion` skill. The plugin registers the skill in the native registry; no manual skill copying is needed. Frontend expansion requires the host integration described above.

## Configuration and limits

Edit `<state-home>/plugin-configs/azo-plugin-buffer-expansion/config/buffer_expansion.json` and reload:

```json
{"max_characters": 12000}
```

The budget counts all inserted characters per message across all references, including blockquote continuation prefixes and notices. The original surrounding prose is not capped. An excerpt that does not fit is replaced by `[buffer expansion omitted: character limit]`, never a silently truncated excerpt. Notices are bounded by the remaining budget; exhausted references insert nothing. Positive integers set the budget, and `null` disables it. Invalid values, including zero and booleans, are rejected. Configuration changes affect newly recorded messages only.

## Snapshots and formatting

On first render preparation, each extraction or failure is saved in the entry's `azo_plugin_buffer_expansion_v1` metadata. JSON save/load, later buffer edits, buffer closure, and repeated rendering reuse that snapshot. Missing or reserved buffers and invalid ranges produce a frozen unavailable notice. Ranges beyond the buffer are rejected rather than clamped. A message with no references is also marked as processed. Existing history first encountered after enabling the plugin is snapshotted at that time, not retroactively at its original send time.

References expand even inside code fences. Put code or captured terminal output inside a fence long enough to contain any fences in the excerpt. `> {{buffer://buf_abcdef:10:25}}` propagates the quote prefix to continuation lines. Escape an opening reference with a backslash to show its syntax literally. Buffer contents are never recursively expanded. Buffer IDs may contain letters, numbers, underscores, dots, and hyphens; colon-separated suffixes are reserved for line ranges.

The renderer stores replacement spans and a source fingerprint rather than overwriting message bodies. If another component changes the source text before this transform, expansion is skipped rather than applying obsolete offsets. Disabling the plugin leaves the original references visible; snapshots remain in saved entry metadata. Shared content is intentionally persisted, so review excerpts for secrets before publishing.

## Development

Runtime dependencies are supplied by Agent Zoo. Run the tests against its existing Pixi environment:

```sh
AZO_HOST_MANIFEST=/path/to/agent-zoo/pixi.toml pixi run test
```

The tests use real entries, buffers, serialization, and native pipeline construction. They cover model/user separation, inclusive ranges, immutable replay, errors, Markdown quoting, escaping, total budgets, configuration validation, and the current missing TUI consumer. The host-projection test expects expanded TUI content and is marked as a strict expected failure until native integration lands.
