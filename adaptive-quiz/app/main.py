from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.routers import quiz
from app.db.mongodb import connect_db, close_db
import logging
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Adaptive Quiz API",
    description="AI-powered adaptive quiz backend for Open edX",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await connect_db()

@app.on_event("shutdown")
async def shutdown():
    await close_db()

app.include_router(quiz.router)

app.mount("/", StaticFiles(directory="static", html=True), name="static")