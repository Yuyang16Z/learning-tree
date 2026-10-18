"""Approximate context windows for well-known model ids.

Used only when a provider lists no context length for a model. Values are
published vendor limits at the time of writing and are marked approximate to
the user; the validated form keeps the final decision with the user.

The app treats a window as input plus the answer reserve, so where a vendor
caps input below its advertised total window, the input cap is listed.
"""

from __future__ import annotations

import re

# (pattern over the lower-cased model id, context window). Order matters: the
# first match wins, so specific families precede broad ones.
_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"deepseek-(?:v4|flash)", 1_048_576),
    (r"deepseek-(?:chat|reasoner)", 131_072),
    (r"gpt-4\.1", 1_047_576),
    (r"gpt-4o", 128_000),
    # 400,000 is gpt-5's total including up to 128,000 output; input stops at 272,000.
    (r"gpt-5(?:\.\d+)?-chat", 128_000),
    (r"gpt-5", 272_000),
    (r"o[34](?:-|$)", 200_000),
    (r"qwen", 131_072),
    (r"kimi-k2", 262_144),
    (r"glm-4\.6", 204_800),
    (r"glm-4\.5", 131_072),
    (r"gemini", 1_048_576),
    (r"grok-4", 262_144),
    (r"mistral-large", 131_072),
)
# The minor version has one or two digits; a longer run such as 20250514 is a date.
_CLAUDE_VERSION = re.compile(r"claude-(?:[a-z]+-)*?(\d+)(?:[-.](\d{1,2})(?!\d))?")


def _claude_window(model_id: str) -> int | None:
    match = _CLAUDE_VERSION.search(model_id)
    if not match:
        return None
    major, minor = int(match.group(1)), int(match.group(2) or 0)
    # Claude 4.6 and later generations advertise a 1M window; earlier ones 200K.
    return 1_000_000 if major >= 5 or (major == 4 and minor >= 6) else 200_000


def lookup(model_id: str) -> int | None:
    """Return the approximate context window for a model id, or None when unknown."""
    name = (model_id or "").strip().lower()
    # Gateways prefix ids with a vendor, e.g. "anthropic/claude-sonnet-4-6".
    tail = name.rsplit("/", 1)[-1]
    for candidate in dict.fromkeys((name, tail)):
        if candidate.startswith("claude"):
            window = _claude_window(candidate)
            if window:
                return window
        for pattern, window in _PATTERNS:
            if re.match(pattern, candidate):
                return window
    return None
