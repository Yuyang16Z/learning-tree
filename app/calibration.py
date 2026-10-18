"""Per-model calibration of the local token estimator from reported provider usage.

The ratio lives in its own table so that learning after every answer neither
rewrites ``ModelConfig`` (which would notify every open window) nor is reset by
editing a model's label, key, window or answer reserve; switching a configuration
to another protocol, address or model name starts it uncalibrated. Only real
answers are used, and of those only the provider requests whose own estimate is
meaningful (see calibration_sample).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session

from .models import ModelCalibration, ModelConfig

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


def record_usage(
    engine,
    config_id: int | None,
    estimated: int,
    actual: int,
    *,
    model: tuple[str, str, str] | None = None,
) -> float | None:
    """Fold one answer's sample into its model's ratio; returns the new ratio or None.

    One atomic upsert, written only while the configuration exists and, given `model`
    (protocol, base_url, llm_model of the answer's request), still names that model.
    Concurrent answers therefore never lose a sample, and an answer that finishes after
    its model was deleted or switched to another model writes nothing: SQLite reuses
    the largest deleted id, and the next model must not inherit the old ratio.
    """
    if config_id is None or estimated < MIN_ESTIMATED_UNITS or actual <= 0:
        return None
    sample = clamp_ratio(actual / estimated)
    target = select(ModelConfig.id).where(ModelConfig.id == config_id)
    if model is not None:
        protocol, base_url, llm_model = model
        target = target.where(
            ModelConfig.protocol == protocol,
            ModelConfig.base_url == base_url,
            ModelConfig.llm_model == llm_model,
        )
    updated_at = ModelCalibration.__table__.c.updated_at.type
    first = select(
        literal(config_id),
        literal(1),
        literal(sample),
        literal(int(estimated)),
        literal(int(actual)),
        literal(datetime.now(timezone.utc), updated_at),
    ).where(target.exists())
    statement = insert(ModelCalibration).from_select(
        ["config_id", "samples", "ratio", "estimated_total", "actual_total", "updated_at"], first
    )
    statement = statement.on_conflict_do_update(
        index_elements=["config_id"],
        set_={
            "ratio": func.max(
                RATIO_MIN,
                func.min(
                    RATIO_MAX,
                    ModelCalibration.ratio * (1 - EMA_ALPHA) + statement.excluded.ratio * EMA_ALPHA,
                ),
            ),
            "samples": ModelCalibration.samples + 1,
            "estimated_total": ModelCalibration.estimated_total
            + statement.excluded.estimated_total,
            "actual_total": ModelCalibration.actual_total + statement.excluded.actual_total,
            "updated_at": statement.excluded.updated_at,
        },
    ).returning(ModelCalibration.ratio)
    try:
        with Session(engine) as session:
            ratio = session.execute(statement).scalar_one_or_none()
            session.commit()
            return None if ratio is None else float(ratio)
    except Exception:  # noqa: BLE001 - calibration is best-effort side data
        return None
