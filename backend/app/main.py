from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import logging
import asyncio
import os

from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb
from app.infrastructure.storage.redis import get_redis
from app.interfaces.dependencies import get_agent_service
from app.interfaces.api.routes import router
from app.infrastructure.logging import setup_logging
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.infrastructure.models.documents import AgentDocument, SessionDocument, UserDocument
from beanie import init_beanie

# Initialize logging system
setup_logging()
logger = logging.getLogger(__name__)

# Configuration is validated by the background initializer so module import
# can still serve liveness/readiness diagnostics when deployment secrets are
# incomplete.

# Startup readiness flag — True once MongoDB + Redis are fully initialized
_app_ready = False


async def _init_databases() -> None:
    """Initialize MongoDB/Beanie and Redis in the background so uvicorn
    starts accepting requests (and healthchecks) immediately.

    Retries MongoDB up to MAX_RETRIES times before giving up so that
    transient Atlas connection delays at startup don't permanently break
    the service.
    """
    global _app_ready
    try:
        settings = get_settings()
    except Exception as exc:
        _app_ready = False
        logger.error("Configuration is not ready: %s", exc)
        return

    MAX_RETRIES = 5
    RETRY_DELAY = 3.0  # seconds between attempts

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"DB init attempt {attempt}/{MAX_RETRIES} — connecting to MongoDB…")
            await get_mongodb().initialize()
            await init_beanie(
                database=get_mongodb().client[settings.mongodb_database],
                document_models=[AgentDocument, SessionDocument, UserDocument],
            )
            logger.info("Successfully initialized Beanie")
            break
        except Exception as exc:
            logger.error(f"MongoDB/Beanie initialization failed (attempt {attempt}/{MAX_RETRIES}): {exc}")
            if attempt < MAX_RETRIES:
                logger.info(f"Retrying in {RETRY_DELAY}s…")
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.critical(
                    "MongoDB initialization failed after all retries — "
                    "API endpoints requiring the database will return 503."
                )
                return

    try:
        await get_redis().initialize()
        logger.info("Successfully initialized Redis")
    except Exception as exc:
        _app_ready = False
        logger.error(f"Redis initialization failed: {exc} — readiness remains false")
        return

    _app_ready = True
    logger.info("Application fully ready — all services initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup - Dzeck AI Agent initializing")

    # Kick off DB init as a background task so the server starts immediately
    # and Replit's healthcheck can reach /health right away.
    asyncio.create_task(_init_databases())

    try:
        yield
    finally:
        logger.info("Application shutdown - Dzeck AI Agent terminating")
        for resource_name, resource_factory in (("MongoDB", get_mongodb), ("Redis", get_redis)):
            try:
                await resource_factory().shutdown()
            except Exception as exc:
                logger.warning("%s shutdown skipped: %s", resource_name, exc)

        logger.info("Cleaning up AgentService instance")
        try:
            await asyncio.wait_for(get_agent_service().shutdown(), timeout=30.0)
            logger.info("AgentService shutdown completed successfully")
        except asyncio.TimeoutError:
            logger.warning("AgentService shutdown timed out after 30 seconds")
        except Exception as exc:
            logger.error(f"Error during AgentService cleanup: {str(exc)}")


app = FastAPI(title="Dzeck AI Agent", lifespan=lifespan)

# Configure CORS from an explicit comma-separated allow-list. Avoid a
# wildcard default so browser credentials/API responses are not exposed to any
# origin by accident.
_cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5000,http://127.0.0.1:5000",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Last-Event-ID"],
)

# Register exception handlers
register_exception_handlers(app)

# Register routes
app.include_router(router, prefix="/api/v1")


# Health check — returns 200 immediately; reports readiness in body
@app.get("/health")
async def health_check():
    """Liveness endpoint; it does not claim that dependencies are ready."""
    return {"status": "ok", "ready": _app_ready}


@app.get("/health/readiness")
async def readiness_check():
    """Readiness endpoint for orchestrators and deployment smoke checks."""
    from fastapi.responses import JSONResponse
    if not _app_ready:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "ready": False},
        )
    return {"status": "ready", "ready": True}


# Serve compiled Vue frontend in production (when frontend/dist exists)
_frontend_dist = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "../../frontend/dist")
)

if os.path.exists(_frontend_dist):
    _assets_dir = os.path.join(_frontend_dist, "assets")
    if os.path.exists(_assets_dir):
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="static-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str):
        target = os.path.join(_frontend_dist, full_path)
        if full_path and os.path.isfile(target):
            return FileResponse(target)
        return FileResponse(os.path.join(_frontend_dist, "index.html"))
else:
    from fastapi.responses import JSONResponse

    @app.get("/", include_in_schema=False)
    async def health_root():
        return JSONResponse({"status": "ok", "ready": _app_ready, "msg": "Dzeck backend running — frontend not built yet"})
