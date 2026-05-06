from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.routers import quiz, events, analytics
from app.db.mongodb import connect_db, close_db
from app.db.sqlite import init_sqlite
import logging
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Adaptive Quiz API",
    description="AI-powered adaptive quiz backend for Open edX",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://local.openedx.io",
        "https://local.openedx.io",
        "http://apps.local.openedx.io",
        "https://apps.local.openedx.io",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await connect_db()
    await init_sqlite()

@app.on_event("shutdown")
async def shutdown():
    await close_db()

app.include_router(quiz.router)
app.include_router(events.router)
app.include_router(analytics.router)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
