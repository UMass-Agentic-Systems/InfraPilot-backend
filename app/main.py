import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import auth
from app.core.logging_config import configure_logging
from app.db.base import Base
from app.db.session import engine

configure_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("InfraPilot backend starting up")

    Base.metadata.create_all(bind=engine)

    # TODO(#18): instantiate KubernetesService.get_instance() (warn on failure, do not crash)
    # TODO(#18): bg_task = asyncio.create_task(sre_background_loop())

    yield

    logger.info("InfraPilot backend shutting down")

    # TODO(#18): cancel and await bg_task
    # TODO(#18): close all WebSocket connections via manager

    engine.dispose()


app = FastAPI(title="InfraPilot", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])

# TODO(#18): wire remaining routers once they exist
# from app.api.v1 import deploy, monitor, chat, visualize
# app.include_router(deploy.router,     prefix="/api/v1/deploy",     tags=["deploy"])
# app.include_router(monitor.router,    prefix="/api/v1/monitor",    tags=["monitor"])
# app.include_router(chat.router,       prefix="/api/v1/chat",       tags=["chat"])
# app.include_router(visualize.router,  prefix="/api/v1/visualize",  tags=["visualize"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
