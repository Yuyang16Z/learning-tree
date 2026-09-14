import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from .config import settings
from .db import engine, init_db
from .documents import router as document_router
from .models import ModelConfig
from .routers import mcp_router, memory_router, models_router, nodes, trees


def seed_default_model() -> None:
    """Seed a default model if default_api_key is configured and no models exist."""
    if not settings.default_api_key:
        return
    with Session(engine) as s:
        if s.exec(select(ModelConfig)).first():
            return
        s.add(
            ModelConfig(
                label=settings.default_label,
                base_url=settings.default_base_url,
                llm_model=settings.default_llm_model,
                api_key=settings.default_api_key,
                is_default=True,
            )
        )
        s.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_default_model()
    from .semantic_models import warm_cached

    warm_cached()
    try:
        yield
    finally:
        from .mcp_client import close_all

        await asyncio.to_thread(close_all)


app = FastAPI(title="LearningTree", version="0.2.0", lifespan=lifespan)

# Allow the local frontend to access the backend across ports via CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:[0-9]+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (models_router, trees, nodes, mcp_router, memory_router):
    app.include_router(module.router)
    app.include_router(module.router, prefix="/api", include_in_schema=False)

app.include_router(document_router)
app.include_router(document_router, prefix="/api", include_in_schema=False)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "name": "学习树"}


DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
def root():
    if (DIST / "index.html").is_file():
        # Revalidate the entry page so a refresh picks up new hashed CSS/JS.
        return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})
    return {"name": "学习树", "docs": "/docs", "frontend": "请先在 web 目录运行 npm run build"}
