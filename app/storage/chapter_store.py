import asyncio
import hashlib
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from weakref import WeakValueDictionary

from app.core.errors import AppError, NotFoundError
from app.schemas.instructions import PageEdits
from app.services import edit_executor
from app.services.edit_executor import InvalidEditError

# Lenient on read (`#page 3`, `# Page 3, <hash>`), canonical on write (`# page 3, <hash>`).
# The stored hash is ignored on read and recomputed on write, so a hand edit can't make it stale.
_MARKER = re.compile(r"^#\s*page\s+(\d+)(?:\s*,\s*\S*)?\s*$", re.IGNORECASE | re.MULTILINE)
# laps.md: `# lap N` headings, lenient on read like pages; no hash (laps are never offset-edited).
_LAP_MARKER = re.compile(r"^#\s*lap\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
# Front matter only counts at the very start of the file and with a closing fence.
_FRONT_MATTER = re.compile(r"\A---\n(.*?)^---[ \t]*(?:\n|\Z)", re.DOTALL | re.MULTILINE)
_CHAPTER_FILE = re.compile(r"chapter-(\d+)\.md")
_MAX_NAME_BYTES = 255  # APFS / ext4 file-name limit; Devanagari is 3 bytes per char in UTF-8


class InvalidBookNameError(AppError):
    status_code = 422
    code = "invalid_book_name"


class InvalidPathError(AppError):
    status_code = 400
    code = "invalid_path"


class ChapterNotFoundError(NotFoundError):
    code = "chapter_not_found"


class BookNotFoundError(NotFoundError):
    code = "book_not_found"


class BookExistsError(AppError):
    status_code = 409
    code = "book_exists"


class ChapterExistsError(AppError):
    status_code = 409
    code = "chapter_exists"


class PageNotFoundError(NotFoundError):
    code = "page_not_found"


class LapNotFoundError(NotFoundError):
    code = "lap_not_found"


class PageChangedError(AppError):
    status_code = 409
    code = "page_changed"


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


@dataclass
class ChapterMeta:
    chapter_name: str
    created_at: str
    updated_at: str
    chapter_summary: str = ""
    extra: dict[str, str] = field(default_factory=dict)  # unknown keys, kept as-is


@dataclass(frozen=True)
class BookMeta:
    book_name: str
    created_at: str


_CHAPTER_KEYS = ("chapter_name", "created_at", "updated_at", "chapter_summary", "chapter_hash")


def page_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def chapter_hash(pages: dict[int, str]) -> str:
    """Hash of the pages only, so metadata (timestamps, name, summary) never changes it."""
    body = "\n\n".join(f"# page {n}\n{pages[n]}" for n in sorted(pages))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _split_front_matter(text: str) -> tuple[dict[str, str] | None, str]:
    m = _FRONT_MATTER.match(text)
    if m is None:
        return None, text
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")  # first colon only: values may contain ':'
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields, text[m.end() :]


def _render_front_matter(fields: dict[str, str]) -> str:
    lines = [f"{k}: {v}" if v else f"{k}:" for k, v in fields.items()]
    return "\n".join(["---", *lines, "---"])


def _utc(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _join(existing: str, new: str) -> str:
    return f"{existing}\n{new}" if existing and new else existing or new


def parse(text: str) -> tuple[ChapterMeta | None, str, dict[int, str]]:
    """Split a chapter file into (front matter, preamble before the first marker, {page: content}).

    `meta` is None for a legacy file written before front matter existed.
    """
    fields, text = _split_front_matter(text)
    meta = None
    if fields is not None:
        known = {k: fields.get(k, "") for k in _CHAPTER_KEYS}
        meta = ChapterMeta(
            chapter_name=known["chapter_name"],
            created_at=known["created_at"],
            updated_at=known["updated_at"],
            chapter_summary=known["chapter_summary"],
            extra={k: v for k, v in fields.items() if k not in _CHAPTER_KEYS},
        )
    preamble, pages = _sections(text, _MARKER)
    return meta, preamble, pages


def _sections(text: str, marker: re.Pattern[str]) -> tuple[str, dict[int, str]]:
    """Split text on numbered markers into (preamble, {number: content}); repeats are merged."""
    matches = list(marker.finditer(text))
    preamble = (text[: matches[0].start()] if matches else text).strip("\n")
    sections: dict[int, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        n = int(m.group(1))
        sections[n] = _join(sections.get(n, ""), text[m.end() : end].strip("\n"))
    return preamble, sections


def parse_laps(text: str) -> tuple[str, dict[int, str]]:
    """Split laps.md into (preamble before the first lap, {lap: content})."""
    return _sections(text, _LAP_MARKER)


def serialize_laps(preamble: str, laps: dict[int, str]) -> str:
    blocks = [preamble] if preamble else []
    blocks += [f"# lap {n}\n{laps[n]}".rstrip("\n") for n in sorted(laps)]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def serialize(meta: ChapterMeta | None, preamble: str, pages: dict[int, str]) -> str:
    blocks = []
    if meta is not None:
        fields = {
            "chapter_name": meta.chapter_name,
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "chapter_summary": meta.chapter_summary,
            "chapter_hash": chapter_hash(pages),
            **meta.extra,
        }
        blocks.append(_render_front_matter(fields))
    if preamble:
        blocks.append(preamble)
    blocks += [f"# page {n}, {page_hash(pages[n])}\n{pages[n]}".rstrip("\n") for n in sorted(pages)]
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


def _read_or_empty(path: Path) -> str:
    """Books created before manuscript.md / laps.md existed get the file on first write."""
    try:
        return _read(path)
    except FileNotFoundError:
        return ""


def _create_empty(path: Path) -> None:
    try:
        path.open("x", encoding="utf-8").close()  # exclusive create: never truncates a file
    except FileExistsError:
        pass


def _birth_time(path: Path) -> datetime:
    st = path.stat()
    return datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_mtime), UTC)


def _touch(meta: ChapterMeta | None, path: Path, chapter: int, now: str) -> ChapterMeta:
    """Set updated_at; a legacy file gets front matter on its first write after the upgrade."""
    if meta is None:
        created_at = _utc(_birth_time(path))
        meta = ChapterMeta(chapter_name=f"Chapter {chapter}", created_at=created_at, updated_at=now)
    meta.updated_at = now
    return meta


def _read_chapter(path: Path, chapter: int) -> tuple[ChapterMeta | None, str, dict[int, str]]:
    try:
        return parse(_read(path))
    except FileNotFoundError as e:
        raise ChapterNotFoundError(f"Chapter {chapter} not found") from e


def _single_block(page_edits: list[PageEdits]) -> PageEdits:
    """Laps and the manuscript have no pages: exactly one block, its page number ignored."""
    if len(page_edits) != 1:
        raise InvalidEditError("laps and manuscript take exactly one block of edits")
    return page_edits[0]


def _edit_unit(content: str, edits: PageEdits, where: str) -> str:
    """Apply one block to a lap / the manuscript if it still hashes to content_sha256."""
    if page_hash(content) != edits.content_sha256:
        raise PageChangedError(f"The {where} changed since the edits were computed")
    try:
        return edit_executor.apply(content, edits.instructions)
    except InvalidEditError as e:
        raise InvalidEditError(f"{where}, {e.message}") from e


class ChapterStore:
    """Books live at {root}/{author_id}/{book_slug}/ with a book.md, manuscript.md and laps.md,
    and chapters at {root}/{author_id}/{book_slug}/chapter-{n}.md.

    Writes are serialised per file with an asyncio.Lock, so this assumes a single process
    (one uvicorn worker). Multi-worker would need fcntl.flock behind the same methods.
    """

    def __init__(self, root: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._root = root.resolve()
        self._clock = clock
        self._locks: WeakValueDictionary[Path, asyncio.Lock] = WeakValueDictionary()

    def _resolve(self, *parts: str) -> Path:
        path = self._root.joinpath(*parts).resolve()
        if not path.is_relative_to(self._root):
            raise InvalidPathError("Resolved path escapes the data root")
        return path

    def book_path(self, author_id: str, book_slug: str) -> Path:
        return self._resolve(author_id, book_slug, "book.md")

    def chapter_path(self, author_id: str, book_slug: str, chapter: int) -> Path:
        return self._resolve(author_id, book_slug, f"chapter-{chapter}.md")

    def manuscript_path(self, author_id: str, book_slug: str) -> Path:
        return self._resolve(author_id, book_slug, "manuscript.md")

    def laps_path(self, author_id: str, book_slug: str) -> Path:
        return self._resolve(author_id, book_slug, "laps.md")

    def _now(self) -> str:
        return _utc(self._clock())

    def _lock_for(self, path: Path) -> asyncio.Lock:
        lock = self._locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[path] = lock
        return lock

    async def create_book(self, author_id: str, book_slug: str, book_name: str) -> BookMeta:
        """A legacy book folder (chapters but no book.md) is adopted, not rejected."""
        path = self.book_path(author_id, book_slug)
        async with self._lock_for(path):
            if path.exists():
                raise BookExistsError(f"Book '{book_slug}' already exists")
            meta = BookMeta(book_name=book_name, created_at=self._now())
            content = _render_front_matter(
                {"book_name": meta.book_name, "created_at": meta.created_at}
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(_atomic_write, path, content + "\n")
            for extra in (self.manuscript_path, self.laps_path):
                await asyncio.to_thread(_create_empty, extra(author_id, book_slug))
            return meta

    async def book_exists(self, author_id: str, book_slug: str) -> bool:
        """Folder, not book.md: legacy books count too."""
        return await asyncio.to_thread(self.book_path(author_id, book_slug).parent.is_dir)

    async def append_to_manuscript(self, author_id: str, book_slug: str, text: str) -> None:
        path = self.manuscript_path(author_id, book_slug)
        async with self._lock_for(path):
            await asyncio.to_thread(self._append_manuscript_sync, path, book_slug, text)

    @staticmethod
    def _append_manuscript_sync(path: Path, book_slug: str, text: str) -> None:
        if not path.parent.is_dir():
            raise BookNotFoundError(f"Book '{book_slug}' not found")
        _atomic_write(path, _join(_read_or_empty(path).strip("\n"), text) + "\n")

    async def read_manuscript(self, author_id: str, book_slug: str) -> str:
        """Whole manuscript, '' when not written yet. No lock: writes are atomic replaces."""
        path = self.manuscript_path(author_id, book_slug)
        return await asyncio.to_thread(self._read_book_file, path, book_slug)

    async def read_lap(self, author_id: str, book_slug: str, lap: int) -> tuple[str, list[int]]:
        """Returns (lap content, sorted lap numbers). No lock: writes are atomic replaces."""
        path = self.laps_path(author_id, book_slug)
        _, laps = parse_laps(await asyncio.to_thread(self._read_book_file, path, book_slug))
        if lap not in laps:
            raise LapNotFoundError(f"Lap {lap} not found")
        return laps[lap], sorted(laps)

    @staticmethod
    def _read_book_file(path: Path, book_slug: str) -> str:
        if not path.parent.is_dir():
            raise BookNotFoundError(f"Book '{book_slug}' not found")
        return _read_or_empty(path).strip("\n")

    async def read_laps(self, author_id: str, book_slug: str) -> dict[int, str]:
        """{lap: content}, {} when laps.md is missing or empty. No lock: writes are atomic."""
        path = self.laps_path(author_id, book_slug)
        return parse_laps(await asyncio.to_thread(self._read_book_file, path, book_slug))[1]

    async def apply_manuscript_edits(
        self, author_id: str, book_slug: str, page_edits: list[PageEdits]
    ) -> str:
        """Edits on the whole manuscript (one block; its page number is ignored)."""
        path = self.manuscript_path(author_id, book_slug)
        async with self._lock_for(path):
            return await asyncio.to_thread(
                self._apply_manuscript_sync, path, book_slug, _single_block(page_edits)
            )

    @classmethod
    def _apply_manuscript_sync(cls, path: Path, book_slug: str, edits: PageEdits) -> str:
        content = cls._read_book_file(path, book_slug)
        edited = _edit_unit(content, edits, "manuscript").strip("\n")
        _atomic_write(path, f"{edited}\n" if edited else "")
        return edited

    async def apply_lap_edits(
        self, author_id: str, book_slug: str, lap: int, page_edits: list[PageEdits]
    ) -> str:
        """Edits on one lap (one block; its page number is ignored)."""
        path = self.laps_path(author_id, book_slug)
        async with self._lock_for(path):
            return await asyncio.to_thread(
                self._apply_lap_sync, path, book_slug, lap, _single_block(page_edits)
            )

    @classmethod
    def _apply_lap_sync(cls, path: Path, book_slug: str, lap: int, edits: PageEdits) -> str:
        preamble, laps = parse_laps(cls._read_book_file(path, book_slug))
        if lap not in laps:
            raise LapNotFoundError(f"Lap {lap} not found")
        content = serialize_laps(
            preamble, {**laps, lap: _edit_unit(laps[lap], edits, f"lap {lap}")}
        )
        # Round-trip check, as for chapters: an edit must never add, merge or drop a lap.
        stored = parse_laps(content)[1]
        if stored.keys() != laps.keys() or any(stored[n] != laps[n] for n in laps if n != lap):
            raise InvalidEditError("edits would change the lap structure of laps.md")
        _atomic_write(path, content)
        return stored[lap]

    async def append_to_lap(self, author_id: str, book_slug: str, lap: int, text: str) -> None:
        path = self.laps_path(author_id, book_slug)
        async with self._lock_for(path):
            await asyncio.to_thread(self._append_lap_sync, path, book_slug, lap, text)

    @staticmethod
    def _append_lap_sync(path: Path, book_slug: str, lap: int, text: str) -> None:
        if not path.parent.is_dir():
            raise BookNotFoundError(f"Book '{book_slug}' not found")
        preamble, laps = parse_laps(_read_or_empty(path))
        laps[lap] = _join(laps.get(lap, ""), text)
        _atomic_write(path, serialize_laps(preamble, laps))

    async def create_chapter(
        self, author_id: str, book_slug: str, chapter: int, chapter_name: str
    ) -> tuple[ChapterMeta, str]:
        """Returns (meta, chapter_hash) of the new, empty chapter."""
        path = self.chapter_path(author_id, book_slug, chapter)
        async with self._lock_for(path):
            if not path.parent.is_dir():  # folder, not book.md: legacy books count too
                raise BookNotFoundError(f"Book '{book_slug}' not found")
            if path.exists():
                raise ChapterExistsError(f"Chapter {chapter} already exists")
            now = self._now()
            meta = ChapterMeta(chapter_name=chapter_name, created_at=now, updated_at=now)
            await asyncio.to_thread(_atomic_write, path, serialize(meta, "", {}))
            return meta, chapter_hash({})

    async def chapter_exists(self, author_id: str, book_slug: str, chapter: int) -> bool:
        return await asyncio.to_thread(self.chapter_path(author_id, book_slug, chapter).is_file)

    async def append_to_page(
        self, author_id: str, book_slug: str, chapter: int, page: int, text: str
    ) -> None:
        path = self.chapter_path(author_id, book_slug, chapter)
        async with self._lock_for(path):
            await asyncio.to_thread(self._append_sync, path, chapter, page, text, self._now())

    @staticmethod
    def _append_sync(path: Path, chapter: int, page: int, text: str, now: str) -> None:
        meta, preamble, pages = _read_chapter(path, chapter)
        meta = _touch(meta, path, chapter, now)
        pages[page] = _join(pages.get(page, ""), text)
        _atomic_write(path, serialize(meta, preamble, pages))

    async def read_page(
        self, author_id: str, book_slug: str, chapter: int, page: int
    ) -> tuple[str, list[int]]:
        """Returns (page content, sorted page numbers). No lock: writes are atomic replaces."""
        path = self.chapter_path(author_id, book_slug, chapter)
        try:
            text = await asyncio.to_thread(_read, path)
        except FileNotFoundError as e:
            raise ChapterNotFoundError(f"Chapter {chapter} not found") from e
        _, _, pages = parse(text)
        if page not in pages:
            raise PageNotFoundError(f"Page {page} not found in chapter {chapter}")
        return pages[page], sorted(pages)

    async def read_book(self, author_id: str, book_slug: str) -> dict[int, dict[int, str]]:
        """{chapter: {page: content}} for every chapter-N.md. No lock: writes are atomic."""
        folder = self.book_path(author_id, book_slug).parent
        return await asyncio.to_thread(self._read_book_sync, folder, book_slug)

    @staticmethod
    def _read_book_sync(folder: Path, book_slug: str) -> dict[int, dict[int, str]]:
        if not folder.is_dir():
            raise BookNotFoundError(f"Book '{book_slug}' not found")
        book: dict[int, dict[int, str]] = {}
        for path in folder.iterdir():
            m = _CHAPTER_FILE.fullmatch(path.name)
            if m is not None and path.is_file():
                book[int(m.group(1))] = parse(_read(path))[2]
        return dict(sorted(book.items()))

    async def book_outline(self, author_id: str, book_slug: str) -> dict[int, list[int]]:
        """{chapter: sorted page numbers}, sorted by chapter."""
        book = await self.read_book(author_id, book_slug)
        return {chapter: sorted(pages) for chapter, pages in book.items()}

    async def read_chapter_pages(
        self, author_id: str, book_slug: str, chapter: int
    ) -> dict[int, str]:
        path = self.chapter_path(author_id, book_slug, chapter)
        return (await asyncio.to_thread(_read_chapter, path, chapter))[2]

    async def apply_edits(
        self, author_id: str, book_slug: str, chapter: int, page_edits: list[PageEdits]
    ) -> dict[int, str]:
        """Apply offset edits to pages of one chapter, all or nothing; returns {page: content}.

        Under the same lock as dictation appends. Every page must still hash to the
        content_sha256 the edits were computed on, else 409 and nothing is written.
        """
        path = self.chapter_path(author_id, book_slug, chapter)
        async with self._lock_for(path):
            return await asyncio.to_thread(self._apply_sync, path, chapter, page_edits, self._now())

    @staticmethod
    def _apply_sync(
        path: Path, chapter: int, page_edits: list[PageEdits], now: str
    ) -> dict[int, str]:
        numbers = [p.page for p in page_edits]
        if len(set(numbers)) != len(numbers):
            raise InvalidEditError("each page may appear only once per chapter")
        meta, preamble, pages = _read_chapter(path, chapter)
        if missing := [n for n in numbers if n not in pages]:
            raise PageNotFoundError(f"Pages {missing} not found in chapter {chapter}")
        if changed := [p.page for p in page_edits if page_hash(pages[p.page]) != p.content_sha256]:
            raise PageChangedError(f"Pages {changed} changed since the edits were computed")

        edited = dict(pages)
        for p in page_edits:
            try:
                edited[p.page] = edit_executor.apply(pages[p.page], p.instructions)
            except InvalidEditError as e:
                raise InvalidEditError(f"page {p.page}, {e.message}") from e

        content = serialize(_touch(meta, path, chapter, now), preamble, edited)
        # Round-trip check: the file must parse back to the same pages, so an edit can never
        # add a phantom page or merge two (e.g. a marker built across an edit boundary).
        stored = parse(content)[2]
        if stored.keys() != pages.keys() or any(
            stored[n] != pages[n] for n in pages if n not in numbers
        ):
            raise InvalidEditError("edits would change the page structure of the chapter")
        _atomic_write(path, content)
        return {n: stored[n] for n in sorted(numbers)}
