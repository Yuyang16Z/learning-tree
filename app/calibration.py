"""Per-model calibration of the local token estimator from reported provider usage.

The ratio lives in its own table so that learning after every answer neither
rewrites ``ModelConfig`` (which would notify every open window) nor is reset by
editing a model. Only real answers with a meaningful estimate are used.
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
