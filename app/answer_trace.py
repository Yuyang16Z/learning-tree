"""Numbers-only diagnostics of one answer: how its requests were budgeted, what each
provider round reported, and how long each stage took.

Never prompt text: the report is stored with the message, sent to the page and shown
under the answer, and it is excluded from tree exports.
"""

from __future__ import annotations


def _count(value) -> int | None:
    return None if value is None else int(value)


def _ms(seconds: float) -> int:
    return int(round(max(0.0, seconds) * 1000))


def _total(rounds: list[dict], key: str) -> int | None:
    # A total some request did not report is unknown: a partial sum would set an
    # answer-wide estimate against part of its usage.
    values = [item.get(key) for item in rounds]
    if not values or any(value is None for value in values):
        return None
    return int(sum(values))


def _largest(rounds: list[dict]) -> dict | None:
    # The request with the largest estimate (the later one on a tie): what a single
    # request carried, which the summed totals are not.
    if not rounds:
        return None
    _, peak = max(
        enumerate(rounds),
        key=lambda item: (item[1].get("estimated_input_tokens") or 0, item[0]),
    )
    return {
        "estimated_input_tokens": int(peak.get("estimated_input_tokens") or 0),
        "actual_input_tokens": _count(peak.get("input_tokens")),
        "cached_input_tokens": _count(peak.get("cached_input_tokens")),
    }


def round_trace(rounds: list[dict]) -> list[dict]:
    """Each provider request in order: its input as estimated and as reported, whether
    it had to be trimmed to fit the budget, and the tool calls it led to with their
    running time (waits for the learner's approval left out)."""
    return [
        {
            "estimated_input_tokens": int(item.get("estimated_input_tokens") or 0),
            "actual_input_tokens": _count(item.get("input_tokens")),
            "cached_input_tokens": _count(item.get("cached_input_tokens")),
            "output_tokens": _count(item.get("output_tokens")),
            "trimmed": bool(item.get("compressed")),
            "tool_calls": int(item.get("tool_calls") or 0),
            "tool_ms": int(item.get("tool_ms") or 0),
        }
        for item in rounds
    ]


def answer_report(
    *,
    usage: dict,
    context_stats: dict,
    memory_count: int,
    calibration: float,
    target_tokens: int,
    timings: dict,
    model_started: float,
    started_at: float,
    now: float,
) -> dict:
    """The diagnostics of one answer from its collected usage, assembly statistics and
    stage timings (seconds from time.monotonic)."""
    rounds = usage.get("rounds") or []
    return {
        # Summed over every provider request: each tool round and the final answer.
        "estimated_input_tokens": _total(rounds, "estimated_input_tokens")
        or int(context_stats.get("estimated_input_tokens") or 0),
        "actual_input_tokens": _total(rounds, "input_tokens"),
        "cached_input_tokens": _total(rounds, "cached_input_tokens"),
        "output_tokens": _total(rounds, "output_tokens"),
        "requests": len(rounds),
        "largest_request": _largest(rounds),
        "rounds": round_trace(rounds),
        "compacted": bool(context_stats.get("compacted")),
        "memory_count": memory_count,
        "tool_rounds": int(usage.get("tool_rounds") or 0),
        # "time" or "tokens" when an agent budget ended the tool rounds early.
        "tool_budget_stop": usage.get("tool_budget_stop"),
        "calibration_ratio": round(float(calibration), 3),
        # Model summaries of older history in the prompt (cached or new).
        "summary_blocks": int(context_stats.get("summary_blocks") or 0),
        # The soft request target in tokens (configured or automatic).
        "target_input_tokens": target_tokens,
        "timings_ms": {
            "retrieval": _ms(timings["retrieval"]),
            "tools": _ms(timings["tools"]),
            "summary": _ms(timings["summary"]),
            "approval": _ms(timings["approval"]),
            "model": _ms(now - model_started - timings["tools"] - timings["approval"]),
            "total": _ms(now - started_at),
        },
    }
