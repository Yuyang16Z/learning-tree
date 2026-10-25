"""Model configuration: add, list, delete and test connections. API keys are masked in responses."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete
from sqlmodel import Session, select

from .. import anthropic_provider
from ..db import get_session
from ..llm import LLMSpec, discover_limits, test_connection
from ..models import ModelCalibration, ModelConfig
from ..schemas import ModelConfigIn, ModelConfigOut, ModelDiscoverIn, ModelDiscoverOut, TestOut
from ..service import mask_key, to_spec

router = APIRouter(prefix="/models", tags=["models"])


def _out(cfg: ModelConfig) -> ModelConfigOut:
    return ModelConfigOut(
        id=cfg.id,
        label=cfg.label,
        base_url=cfg.base_url,
        llm_model=cfg.llm_model,
        key_hint=mask_key(cfg.api_key),
        protocol=cfg.protocol,
        max_tokens=cfg.max_tokens,
        context_window=cfg.context_window,
        target_input_tokens=cfg.target_input_tokens,
        is_default=cfg.is_default,
    )


@router.post("", response_model=ModelConfigOut)
def add_model(body: ModelConfigIn, session: Session = Depends(get_session)) -> ModelConfigOut:
    cfg = ModelConfig(**body.model_dump())
    if cfg.is_default:  # Keep exactly one default model at a time.
        for other in session.exec(select(ModelConfig).where(ModelConfig.is_default == True)):  # noqa: E712
            other.is_default = False
            session.add(other)
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return _out(cfg)


def _endpoint(protocol: str, url: str) -> str | None:
    """The address a provider client sends requests to; None when it is not valid."""
    value = url.strip().rstrip("/")
    if protocol != "anthropic":
        return value
    try:
        return anthropic_provider.base_url(value)
    except ValueError:
        return None


def _identity(cfg: ModelConfig) -> tuple:
    """What a calibration ratio is learned for: the protocol, address and model name.
    Label, key, window, answer reserve and target edits keep the ratio."""
    return (cfg.protocol, _endpoint(cfg.protocol, cfg.base_url), cfg.llm_model.strip())


@router.post("/discover", response_model=ModelDiscoverOut)
def discover_model(
    body: ModelDiscoverIn, session: Session = Depends(get_session)
) -> ModelDiscoverOut:
    """Suggest a model's context window; the form applies it, nothing is stored here.

    A blank key with config_id reuses that configuration's stored key only for its own
    protocol and address: after either was edited in the form, no request is made and
    the answer comes from the catalog with a note asking for the new endpoint's key.
    """
    api_key = body.api_key.strip()
    ask_provider = True
    if not api_key and body.config_id is not None:
        cfg = session.get(ModelConfig, body.config_id)
        if not cfg:
            raise HTTPException(404, "模型不存在")
        target = _endpoint(body.protocol, body.base_url)
        if (
            body.protocol == cfg.protocol
            and target is not None
            and target == _endpoint(cfg.protocol, cfg.base_url)
        ):
            api_key = cfg.api_key
        else:
            ask_provider = False
    spec = LLMSpec(
        label="",
        base_url=body.base_url.strip(),
        llm_model=body.llm_model.strip(),
        api_key=api_key,
        protocol=body.protocol,
    )
    return ModelDiscoverOut(**discover_limits(spec, ask_provider=ask_provider))


@router.get("", response_model=list[ModelConfigOut])
def list_models(session: Session = Depends(get_session)) -> list[ModelConfigOut]:
    return [_out(c) for c in session.exec(select(ModelConfig).order_by(ModelConfig.id))]


@router.put("/{config_id}", response_model=ModelConfigOut)
def update_model(
    config_id: int, body: ModelConfigIn, session: Session = Depends(get_session)
) -> ModelConfigOut:
    cfg = session.get(ModelConfig, config_id)
    if not cfg:
        raise HTTPException(404, "模型不存在")
    if body.is_default:
        for other in session.exec(select(ModelConfig).where(ModelConfig.is_default == True)):  # noqa: E712
            other.is_default = False
            session.add(other)
    identity = _identity(cfg)
    for name, value in body.model_dump().items():
        if name == "api_key" and not value.strip():
            continue
        setattr(cfg, name, value)
    if _identity(cfg) != identity:
        # The learned ratio measured another model's tokenizer; this one starts afresh.
        session.execute(delete(ModelCalibration).where(ModelCalibration.config_id == config_id))
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return _out(cfg)


@router.post("/{config_id}/default", response_model=ModelConfigOut)
def set_default(config_id: int, session: Session = Depends(get_session)) -> ModelConfigOut:
    cfg = session.get(ModelConfig, config_id)
    if not cfg:
        raise HTTPException(404, "模型不存在")
    for other in session.exec(select(ModelConfig).where(ModelConfig.is_default == True)):  # noqa: E712
        other.is_default = False
        session.add(other)
    cfg.is_default = True
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return _out(cfg)


@router.delete("/{config_id}")
def delete_model(config_id: int, session: Session = Depends(get_session)) -> dict:
    cfg = session.get(ModelConfig, config_id)
    if not cfg:
        raise HTTPException(404, "模型不存在")
    session.execute(delete(ModelCalibration).where(ModelCalibration.config_id == config_id))
    session.delete(cfg)
    session.commit()
    return {"deleted": config_id}


@router.post("/{config_id}/test", response_model=TestOut)
def test_model(config_id: int, session: Session = Depends(get_session)) -> TestOut:
    cfg = session.get(ModelConfig, config_id)
    if not cfg:
        raise HTTPException(404, "模型不存在")
    ok, detail = test_connection(to_spec(cfg))
    return TestOut(ok=ok, detail=detail)
