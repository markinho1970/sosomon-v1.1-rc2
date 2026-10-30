"""
SoSoMon Backend — FastAPI Application
"""

# load_dotenv MUST be first — module-level os.getenv() calls in services/sodex.py etc.
# capture values at import time, so .env must be loaded before any other import.
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger

from database import create_tables
from api.indexes import router as indexes_router
from api.agents import router as agents_router
from api.macro import router as macro_router
from api.stats import router as stats_router
from api.invest import router as invest_router
from api.admin import router as admin_router
from api.audit import router as audit_router
from api.performance import router as performance_router
from api.faucet import router as faucet_router
from api.announcements import router as announcements_router
from api.universe import router as universe_router
from scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("SoSoMon backend starting up...")
    create_tables()
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("SoSoMon backend shut down.")


app = FastAPI(
    title="SoSoMon API",
    description="AI-managed thematic crypto indexes on SoSoValue ValueChain",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://sosomon.ekem.com.br",
        "https://sosomon.ekem.com.br:8443",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(indexes_router)
app.include_router(agents_router)
app.include_router(macro_router)
app.include_router(stats_router)
app.include_router(invest_router)
app.include_router(admin_router)
app.include_router(audit_router)
app.include_router(performance_router)
app.include_router(faucet_router)
app.include_router(announcements_router)
app.include_router(universe_router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.1.0"}


@app.get("/")
def root():
    return {
        "name": "SoSoMon API",
        "tagline": "Thematic indexes. Managed by AI. Verified on-chain.",
        "docs": "/docs",
    }
