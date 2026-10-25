from fastapi import APIRouter

from app.api import books, dictationer, health, instructions, live

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(books.router)
api_router.include_router(dictationer.router)
api_router.include_router(instructions.router)
api_router.include_router(live.router)
