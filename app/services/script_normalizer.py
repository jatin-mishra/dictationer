import logging
import re
import unicodedata

from app.schemas.dictation import Language
from app.services.audio import NoSpeechError

log = logging.getLogger(__name__)

# A transcribed line that looks like a page marker would create a phantom page in the chapter file.
_MARKER_LINE = re.compile(r"^#(?=\s*page\s+\d+)", re.IGNORECASE | re.MULTILINE)

_MIN_RATIO = {Language.HI: 0.90, Language.EN: 0.90, Language.HINGLISH: 0.95}


def _script(c: str) -> str:
    cp = ord(c)
    if 0x0900 <= cp <= 0x097F:
        return "devanagari"
    if cp < 0x0250:  # Basic Latin .. Latin Extended-B
        return "latin"
    return "other"


def _script_warnings(text: str, language: Language) -> list[str]:
    counts = {"devanagari": 0, "latin": 0, "other": 0}
    for c in text:
        if unicodedata.category(c)[0] in "LM":  # letters and combining marks (matras)
            counts[_script(c)] += 1
    total = sum(counts.values())
    if total == 0:
        return []

    dev, latin = counts["devanagari"] / total, counts["latin"] / total
    if language is Language.HINGLISH and counts["devanagari"] == 0:
        return ["script_mismatch: no Hindi (Devanagari) detected in hinglish text"]
    expected = {
        Language.HI: ("Devanagari", dev),
        Language.EN: ("Latin", latin),
        Language.HINGLISH: ("Devanagari + Latin", dev + latin),
    }
    name, ratio = expected[language]
    if ratio < _MIN_RATIO[language]:
        return [f"script_mismatch: expected {name}, got {ratio:.0%}"]
    return []


def guard(text: str) -> str:
    """The minimum before text may be stored: reject empty text, escape page-marker lines."""
    if not text.strip():
        raise NoSpeechError("Transcription is empty")
    return _MARKER_LINE.sub(r"\\#", text)


def normalize(text: str, language: Language) -> tuple[str, list[str]]:
    """NFC, collapse whitespace, escape page markers, and check the script (warn, never rewrite)."""
    text = unicodedata.normalize("NFC", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    text = guard("\n".join(line for line in lines if line))

    warnings = _script_warnings(text, language)
    if warnings:
        log.warning("script check failed for %s: %s", language.value, warnings)
    return text, warnings
