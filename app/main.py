import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import auth, chat, deploy, monitor, visualize
from app.core.logging_config import configure_logging
from app.core.ws_manager import manager
from app.db.base import Base
from app.db.session import engine
from app.services.k8s_client import KubernetesService
from app.services.sre_background import sre_background_loop

configure_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("InfraPilot backend starting up")

    Base.metadata.create_all(bind=engine)

    try:
        KubernetesService.get_instance()
    except Exception as exc:
        logger.warning("Kubernetes client unavailable at startup: %s", exc)

    bg_task = asyncio.create_task(sre_background_loop())

    yield

    logger.info("InfraPilot backend shutting down")

    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass

    await manager.close_all()

    engine.dispose()


app = FastAPI(title="InfraPilot", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Ensures 500 responses flow through CORSMiddleware so the browser doesn't
    # block them as "Failed to fetch" and the frontend can render the detail.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {exc.__class__.__name__}"},
    )


app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(chat.router, prefix="/api/v1/chat", tags=["chat"])
app.include_router(deploy.router, prefix="/api/v1/deploy", tags=["deploy"])
app.include_router(monitor.router, prefix="/api/v1/monitor", tags=["monitor"])
app.include_router(visualize.router, prefix="/api/v1/visualize", tags=["visualize"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
