import asyncio
import os
import re
import unicodedata
from pathlib import Path
from weakref import WeakValueDictionary

from app.core.errors import AppError, NotFoundError

# Lenient on read (`#page 3`, `# Page 3`), canonical on write (`# page 3`).
_MARKER = re.compile(r"^#\s*page\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
_MAX_NAME_BYTES = 255  # APFS / ext4 file-name limit; Devanagari is 3 bytes per char in UTF-8


class InvalidBookNameError(AppError):
    status_code = 422
    code = "invalid_book_name"


class InvalidPathError(AppError):
    status_code = 400
    code = "invalid_path"


class ChapterNotFoundError(NotFoundError):
    code = "chapter_not_found"


class PageNotFoundError(NotFoundError):
    code = "page_not_found"


def slugify(book_name: str) -> str:
    """Folder-safe book name that keeps Devanagari intact.

    Filters by Unicode category (Letter / Mark / Number) instead of `\\w`, which would drop
    matras such as `ा` and corrupt names like `गोदान`. NFC first, so the same title typed on
    different keyboards maps to the same folder.
    """
    s = unicodedata.normalize("NFC", book_name).lower()
    s = re.sub(r"\s+", "-", s)
    s = "".join(c for c in s if unicodedata.category(c)[0] in "LMN" or c in "-_")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = unicodedata.normalize("NFC", s)
    if not s:
        raise InvalidBookNameError("Book name must contain letters or digits")
    if len(s.encode("utf-8")) > _MAX_NAME_BYTES:
        raise InvalidBookNameError("Book name is too long")
    return s


def _join(existing: str, new: str) -> str:
    return f"{existing}\n{new}" if existing and new else existing or new


def parse(text: str) -> tuple[str, dict[int, str]]:
    """Split a chapter file into (preamble before the first marker, {page: content})."""
    matches = list(_MARKER.finditer(text))
    preamble = (text[: matches[0].start()] if matches else text).strip("\n")
    pages: dict[int, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        page = int(m.group(1))
        pages[page] = _join(pages.get(page, ""), text[m.end() : end].strip("\n"))
    return preamble, pages


def serialize(preamble: str, pages: dict[int, str]) -> str:
    blocks = [preamble] if preamble else []
    blocks += [f"# page {n}\n{pages[n]}".rstrip("\n") for n in sorted(pages)]
    return "\n\n".join(blocks) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # readers see the old file or the new one, never a partial one
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")  # tolerate a BOM from hand-edited files


class ChapterStore:
    """Chapters live at {root}/{author_id}/{book_slug}/chapter-{n}.md.

    Writes are serialised per file with an asyncio.Lock, so this assumes a single process
    (one uvicorn worker). Multi-worker would need fcntl.flock behind the same methods.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._locks: WeakValueDictionary[Path, asyncio.Lock] = WeakValueDictionary()

    def chapter_path(self, author_id: str, book_slug: str, chapter: int) -> Path:
        path = (self._root / author_id / book_slug / f"chapter-{chapter}.md").resolve()
        if not path.is_relative_to(self._root):
            raise InvalidPathError("Resolved path escapes the data root")
        return path

    def _lock_for(self, path: Path) -> asyncio.Lock:
        lock = self._locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[path] = lock
        return lock

    async def append_to_page(
        self, author_id: str, book_slug: str, chapter: int, page: int, text: str
    ) -> None:
        path = self.chapter_path(author_id, book_slug, chapter)
        async with self._lock_for(path):
            await asyncio.to_thread(self._append_sync, path, page, text)

    @staticmethod
    def _append_sync(path: Path, page: int, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        preamble, pages = parse(_read(path)) if path.exists() else ("", {})
        pages[page] = _join(pages.get(page, ""), text)
        _atomic_write(path, serialize(preamble, pages))

    async def read_page(
        self, author_id: str, book_slug: str, chapter: int, page: int
    ) -> tuple[str, list[int]]:
        """Returns (page content, sorted page numbers). No lock: writes are atomic replaces."""
        path = self.chapter_path(author_id, book_slug, chapter)
        try:
            text = await asyncio.to_thread(_read, path)
        except FileNotFoundError as e:
            raise ChapterNotFoundError(f"Chapter {chapter} not found") from e
        _, pages = parse(text)
        if page not in pages:
            raise PageNotFoundError(f"Page {page} not found in chapter {chapter}")
        return pages[page], sorted(pages)
