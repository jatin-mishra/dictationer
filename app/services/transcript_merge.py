"""Join per-segment transcripts, dropping the text spoken twice in the overlapping audio.

Engines return plain text without timestamps, so the overlap is found on words: the longest run
of words (compared case- and punctuation-insensitively) shared by the tail of the text so far and
the head of the next segment. Words cut at a segment edge are often garbled, so the run may end up
to `max_edge_words` before the end of the previous text and start up to `max_edge_words` into the
next one; those edge words are dropped, keeping each side's clean copy of the audio.
"""

import logging
import unicodedata
from collections.abc import Sequence

log = logging.getLogger(__name__)


def _norm(word: str) -> str:
    # Drop only punctuation (P*): Devanagari vowel signs are marks (M*) and must stay.
    word = unicodedata.normalize("NFC", word).casefold()
    return "".join(ch for ch in word if not unicodedata.category(ch).startswith("P"))


def _overlap(
    prev: list[str], nxt: list[str], window: int, min_words: int, max_edge: int
) -> tuple[int, int, int] | None:
    """Best shared run as (words to drop from the end of prev, from the start of nxt, run length).

    Longest run wins; ties go to the run closest to the cut. None if no run of >= min_words.
    """
    tail = [_norm(w) for w in prev[-window:]]
    head = [_norm(w) for w in nxt[:window]]
    best: tuple[int, int, int] | None = None
    best_key = (0, 0)
    for a in range(len(tail)):
        for b in range(min(max_edge + 1, len(head))):
            k = 0
            while a + k < len(tail) and b + k < len(head) and tail[a + k] == head[b + k]:
                k += 1
            prev_drop = len(tail) - a - k
            key = (k, -(prev_drop + b))
            if k >= min_words and prev_drop <= max_edge and key > best_key:
                best, best_key = (prev_drop, b, k), key
    return best


def merge(
    texts: Sequence[str], max_overlap_words: int, min_overlap_words: int, max_edge_words: int
) -> str:
    words: list[str] = []
    for i, text in enumerate(texts):
        nxt = text.split()
        if not nxt:
            continue
        if not words:
            words = nxt
            continue
        found = _overlap(words, nxt, max_overlap_words, min_overlap_words, max_edge_words)
        if found is None:
            log.debug("no overlap found before segment %d; joining as-is", i)
            words.extend(nxt)
            continue
        prev_drop, next_drop, k = found
        del words[len(words) - prev_drop :]
        words.extend(nxt[next_drop + k :])
    return " ".join(words)
