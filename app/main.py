import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from . import access_token
from .changes import router as changes_router
from .config import settings
from .db import engine, init_db
from .documents import router as document_router
from .learning_presets import router as presets_router
from .models import ModelConfig
from .phone_access import router as phone_router
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

_LOCAL_HOSTNAMES = access_token.LOCAL_HOSTNAMES
_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
_hostname = access_token.hostname
# Shown by the app when a phone's pairing is missing, wrong or was rotated.
PAIRING_REQUIRED = "需要重新配对：请在电脑上打开「设置 → 手机访问」，用手机扫描二维码。"


def host_allowed(host_header: str) -> bool:
    """Loopback names, Tailscale Serve names and configured extras only.

    A DNS-rebinding page reaches this server under its own domain, so its Host header
    gives it away even though the request arrives on 127.0.0.1."""
    name = _hostname(host_header)
    return (
        name in _LOCAL_HOSTNAMES or name.endswith(".ts.net") or name in access_token.extra_hosts()
    )


def origin_allowed(origin: str, host_header: str) -> bool:
    """A local page on any port, or the same origin (for example Tailscale HTTPS)."""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False  # Includes "null" from sandboxed frames and file pages.
    return parts.hostname in _LOCAL_HOSTNAMES or parts.netloc.lower() == host_header.lower()


@app.middleware("http")
async def reject_cross_site_requests(request: Request, call_next):
    host = request.headers.get("host", "")
    if not host_allowed(host):
        return JSONResponse({"detail": "不允许的主机名"}, status_code=400)
    origin = request.headers.get("origin")
    # CORS only stops a foreign page from reading responses; a body-less cross-site POST
    # still runs. State changes therefore require a local or same-origin page.
    if request.method in _STATE_CHANGING and origin is not None:
        if not origin_allowed(origin, host):
            return JSONResponse({"detail": "不允许的跨站请求"}, status_code=403)
    # Requests from elsewhere (a phone through Tailscale Serve, another host name) need
    # the pairing cookie; this computer's own requests never do.
    if (
        settings.access_token_mode == "required"
        and access_token.is_remote(request.headers)
        and not access_token.exempt(request.method, request.url.path)
        and not access_token.token_matches(request.cookies.get(access_token.COOKIE_NAME))
    ):
        return JSONResponse({"detail": PAIRING_REQUIRED}, status_code=401)
    return await call_next(request)


for module in (models_router, trees, nodes, mcp_router, memory_router):
    app.include_router(module.router)
    app.include_router(module.router, prefix="/api", include_in_schema=False)

for extra_router in (document_router, changes_router, phone_router, presets_router):
    app.include_router(extra_router)
    app.include_router(extra_router, prefix="/api", include_in_schema=False)


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
