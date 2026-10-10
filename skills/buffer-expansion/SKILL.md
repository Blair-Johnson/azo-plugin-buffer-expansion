---
name: buffer-expansion
description: Use when sharing buffer excerpts, quoting code or terminal output, or drafting a user-facing response in a scratch buffer before publishing it with a buffer reference. Requires azo-plugin-buffer-expansion and a host that projects the user render channel.
---

# Buffer Expansion

Insert `{{buffer://buf_abcdef}}` in an assistant message to share a whole buffer, or `{{buffer://buf_abcdef:10:25}}` to share lines 10 through 25 (1-based, inclusive). Replace the example ID with an actual readable buffer ID. Expansion is user-facing only: the model and stored message retain the reference. Do not use it in tool arguments as a substitute for their own input syntax.

Draft a response with `create_buffer`, edit and review it, then send its reference as the response. Finish editing before sending: the first render preparation snapshots the extraction on the entry. Later edits, closing the buffer, and session reloads do not change what was shared. Send a new message to share a revised draft.

For code or captured terminal output, place the reference inside a fenced block with an appropriate language. Use an output buffer returned by terminal tools, or copy the relevant captured output into a scratch buffer first; terminal snapshot IDs are not buffer IDs.

````text
```python
{{buffer://buf_abcdef:10:25}}
```
````

Use `> {{buffer://buf_abcdef:10:25}}` for a Markdown blockquote; continuation lines receive the same quote prefix. Choose a fence longer than any fence inside the excerpt. Extraction is literal, not recursive: references inside shared buffer text are not expanded. Prefix the opening braces with a backslash to show literal syntax instead: `\{{buffer://buf_abcdef}}`.

The default budget is 12,000 inserted characters per message, shared across all references, including quote prefixes and notices. Oversized excerpts are replaced by a short omission notice rather than silently truncated. Once the budget is exhausted, later references insert nothing. Missing buffers or invalid ranges produce a frozen unavailable notice. Review buffers first, prefer focused excerpts, and never publish secrets just because they are in a buffer.

The user can change `max_characters` in `plugin-configs/azo-plugin-buffer-expansion/config/buffer_expansion.json`; `null` disables the limit. Reload after configuration changes. Changes affect new snapshots, not messages already shared.
