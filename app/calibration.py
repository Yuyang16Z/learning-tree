"""Per-model calibration of the local token estimator from reported provider usage.

The ratio lives in its own table so that learning after every answer neither
rewrites ``ModelConfig`` (which would notify every open window) nor is reset by
editing a model. Only real answers are used, and of those only the provider
requests whose own estimate is meaningful (see calibration_sample).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session

from .models import ModelCalibration

MIN_ESTIMATED_UNITS = 2000  # Smaller requests (and test fixtures) carry too much noise.
EMA_ALPHA = 0.3
RATIO_MIN = 0.5
RATIO_MAX = 3.0


def clamp_ratio(ratio: float) -> float:
    return max(RATIO_MIN, min(RATIO_MAX, ratio))


def load_calibration(session: Session | None, config_id: int | None) -> float:
    """Return the learned ratio for a model configuration, or 1.0 when unknown."""
    if session is None or config_id is None:
        return 1.0
    try:
        row = session.get(ModelCalibration, config_id)
    except Exception:  # noqa: BLE001 - a database without the table degrades to 1.0
        return 1.0
    if row is None or row.samples <= 0:
        return 1.0
    return clamp_ratio(float(row.ratio))


def calibration_sample(rounds: list[dict]) -> tuple[int, int] | None:
    """Paired (estimated, actual) input totals of an answer's requests that can teach the ratio.

    Each provider request (tool round or final answer) is judged on its own. It counts
    only when the provider reported its input tokens, its own estimate reaches
    MIN_ESTIMATED_UNITS (fixed per-request overhead dominates smaller ones, however many
    an answer makes) and it carries no image: an image's fixed reservation would measure
    the provider's image pricing instead of the text estimator. A request without its
    pair stays out of both totals. None when no request qualifies.
    """
    estimated = actual = 0
    for round_ in rounds:
        guess, reported = round_.get("estimated_input_tokens"), round_.get("input_tokens")
        if not guess or guess < MIN_ESTIMATED_UNITS or not reported or reported <= 0:
            continue
        if round_.get("has_images"):
            continue
        estimated += int(guess)
        actual += int(reported)
    return (estimated, actual) if estimated else None


def record_usage(engine, config_id: int | None, estimated: int, actual: int) -> float | None:
    """Fold one real request into the model's ratio; returns the new ratio or None."""
    if config_id is None or estimated < MIN_ESTIMATED_UNITS or actual <= 0:
        return None
    sample = clamp_ratio(actual / estimated)
    try:
        with Session(engine) as session:
            row = session.get(ModelCalibration, config_id)
            if row is None:
                row = ModelCalibration(config_id=config_id, ratio=sample)
            else:
                row.ratio = clamp_ratio(row.ratio * (1 - EMA_ALPHA) + sample * EMA_ALPHA)
            row.samples += 1
            row.estimated_total += int(estimated)
            row.actual_total += int(actual)
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
            session.commit()
            return row.ratio
    except Exception:  # noqa: BLE001 - calibration is best-effort side data
        return None
