"""Model configuration: add, list, delete and test connections. API keys are masked in responses."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db import get_session
from ..llm import test_connection
from ..models import ModelConfig
from ..schemas import ModelConfigIn, ModelConfigOut, TestOut
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
    for name, value in body.model_dump().items():
        if name == "api_key" and not value.strip():
            continue
        setattr(cfg, name, value)
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
