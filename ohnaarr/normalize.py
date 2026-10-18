"""Text repair and normalisation.

German releases break Sonarr's parser in a handful of recurring ways:

* mojibake - UTF-8 bytes written by the download client and read back as
  cp1252/latin-1, so "Mädchen" arrives as "MÃ¤dchen" (or "MÃ\u0083Â¤dchen" when it
  happened twice).
* HTML entities and percent escapes from indexer/NZB metadata ("M&auml;dchen",
  "M%C3%A4dchen").
* transliteration by the uploader - "Maedchen" where the TVDB title is
  "Mädchen", or "Madchen" where the umlaut was simply dropped.
* decomposed Unicode - "a" + U+0308 instead of "ä", which compares unequal to
  Sonarr's NFC titles.

`repair` undoes the encoding damage. `fold_ae` / `fold_plain` produce the two
comparison forms needed to match a repaired title against Sonarr's titles no
matter which direction the uploader transliterated.
"""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import unquote

# Characters that only appear in cp1252-decoded UTF-8 of Latin text.
_MOJIBAKE_MARKERS = re.compile(r"[ÃÂ][\u0080-¿ -∞ŒšŽŸ]")

# German-specific folding to the conventional two-letter transliteration.
_AE_MAP = {
    "ä": "ae", "ö": "oe", "ü": "ue",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "ß": "ss", "ẛ": "ss", "ſ": "s",
}

# Tokens that describe the release, not the content. Stripped before title matching.
_RELEASE_NOISE = re.compile(
    r"\b("
    r"\d{3,4}p|\d{3,4}i|[xh]\.?26[45]|hevc|avc|xvid|divx|av1|vp9|"
    r"web[\-. ]?dl|webrip|web|bluray|blu[\-. ]?ray|bdrip|brrip|dvdrip|dvd|hdtv|sdtv|pdtv|tvrip|"
    r"remux|hdr10\+?|hdr|dolby[\-. ]?vision|dv|sdr|imax|uhd|hd|sd|"
    r"ddp?[\-. ]?[57][\-. ]?1|dts(?:[\-. ]?hd)?(?:[\-. ]?ma)?|truehd|atmos|eac3|ac3|aac(?:2[\-. ]?0)?|flac|mp3|opus|"
    r"dl|dual|multi|ml|synced|repack|proper|internal|extended|uncut|unrated|"
    r"remastered|complete|retail|custom|subbed|dubbed|"
    r"german|deutsch|ger|eng|english|engsub|gersub|subs?|"
    # streaming/broadcast source tags
    r"nf|netflix|amzn|dsnp|atvp|hulu|hmax|max|stan|itv|ip|crav|pcok|"
    r"zdf|ard|srf|orf|rtl|sky|sat1|prosieben|vox|kabel1|joyn|wow|"
    r"ger[\-. ]?dub|dubbed|omu|"
    r"nfo|fix|read[\-. ]?nfo"
    r")\b",
    re.IGNORECASE,
)

_BRACKETED = re.compile(r"[\[\(\{][^\[\]\(\)\{\}]*[\]\)\}]")
_SEPARATORS = re.compile(r"[._\-+]+")
_MULTISPACE = re.compile(r"\s{2,}")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def repair(text: str) -> str:
    """Undo encoding damage in a file or release name.

    Idempotent and lossless for already-correct input.
    """
    if not text:
        return ""

    out = text

    # Percent escapes ("%C3%A4") sometimes survive from indexer metadata.
    if "%" in out:
        try:
            decoded = unquote(out, errors="strict")
            if decoded != out and "�" not in decoded:
                out = decoded
        except (UnicodeDecodeError, ValueError):
            pass

    # HTML entities ("&auml;", "&#228;").
    if "&" in out:
        unescaped = html.unescape(out)
        if "�" not in unescaped:
            out = unescaped

    out = _fix_mojibake(out)

    # Normalise to composed form so "a"+U+0308 equals "ä".
    out = unicodedata.normalize("NFC", out)

    # Strip zero-width and control characters that break path comparisons.
    out = "".join(
        ch for ch in out
        if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co")
        or ch in "\t\n"
    )

    return out.strip()


# "Â" immediately before a Latin-1 punctuation/letter byte is always an
# artifact of UTF-8 read as cp1252; it also appears when the C1 control that
# belongs to a double mis-decode was stripped somewhere along the way
# ("MÃ<0x83>Â¤dchen" -> "MÃÂ¤dchen").
_STRAY_A_CIRCUMFLEX = re.compile(r"Â(?=[ -ÿ])")


def _fix_mojibake(text: str) -> str:
    """Round-trip cp1252/latin-1 mis-decodes back to UTF-8, up to three layers."""
    out = text
    for _ in range(4):
        if not _MOJIBAKE_MARKERS.search(out):
            break
        candidate = _roundtrip(out)
        if candidate is None:
            # A stripped C1 control can block the round-trip; drop the filler
            # "Â" and try once more.
            stripped = _STRAY_A_CIRCUMFLEX.sub("", out)
            if stripped == out:
                break
            candidate = _roundtrip(stripped)
            if candidate is None:
                break
        if candidate == out:
            break
        out = candidate
    return out


def _roundtrip(text: str) -> str | None:
    for codec in ("cp1252", "latin-1"):
        try:
            decoded = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if "�" in decoded:
            continue
        return decoded
    return None


def fold_ae(text: str) -> str:
    """Comparison key using German two-letter transliteration (ä -> ae)."""
    repaired = repair(text)
    mapped = "".join(_AE_MAP.get(ch, ch) for ch in repaired)
    stripped = _strip_diacritics(mapped)
    return _collapse(stripped)


def fold_plain(text: str) -> str:
    """Comparison key dropping diacritics entirely (ä -> a).

    Covers uploaders who strip the umlaut instead of transliterating it.
    """
    return _collapse(_strip_diacritics(repair(text)))


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _collapse(text: str) -> str:
    out = text.lower()
    out = _SEPARATORS.sub(" ", out)
    out = _NON_WORD.sub(" ", out)
    out = _MULTISPACE.sub(" ", out)
    return out.strip()


def fold_keys(text: str) -> tuple[str, str]:
    """Both comparison keys for `text`, deduplicated by the caller if equal."""
    return fold_ae(text), fold_plain(text)


def clean_release_title(text: str, *, keep_years: bool = False) -> str:
    """Human-readable title with release noise removed.

    Used for `/parse` retries and as the haystack for title matching.
    `keep_years=True` is for text already known to be only the title, where a
    four-digit group belongs to the title itself ("2012", "Blade Runner 2049").
    """
    out = repair(text)
    out = _BRACKETED.sub(" ", out)
    out = _SEPARATORS.sub(" ", out)
    out = _RELEASE_NOISE.sub(" ", out)
    if not keep_years:
        out = re.sub(r"\b(19|20)\d{2}\b", " ", out)  # release year
    out = re.sub(r"[-\s]+$", "", out)
    out = _MULTISPACE.sub(" ", out)
    return out.strip(" -_.")


def strip_extension(name: str) -> str:
    return re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name)


def normalised_filename(name: str) -> str:
    """Repaired filename, safe to hand back to Sonarr's parser.

    Umlauts are transliterated because Sonarr's own `/parse` is more reliable on
    ASCII, while the rest of the name keeps its structure (separators, S/E tags).
    """
    repaired = repair(name)
    return "".join(_AE_MAP.get(ch, ch) for ch in repaired)


def apply_replacements(text: str, pairs: list[tuple[str, str]]) -> str:
    """Literal, case-insensitive substitutions from the overrides file."""
    out = text
    for needle, replacement in pairs:
        if not needle:
            continue
        out = re.sub(re.escape(needle), replacement, out, flags=re.IGNORECASE)
    return out


def similarity_candidates(text: str) -> list[str]:
    """Distinct comparison keys for `text`, longest first."""
    keys = {k for k in fold_keys(text) if k}
    return sorted(keys, key=len, reverse=True)
