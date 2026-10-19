from fastapi import APIRouter

from app.api import books, dictationer, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(books.router)
api_router.include_router(dictationer.router)
