import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import logging
from contextlib import asynccontextmanager

from config import settings
from database import connect_to_mongo, close_mongo_connection, db
from api import auth, circulars, gaps, policies, audit


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await connect_to_mongo()
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
    yield
    await close_mongo_connection()


app = FastAPI(
    title="SuRaksha MAPS v4.0 API",
    description="Backend API for Compliance Gap Detection Framework",
    version="4.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────
app.include_router(auth.router,       prefix="/api/auth",      tags=["Authentication"])
app.include_router(circulars.router,  prefix="/api/circulars", tags=["Watcher Circulars"])
app.include_router(gaps.router,       prefix="/api/gaps",      tags=["Gap Detection"])
app.include_router(policies.router,   prefix="/api/admin/policies", tags=["Policies"])
app.include_router(auth.admin_router, prefix="/api",           tags=["Admin Users"])
app.include_router(audit.router,      prefix="/api/admin/audit", tags=["Audit Logs"])






# Serve uploaded proof files
_upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(_upload_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_upload_dir), name="uploads")


# ── Health Check ─────────────────────────────────────────────
@app.get("/health", tags=["Health"])
@app.get("/api/health", tags=["Health"])
async def health_check():
    db_status = "disconnected"
    if db.client is not None:
        try:
            await db.client.admin.command("ping")
            db_status = "connected"
        except Exception:
            db_status = "error"

    return {
        "status": "ok",
        "version": "4.0.0",
        "database": db_status,
        "demo_mode": settings.demo_mode,
    }
