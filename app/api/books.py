from fastapi import APIRouter, status

from app.api.dictationer import AuthorId, Store
from app.schemas.books import BookResponse, ChapterResponse, CreateBookRequest, CreateChapterRequest
from app.storage.chapter_store import slugify

router = APIRouter(prefix="/v1/books", tags=["books"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=BookResponse)
async def create_book(author_id: AuthorId, body: CreateBookRequest, store: Store) -> BookResponse:
    book = slugify(body.book_name)
    meta = await store.create_book(author_id, book, body.book_name)
    return BookResponse(book=book, book_name=meta.book_name, created_at=meta.created_at)


@router.post(
    "/{book_name}/chapters", status_code=status.HTTP_201_CREATED, response_model=ChapterResponse
)
async def create_chapter(
    author_id: AuthorId, book_name: str, body: CreateChapterRequest, store: Store
) -> ChapterResponse:
    book = slugify(book_name)
    meta, chapter_hash = await store.create_chapter(
        author_id, book, body.chapter_number, body.chapter_name
    )
    return ChapterResponse(
        book=book,
        chapter=body.chapter_number,
        chapter_name=meta.chapter_name,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        chapter_hash=chapter_hash,
    )
