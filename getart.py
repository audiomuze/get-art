#!/usr/bin/env python3
"""
Apple Music Artwork Downloader
Retrieves high-quality artwork from Apple Music using artist/album information.
"""

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse
from urllib.request import HTTPError, Request, urlopen

try:
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover - Python < 3.8 fallback
    import importlib_metadata  # type: ignore

try:
    from mutagen import File as MutagenFile
except ImportError:  # pragma: no cover - optional dependency
    MutagenFile = None

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - optional dependency
    fuzz = None

try:
    import difPy  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    difPy = None  # type: ignore

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - optional dependency
        tomllib = None  # type: ignore


SUPPORTED_AUDIO_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".m4b",
    ".m4p",
    ".mp4",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".wv",
    ".wma",
    ".aiff",
    ".aif",
    ".aifc",
    ".ape",
    ".alac",
    ".dsf",
    ".dff"
}

FUZZY_SCORE_THRESHOLD = 90.0
ARTIST_FUZZY_THRESHOLD = 96.0

ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"

ARTWORK_TARGET_STEMS = {"folder", "cover", "xfolder", "xfolder_fallback"}
ARTWORK_VALID_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
DEFAULT_CANONICAL_ARTWORK_STEM = "folder"
NOT_DUPE_PREFIX = "_not_dupe_"
DEFAULT_DEDUPE_SIMILARITY = 200
DEFAULT_DEDUPE_PX_SIZE = 100


def _format_rate_limit_tag(delay_seconds: float) -> str:
    """Return a colored rate-limit tag showing the enforced delay."""
    secs = max(delay_seconds, 0.0)
    secs_str = f"{secs:.1f}".rstrip('0').rstrip('.') if secs else "0"
    return f"[{ANSI_RED}Rate Limited, {secs_str}s{ANSI_RESET}]"

CONFIG_FILE = Path(__file__).with_name("getart.toml")
CONFIG_SECTION = "preferences"
CONFIG_DEFAULTS: dict[str, Any] = {
    "verbose": False,
    "throttle": 1.0,
    "ignore_log": False,
    "overwrite": False,
    "retry": False,
    "retry_only": False,
    "retry_fallbacks": False,
    "fallback_only": False,
    "dry_run": False,
    "allow_artist_only_match": False,
    "dedupe_artwork": True,
    "canonical_artwork_stem": DEFAULT_CANONICAL_ARTWORK_STEM,
}

CONFIG_TEMPLATE = """# get-art preferences
# Command-line options take precedence over these defaults.
[preferences]
verbose = false
throttle = 1.0
ignore_log = false
overwrite = false
retry = false
retry_only = false
retry_fallbacks = false
fallback_only = false
dry_run = false
allow_artist_only_match = false
dedupe_artwork = true
canonical_artwork_stem = "folder"
"""


def _ensure_config_file(path: Path = CONFIG_FILE) -> None:
    """Create a default config file when one is missing."""
    if path.exists():
        return

    try:
        path.write_text(CONFIG_TEMPLATE + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk permission issues
        print(f"Warning: Could not create default config file at {path}: {exc}")


def _load_preferences() -> dict[str, Any]:
    """Load user preferences from the TOML config file."""
    defaults = CONFIG_DEFAULTS.copy()

    _ensure_config_file()
    if tomllib is None:
        return defaults

    try:
        with open(CONFIG_FILE, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return defaults
    except Exception as exc:  # pragma: no cover - corrupt config
        print(f"Warning: Could not parse config file {CONFIG_FILE}: {exc}")
        return defaults

    section = data.get(CONFIG_SECTION)
    if isinstance(section, dict):
        for key in defaults:
            if key in section and section[key] is not None:
                defaults[key] = section[key]
    defaults["canonical_artwork_stem"] = _normalize_canonical_stem(
        defaults.get("canonical_artwork_stem")
    )
    return defaults


def _normalize_canonical_stem(value: Any) -> str:
    """Return a sanitized filename stem for canonical artwork outputs."""
    candidate = DEFAULT_CANONICAL_ARTWORK_STEM
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            stem, _ = os.path.splitext(cleaned)
            candidate = stem or cleaned

    candidate = re.sub(r'[<>:"/\\|?*]', '_', candidate)
    candidate = re.sub(r"\s+", '_', candidate).strip('_')
    return candidate or DEFAULT_CANONICAL_ARTWORK_STEM


def _resolve_script_identity() -> tuple[str, str]:
    """Return a tuple of (script name, version string)."""
    script_name = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else (
        os.path.basename(__file__) if "__file__" in globals() else "getart.py"
    )

    env_version = os.environ.get("GETART_VERSION")
    if env_version:
        return script_name, env_version

    try:
        version = importlib_metadata.version("get-art")
    except Exception:
        version = "local-dev"

    return script_name, version


SCRIPT_NAME, SCRIPT_VERSION = _resolve_script_identity()


class RateLimitExceededError(RuntimeError):
    """Raised when Apple Music continues throttling after enforced backoff."""


class AppleMusicArtworkDownloader:
    """Self-contained Apple Music artwork downloader"""

    def __init__(self, verbose: bool = False, throttle: float = 1,
                 allow_artist_only_match: bool = False):
        """
        Initialize the downloader.

        Args:
            verbose: Enable detailed logging
            throttle: Seconds to wait if rate-limited
            allow_artist_only_match: Permit saving the first artist-only match
                when album/title comparisons fail
        """
        self.verbose = verbose
        self.throttle = throttle
        self.allow_artist_only_match = allow_artist_only_match

        # Configuration matching your defaults
        self.ART_SIZE = 9999
        self.ART_QUALITY = 100

        # Build the quality suffix
        quality_suffix = "bb" if self.ART_QUALITY == 0 else f"-{self.ART_QUALITY}"
        self.file_suffix = f"{self.ART_SIZE}x{self.ART_SIZE}{quality_suffix}"

        # HTTP settings
        self.USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.55 Safari/537.36"
        self.THROTTLED_HTTP_CODES = [403, 429]
        self.MAX_RETRIES = 5
        self.rate_limit_delay = 0.0
        self.rate_limit_escalated = False
        self.last_match_type = None
        self.last_query_entity = None

        if self.verbose:
            print(f"Initialized with size={self.ART_SIZE}, quality={self.ART_QUALITY}")
            if fuzz is None:
                print("RapidFuzz not available; fuzzy scoring will fall back to simple overlap checks.")

    @property
    def current_delay(self) -> float:
        """Return the effective delay to honor between requests."""
        return max(self.throttle, self.rate_limit_delay)

    def _enter_rate_limit_mode(self, url: str) -> bool:
        """Handle first-time rate limiting, enforcing five second delays."""
        if self.rate_limit_escalated:
            return False

        wait_time = 5.0
        self.rate_limit_escalated = True
        self.rate_limit_delay = max(self.rate_limit_delay, wait_time)
        if self.verbose:
            host = urlparse(url).netloc
            print(
                f"Apple Music throttled responses from {host}; waiting {wait_time:.0f}s and enabling {wait_time:.0f}s inter-request delays"
            )
        time.sleep(wait_time)
        return True

    def _urlopen_safe(self, url: str) -> bytes:
        """Make HTTP request with bounded retry/backoff handling"""
        attempts = 0

        while True:
            try:
                req = Request(url)
                req.add_header("User-Agent", self.USER_AGENT)
                response = urlopen(req)
                return response.read()
            except HTTPError as e:
                if e.code in self.THROTTLED_HTTP_CODES:
                    if self._enter_rate_limit_mode(url):
                        continue
                    raise RateLimitExceededError(
                        "Apple Music is still throttling requests after enforced delay. Please resume later."
                    )

                attempts += 1
                if attempts <= self.MAX_RETRIES:
                    wait_time = max(self.current_delay, 1.0) * (2 ** (attempts - 1))
                    if self.verbose:
                        print(
                            f"HTTP Error {e.code} for {url}: {e.reason}. Retrying in {wait_time:.1f}s"
                        )
                    time.sleep(wait_time)
                    continue

                if self.verbose:
                    print(f"HTTP Error {e.code} for {url}: {e.reason}")
                raise
            except Exception as e:
                if self.verbose:
                    print(f"Error accessing {url}: {str(e)}")
                raise

    def _query_itunes(self, artist: str, album: str = None, title: str = None) -> dict:
        """Query iTunes Search API for music metadata."""
        token = album or title or ""
        query_term = f"{artist} {token}".strip()

        # Prefer album search when album metadata is available, but fall back to
        # track search if Apple has not indexed the album yet (e.g., preorders).
        entity_sequence = ["album"] if album else ["musicTrack"]
        if album:
            entity_sequence.append("musicTrack")

        last_response = {}
        for idx, entity in enumerate(entity_sequence):
            url = (
                f"https://itunes.apple.com/search?term={quote(query_term)}"
                f"&media=music&entity={entity}"
            )

            if self.verbose:
                if idx == 0:
                    print(f"Searching iTunes API ({entity}): {artist} - {token}")
                else:
                    print(
                        "Primary album search returned no results; retrying as track search"
                    )

            try:
                raw_json = self._urlopen_safe(url).decode("utf8")
                info = json.loads(raw_json)
            except json.JSONDecodeError as e:
                if self.verbose:
                    print(f"Failed to parse JSON response: {e}")
                info = {}
            except RateLimitExceededError:
                raise
            except Exception as e:
                if self.verbose:
                    print(f"Error querying iTunes: {e}")
                info = {}

            last_response = info or {}
            if last_response.get('resultCount'):
                self.last_query_entity = entity
                return last_response

        # No results from any attempt; record the last entity we asked for.
        self.last_query_entity = entity_sequence[-1]
        return last_response

    def _find_best_artwork_url(self, results: list, artist: str, album: str = None,
                               title: str = None) -> tuple[str, str]:
        """Find the best matching artwork URL and classify match strictness."""
        if not results:
            return None, None

        artist_lower = artist.lower()
        album_lower = album.lower() if album else None
        title_lower = title.lower() if title else None

        def normalize(text: str) -> str:
            return (text or "").strip().lower()

        def is_overlap(target: str, candidate: str) -> bool:
            return bool(target and candidate and (
                target == candidate or
                target in candidate or
                candidate in target
            ))

        def tokenize(text: str) -> set[str]:
            if not text:
                return set()
            return set(re.findall(r"[a-z0-9']+", text))

        def format_art_url(raw_url: str) -> str:
            if not raw_url:
                return None
            return raw_url.replace('100x100bb', self.file_suffix)

        artist_tokens = tokenize(artist_lower)

        def artist_matches(result_artist_lower: str) -> bool:
            if not result_artist_lower:
                return False
            if artist_lower == result_artist_lower:
                return True

            direct_pattern = rf"\b{re.escape(artist_lower)}\b"
            if re.search(direct_pattern, result_artist_lower):
                return True

            reverse_pattern = rf"\b{re.escape(result_artist_lower)}\b"
            if re.search(reverse_pattern, artist_lower):
                return True

            if fuzz:
                score = float(fuzz.token_set_ratio(artist_lower, result_artist_lower))
                return score >= ARTIST_FUZZY_THRESHOLD

            candidate_tokens = tokenize(result_artist_lower)
            if not candidate_tokens or not artist_tokens:
                return False
            if artist_tokens == candidate_tokens:
                return True
            return (
                artist_tokens.issubset(candidate_tokens) or
                candidate_tokens.issubset(artist_tokens)
            )

        def fuzzy_ratio(text_a: str, text_b: str) -> float:
            if not text_a or not text_b:
                return 0.0
            if fuzz:
                return float(fuzz.token_set_ratio(text_a, text_b))
            return 100.0 if is_overlap(text_a, text_b) else 0.0

        if album_lower:
            best_fuzzy_candidate = (None, None, 0.0)
            first_artist_match = None

            for result in results:
                result_artist_raw = result.get('artistName', '')
                result_artist_lower = normalize(result_artist_raw)

                if not artist_matches(result_artist_lower):
                    continue

                art_url = format_art_url(result.get('artworkUrl100', ''))
                if not art_url:
                    continue

                result_album_raw = result.get('collectionName', '')
                result_album_lower = normalize(result_album_raw)

                if album_lower == result_album_lower:
                    if self.verbose:
                        print(f"Found exact album match: {result_artist_raw} - {result_album_raw}")
                    return art_url, "exact"

                score = fuzzy_ratio(album_lower, result_album_lower)
                if score >= FUZZY_SCORE_THRESHOLD and score > best_fuzzy_candidate[2]:
                    best_fuzzy_candidate = (art_url, "fuzzy", score)

                if not first_artist_match:
                    first_artist_match = (art_url, "artist")

            if best_fuzzy_candidate[0]:
                return best_fuzzy_candidate[0], best_fuzzy_candidate[1]
            if self.allow_artist_only_match and first_artist_match:
                return first_artist_match
            return None, None

        # No album specified; match on artist and optionally title
        best_fuzzy_candidate = (None, None, 0.0)
        first_artist_match = None

        for result in results:
            result_artist_raw = result.get('artistName', '')
            result_artist_lower = normalize(result_artist_raw)

            if not artist_matches(result_artist_lower):
                continue

            art_url = format_art_url(result.get('artworkUrl100', ''))
            if not art_url:
                continue

            if title_lower:
                result_title_raw = result.get('trackName', '')
                result_title_lower = normalize(result_title_raw)

                if title_lower == result_title_lower:
                    if self.verbose:
                        print(f"Found exact track match: {result_artist_raw} - {result_title_raw}")
                    return art_url, "exact"

                score = fuzzy_ratio(title_lower, result_title_lower)
                if score >= FUZZY_SCORE_THRESHOLD and score > best_fuzzy_candidate[2]:
                    best_fuzzy_candidate = (art_url, "fuzzy", score)

            if not first_artist_match:
                first_artist_match = (art_url, "artist")

        if best_fuzzy_candidate[0]:
            return best_fuzzy_candidate[0], best_fuzzy_candidate[1]
        if self.allow_artist_only_match and first_artist_match:
            return first_artist_match
        return None, None

    def get_artwork(self, artist: str, album: str = None, title: str = None) -> bytes:
        """
        Retrieve artwork from Apple Music.

        Args:
            artist: Artist name
            album: Album name (optional)
            title: Track title (optional, used if album is None)

        Returns:
            bytes: Raw image data, or None if not found
        """
        self.last_match_type = None

        if self.verbose:
            print(f"\nSearching for artwork: Artist='{artist}', Album='{album}', Title='{title}'")

        # Query iTunes for the artist/album
        info = self._query_itunes(artist, album, title)

        if not info or not info.get('resultCount', 0):
            if self.verbose:
                print("No results found in iTunes search")
            return None

        results = info.get('results', [])
        if self.verbose:
            print(f"Found {len(results)} result(s)")

        # Find the best matching artwork URL
        art_url, match_type = self._find_best_artwork_url(results, artist, album, title)

        if not art_url:
            if self.verbose:
                print("No suitable artwork URL found")
            return None

        self.last_match_type = match_type or "exact"

        # Download the artwork
        try:
            if self.verbose:
                print(f"Downloading artwork from: {art_url}")

            image_data = self._urlopen_safe(art_url)

            if self.verbose:
                print(f"Successfully downloaded {len(image_data):,} bytes")

            return image_data
        except RateLimitExceededError:
            raise
        except Exception as e:
            if self.verbose:
                print(f"Error downloading artwork: {e}")
            return None

    def save_artwork(self, artist: str, album: str = None, title: str = None,
                    filename: str = "xfolder.jpg") -> bool:
        """
        Retrieve and save artwork in one step.

        Args:
            artist: Artist name
            album: Album name (optional)
            title: Track title (optional)
            filename: Output filename (default: xfolder.jpg)

        Returns:
            bool: True if successful, False otherwise
        """
        image_data = self.get_artwork(artist, album, title)

        if not image_data:
            if self.verbose:
                print("Failed to retrieve artwork")
            return False

        try:
            with open(filename, "wb") as f:
                f.write(image_data)

            if self.verbose:
                print(f"Artwork saved to: {filename}")

            return True
        except Exception as e:
            if self.verbose:
                print(f"Error saving artwork to {filename}: {e}")
            return False


def parse_folder_name(folder_name: str):
    """
    Parse folder name in format "Artist - Album" or "Artist - Album [Extra Info]"

    Args:
        folder_name: Folder name to parse

    Returns:
        tuple: (artist, album) or (None, None) if format is invalid
    """
    # Remove any trailing slash
    folder_name = folder_name.rstrip('/\\')

    # Split by " - " (hyphen with spaces)
    if ' - ' not in folder_name:
        return None, None

    parts = folder_name.split(' - ', 1)
    artist = re.sub(r'\s+', ' ', parts[0]).strip()
    album = _normalize_album_text(parts[1])

    return artist, album


def _normalize_album_text(album_text: str) -> str:
    """Normalize album metadata by stripping quality/format noise."""
    if not album_text:
        return ""

    cleaned = re.sub(r'\s*\[.*?\]\s*', ' ', album_text).strip()
    cleaned = _strip_year_parentheses(cleaned)
    cleaned = _strip_quality_parentheses(cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    cleaned = _remove_audio_format_tokens(cleaned)
    cleaned = _strip_quality_suffixes(cleaned)
    return cleaned.strip()


# Descriptors that represent file/encoding quality rather than the actual album title
YEAR_PAREN_PATTERN = re.compile(r'\s*\((?:19|20)\d{2}\)\s*')


QUALITY_KEYWORDS = (
    "hi-res",
    "hi res",
    "hi-resolution",
    "hi definition",
    "hi-def",
    "high-res",
    "24bit",
    "32bit",
    "24/96",
    "24/192",
    "24-96",
    "24-192",
    "24 96",
    "24 192",
    "dsd",
    "mqa",
    "sacd",
    "uhd",
    "uhq"
)

FORMAT_KEYWORDS = (
    "flac",
    "alac",
    "aac",
    "mp3",
    "mp4",
    "m4a",
    "m4b",
    "ogg",
    "opus",
    "wav",
    "wave",
    "aiff",
    "aif",
    "dsf",
    "dff",
    "ape",
    "wv",
    "wma",
    "pcm",
    "cd",
    "vinyl",
    "blu-ray",
    "bluray",
    "dvd"
)

QUALITY_SUFFIX_PATTERN = re.compile(
    r'(?:\s*[-–—]\s*)?(?:' +
    r'|'.join(re.escape(term) for term in QUALITY_KEYWORDS) +
    r')\s*$',
    re.IGNORECASE
)

AUDIO_FORMAT_PATTERN = re.compile(
    r'(?<![^\W_])'                    # not preceded by a letter/digit (underscore allowed)
    r'(?:\s*[-–—_/]*\s*)?'           # optional separators like " - ", "_", "/"
    r'(?:' +
    r'|'.join(re.escape(term) for term in FORMAT_KEYWORDS) +
    r')'
    r'(?:\s+(?:audio|rip|version))?'  # optional suffix words
    r'(?![^\W_])',                    # not followed by a letter/digit
    re.IGNORECASE
)


def _strip_quality_suffixes(text: str) -> str:
    """Remove trailing format/quality descriptors (Hi-Res, 24Bit, etc.)."""
    cleaned = text
    # Remove successive quality descriptors if multiple are appended.
    while True:
        new = QUALITY_SUFFIX_PATTERN.sub('', cleaned).strip()
        if new == cleaned:
            break
        cleaned = new
    return re.sub(r'\s+', ' ', cleaned)


def _strip_year_parentheses(text: str) -> str:
    """Remove standalone four-digit year markers enclosed in parentheses."""
    return YEAR_PAREN_PATTERN.sub(' ', text)


def _looks_like_quality_note(note: str) -> bool:
    normalized = note.strip().lower()
    if not normalized:
        return False

    # Compare using token-ish matching so "ape" does not match inside "shape".
    normalized = re.sub(r'[-–—]', ' ', normalized)
    for keyword in QUALITY_KEYWORDS + FORMAT_KEYWORDS:
        keyword_norm = keyword.lower().replace('-', ' ')
        pattern = r'(?<![^\W_])' + re.escape(keyword_norm) + r'(?![^\W_])'
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return True
    return False


def _strip_quality_parentheses(text: str) -> str:
    """Remove parentheses that only describe format/quality, keep others."""
    def replacer(match):
        inner = match.group(1)
        return ' ' if _looks_like_quality_note(inner) else f"({inner})"

    return re.sub(r'\(([^)]*)\)', replacer, text)


def _remove_audio_format_tokens(text: str) -> str:
    """Remove standalone audio format tokens wherever they appear."""
    cleaned = AUDIO_FORMAT_PATTERN.sub(' ', text)

    # If the format token was attached via separators (e.g. "Album_FLAC" or
    # "Album - FLAC"), the substitution can leave trailing/isolated separators.
    # Remove only separators that are adjacent to whitespace or at the end so we
    # don't clobber legitimate in-word punctuation (e.g. "Spider-Man").
    cleaned = re.sub(r'[_–—\-./\\]+(?=\s)', ' ', cleaned)
    cleaned = re.sub(r'(?<=\s)[_–—\-./\\]+', ' ', cleaned)
    cleaned = re.sub(r'[_–—\-./\\]+$', '', cleaned)

    return re.sub(r'\s+', ' ', cleaned).strip()


def sanitize_filename(name: str) -> str:
    """Make filename safe for most filesystems while preserving readability."""
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    return sanitized or "xfolder.jpg"


def _append_suffix_to_filename(path: str, suffix: str = "_fallback") -> str:
    """Return a new path with suffix inserted before the extension."""
    directory, filename = os.path.split(path)
    base, ext = os.path.splitext(filename or "xfolder.jpg")
    if not ext:
        ext = ".jpg"
    new_name = f"{base}{suffix}{ext}"
    return os.path.join(directory, new_name)


def _finalize_output_path(path: str, match_type: str) -> tuple[str, bool]:
    """Rename artwork to *_fallback when Apple returned only a partial/artist/fuzzy match."""
    if match_type not in {"partial", "artist", "fuzzy"}:
        return path, False

    fallback_path = _append_suffix_to_filename(path)
    if fallback_path == path:
        return path, True

    try:
        if os.path.exists(path):
            os.replace(path, fallback_path)
    except FileNotFoundError:
        pass
    return fallback_path, True


def _collect_artwork_candidates(
    folder: Path,
    canonical_stem: str = DEFAULT_CANONICAL_ARTWORK_STEM,
) -> list[Path]:
    """Return artwork files that match the canonical stems/extensions."""
    allowed_stems = {stem.lower() for stem in ARTWORK_TARGET_STEMS}
    normalized_canonical = _normalize_canonical_stem(canonical_stem).lower()
    allowed_stems.add(normalized_canonical)
    allowed_stems.add(f"{normalized_canonical}_fallback")

    candidates: list[Path] = []
    try:
        for entry in folder.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in ARTWORK_VALID_EXTS:
                continue
            if entry.stem.lower() in allowed_stems:
                candidates.append(entry)
    except FileNotFoundError:
        return []
    return candidates


def dedupe_artwork_variants(folder_path: str, *, similarity: int = DEFAULT_DEDUPE_SIMILARITY,
                            px_size: int = DEFAULT_DEDUPE_PX_SIZE,
                            canonical_stem: str = DEFAULT_CANONICAL_ARTWORK_STEM,
                            dry_run: bool = False, verbose: bool = False) -> tuple[str | None, list[str]]:
    """Promote the highest-quality canonical artwork inside folder_path.

    Returns (canonical_path, messages). canonical_path is None when no eligible
    artwork exists or when operations failed; messages describe performed steps.
    """
    folder = Path(folder_path)
    messages: list[str] = []
    canonical_stem = _normalize_canonical_stem(canonical_stem)

    def _log(message: str) -> None:
        if verbose:
            messages.append(message)
    if not folder.is_dir():
        return None, messages

    candidates = _collect_artwork_candidates(folder, canonical_stem)
    if not candidates:
        return None, messages

    if len(candidates) >= 2:
        if difPy is None:
            _log("DEDUP: difPy not installed; skipping similarity checks")
        else:
            try:
                file_paths = [str(p) for p in candidates]
                search = difPy.build(file_paths, in_folder=False, px_size=px_size)
                results = difPy.search(search, similarity=cast(Any, similarity), same_dim=False)
                lower_quality = [
                    Path(file_path) for file_path in getattr(results, "lower_quality", [])
                    if Path(file_path).parent == folder
                ]
            except Exception as exc:  # pragma: no cover - difPy runtime errors
                lower_quality = []
                _log(f"DEDUP: difPy error; skipping quality-based deletions ({exc})")

            if lower_quality:
                for file_path in lower_quality:
                    if dry_run:
                        _log(f"[-] Would delete lower-res duplicate: {file_path.name}")
                        continue
                    try:
                        file_path.unlink()
                        _log(f"[-] Deleting lower-res duplicate: {file_path.name}")
                    except Exception as exc:  # pragma: no cover - filesystem issues
                        _log(f"[!] Error deleting {file_path.name}: {exc}")

    remaining = _collect_artwork_candidates(folder, canonical_stem)
    if not remaining:
        return None, messages

    def _file_size(entry: Path) -> int:
        try:
            return entry.stat().st_size
        except OSError:
            return 0

    remaining.sort(key=_file_size, reverse=True)
    best_file = remaining[0]
    suffix = best_file.suffix or ".jpg"
    canonical_target = folder / f"{canonical_stem}{suffix}"

    def _rename(source: Path, destination: Path, description: str):
        if dry_run:
            _log(f"[*] Would {description}: {source.name} -> {destination.name}")
            return True
        try:
            source.rename(destination)
            _log(f"[*] {description}: {source.name} -> {destination.name}")
            return True
        except Exception as exc:  # pragma: no cover - filesystem issues
            _log(f"[!] Error renaming {source.name} -> {destination.name}: {exc}")
            return False

    if best_file.exists() and best_file.resolve() != canonical_target.resolve():
        if canonical_target.exists() and canonical_target != best_file:
            preserve_target = folder / f"{NOT_DUPE_PREFIX}{canonical_target.name}"
            if canonical_target.exists():
                _rename(canonical_target, preserve_target, "preserve existing canonical file")
        _rename(best_file, canonical_target, "promote best artwork to canonical name")
    else:
        canonical_target = best_file

    # Refresh candidates to reflect the current on-disk state so we don't
    # accidentally re-tag the newly promoted canonical file.
    updated_candidates = _collect_artwork_candidates(folder, canonical_stem)

    def _is_canonical(path: Path) -> bool:
        try:
            if canonical_target.exists() and path.resolve() == canonical_target.resolve():
                return True
        except FileNotFoundError:
            pass
        return path.name == canonical_target.name

    for other in updated_candidates:
        if _is_canonical(other):
            continue
        if other.name.startswith(NOT_DUPE_PREFIX):
            continue
        if not other.exists():
            continue
        new_name = folder / f"{NOT_DUPE_PREFIX}{other.name}"
        _rename(other, new_name, "tag non-canonical artwork")

    return str(canonical_target), messages


def _directory_contains_audio_files(folder_path: str) -> bool:
    """Return True when folder contains at least one supported audio file."""
    try:
        for entry in os.listdir(folder_path):
            candidate = os.path.join(folder_path, entry)
            if not os.path.isfile(candidate):
                continue
            _, ext = os.path.splitext(entry)
            if ext.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                return True
    except OSError:
        return False
    return False


def _folder_meets_naming_rules(folder_path: str) -> bool:
    """Return True when folder name (or its parent when disc-like) parses as Artist - Album."""
    normalized = (folder_path or '').rstrip('/\\')
    folder_name = os.path.basename(normalized) or ''
    artist, album = parse_folder_name(folder_name)
    if artist and album:
        return True

    parent_path = os.path.dirname(normalized)
    parent_name = os.path.basename(parent_path) or ''
    if parent_name and _looks_like_disc_folder(folder_name):
        parent_artist, parent_album = parse_folder_name(parent_name)
        return bool(parent_artist and parent_album)
    return False


def _collect_processable_folders(root_path: str) -> list[tuple[str, str]]:
    """Walk the directory tree and collect folders that satisfy naming rules."""
    normalized_root = os.path.abspath(root_path)
    candidates: list[tuple[str, str]] = []

    for current_root, dirnames, _ in os.walk(normalized_root):
        dirnames[:] = sorted(dirnames, key=str.casefold)
        if _folder_meets_naming_rules(current_root):
            rel_path = os.path.relpath(current_root, normalized_root)
            if rel_path == ".":
                folder_label = os.path.basename(normalized_root) or normalized_root
            else:
                folder_label = rel_path
            candidates.append((folder_label, current_root))

    if not candidates and _directory_contains_audio_files(normalized_root):
        folder_label = os.path.basename(normalized_root) or normalized_root
        candidates.append((folder_label, normalized_root))

    candidates.sort(key=lambda item: item[1].casefold())
    return candidates


STRICT_DISC_KEYWORDS = (
    "cd",
    "disc",
    "disk",
    "dvd",
    "bluray",
    "blu-ray",
    "blueray",
    "bd",
    "br",
    "sacd",
    "lp",
    "vinyl",
    "uhq",
    "uhcd"
)

OPTIONAL_SUFFIX_DISC_KEYWORDS = (
    "boxset",
    "box",
    "set"
)


def _looks_like_disc_folder(name: str) -> bool:
    """Return True when folder name resembles a disc/CD/DVD subfolder."""
    if not name:
        return False

    normalized = name.strip().lower().replace('–', '-')
    compact = re.sub(r'[\s._-]+', '', normalized)

    for keyword in STRICT_DISC_KEYWORDS:
        compact_keyword = keyword.replace('-', '')
        if compact.startswith(compact_keyword):
            return True

    for keyword in OPTIONAL_SUFFIX_DISC_KEYWORDS:
        compact_keyword = keyword.replace('-', '')
        if compact.startswith(compact_keyword):
            remainder = compact[len(compact_keyword):]
            if remainder and (remainder[0].isdigit() or remainder[0] in "ivxlcdmab"):
                return True

    return False


def derive_artist_album_from_path(folder_path: str):
    """Derive artist/album metadata from folder or its parent if needed."""
    normalized_path = (folder_path or '').rstrip('/\\')
    folder_name = os.path.basename(normalized_path) or ''
    artist, album = parse_folder_name(folder_name)
    if artist and album:
        return artist, album, folder_name, False

    parent_path = os.path.dirname(normalized_path)
    parent_name = os.path.basename(parent_path) or ''

    if folder_name and parent_name and _looks_like_disc_folder(folder_name):
        parent_artist, parent_album = parse_folder_name(parent_name)
        if parent_artist and parent_album:
            return parent_artist, parent_album, parent_name, True

    return artist, album, folder_name, False


def _dedupe_preserve_order(values):
    """Return a list with duplicates removed while preserving order."""
    seen = set()
    result = []
    for value in values:
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _flatten_tag_values(raw_value):
    """Return a flat list of string values from Mutagen tag containers."""
    if raw_value is None:
        return []

    if isinstance(raw_value, (list, tuple, set)):
        flattened = []
        for item in raw_value:
            flattened.extend(_flatten_tag_values(item))
        return flattened

    if isinstance(raw_value, Iterable) and not isinstance(raw_value, (str, bytes)):
        flattened = []
        for item in raw_value:
            flattened.extend(_flatten_tag_values(item))
        return flattened

    if hasattr(raw_value, "text"):
        return _flatten_tag_values(raw_value.text)

    text = str(raw_value).strip()
    if not text:
        return []

    separators = []
    if '\\' in text:
        separators.append('\\')
    if '\x00' in text:
        separators.append('\x00')

    for sep in separators:
        parts = [segment.strip() for segment in text.split(sep) if segment.strip()]
        if len(parts) > 1:
            return parts

    return [text]


def _find_first_audio_file(folder_path: str) -> str:
    """Return the first audio file (sorted) with an extension Mutagen can parse."""
    try:
        entries = sorted(os.listdir(folder_path))
    except Exception:
        return None

    for entry in entries:
        candidate = os.path.join(folder_path, entry)
        if not os.path.isfile(candidate):
            continue
        _, ext = os.path.splitext(entry)
        if ext.lower() in SUPPORTED_AUDIO_EXTENSIONS:
            return candidate
    return None


def _extract_tag_candidates(audio_path: str, verbose: bool = False):
    """Return unique (artist, album) combos from the first audio file's tags."""
    if MutagenFile is None:
        return []

    try:
        audio = MutagenFile(audio_path, easy=True)
    except Exception as exc:
        if verbose:
            print(f"  TAG FALLBACK: Unable to read tags from '{os.path.basename(audio_path)}' ({exc})")
        return []

    if not audio:
        if verbose:
            print(f"  TAG FALLBACK: '{os.path.basename(audio_path)}' is not a supported audio file")
        return []

    album_values = _dedupe_preserve_order(_flatten_tag_values(audio.get("album")))
    if not album_values:
        if verbose:
            print("  TAG FALLBACK: No 'album' tag present; skipping tag-based retry")
        return []

    artist_values = _dedupe_preserve_order(
        _flatten_tag_values(audio.get("albumartist")) +
        _flatten_tag_values(audio.get("artist"))
    )
    if not artist_values:
        if verbose:
            print("  TAG FALLBACK: No 'albumartist' or 'artist' tags present; skipping")
        return []

    combos = []
    seen = set()
    for artist in artist_values:
        artist_clean = re.sub(r'\s+', ' ', artist.strip())
        if not artist_clean:
            continue
        for album in album_values:
            album_clean = _normalize_album_text(album)
            if not album_clean:
                continue
            key = (artist_clean.lower(), album_clean.lower())
            if key in seen:
                continue
            seen.add(key)
            combos.append((artist_clean, album_clean))
    return combos


def _derive_metadata_from_tags(folder_path: str, verbose: bool = False) -> tuple[str | None, str | None, str | None]:
    """Derive artist/album metadata from the first audio file's tags."""
    if MutagenFile is None:
        if verbose:
            print("  TAG METADATA: Mutagen not installed; skipping metadata extraction")
        return None, None, None

    audio_path = _find_first_audio_file(folder_path)
    if not audio_path:
        if verbose:
            print("  TAG METADATA: No supported audio files found for metadata extraction")
        return None, None, None

    if verbose:
        print(f"  TAG METADATA: Deriving metadata from '{os.path.basename(audio_path)}'")

    candidates = _extract_tag_candidates(audio_path, verbose=verbose)
    if not candidates:
        return None, None, None

    artist_candidate, album_candidate = candidates[0]
    artist_clean = re.sub(r'\s+', ' ', (artist_candidate or '').strip())
    album_clean = _normalize_album_text(album_candidate or '')
    if not artist_clean or not album_clean:
        return None, None, None

    source_label = f"audio tags from '{os.path.basename(audio_path)}'"
    return artist_clean, album_clean, source_label


def attempt_tag_based_fallback(folder_path: str, downloader: AppleMusicArtworkDownloader,
                               output_path: str, verbose: bool = False):
    """Try to retrieve artwork using tags from the first audio file in folder."""
    if MutagenFile is None:
        if verbose:
            print("  TAG FALLBACK: Mutagen not installed; skipping tag-based retry")
        return False, None, None, False

    audio_path = _find_first_audio_file(folder_path)
    if not audio_path:
        if verbose:
            print("  TAG FALLBACK: No supported audio files found for tag retry")
        return False, None, None, False

    if verbose:
        print(f"  TAG FALLBACK: Inspecting tags from '{os.path.basename(audio_path)}'")

    candidates = _extract_tag_candidates(audio_path, verbose=verbose)
    if not candidates:
        return False, None, None, True

    for artist_candidate, album_candidate in candidates:
        if verbose:
            print(f"  TAG FALLBACK: Trying Artist='{artist_candidate}', Album='{album_candidate}'")
        if downloader.save_artwork(
            artist=artist_candidate,
            album=album_candidate,
            filename=output_path
        ):
            return True, artist_candidate, album_candidate, True

    if verbose:
        print("  TAG FALLBACK: All tag-derived combinations failed")
    return False, None, None, True


class ProcessingLogger:
    """Track successful and failed processing attempts."""

    LOG_FILENAME = "getart.log"
    FAILED_LOG_FILENAME = "getart-failed-lookups.log"
    FALLBACK_LOG_FILENAME = "getart-fallback-lookups.log"

    def __init__(self, log_dir: str):
        """Initialize logger with directory path."""
        self.log_dir = os.path.abspath(log_dir)
        self.log_file = os.path.join(self.log_dir, self.LOG_FILENAME)
        self.failed_log_file = os.path.join(self.log_dir, self.FAILED_LOG_FILENAME)
        self.fallback_log_file = os.path.join(self.log_dir, self.FALLBACK_LOG_FILENAME)
        self.successful_folders = self._load_log(self.log_file)
        self.failed_folders = self._load_log(self.failed_log_file)
        self.fallback_folders = self._load_log(self.fallback_log_file)

    def _load_log(self, file_path: str) -> set:
        """Load folder identifiers from the specified log file."""
        entries = set()
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            parts = line.split('|')
                            if parts:
                                folder_path = parts[0].strip()
                                if folder_path:
                                    entries.add(folder_path)
            except Exception as e:
                print(f"Warning: Could not read log file {file_path}: {e}")
        return entries

    def _ensure_log_header(self, file_path: str, header_lines: list):
        """Create log file with descriptive header if missing."""
        if os.path.exists(file_path):
            return
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                for line in header_lines:
                    f.write(line + "\n")
        except Exception as e:
            print(f"Warning: Could not initialize log file {file_path}: {e}")

    def _replace_log_entry(self, file_path: str, folder_path: str, new_entry: str) -> bool:
        """Replace an existing log entry for folder_path with new_entry."""
        try:
            if not os.path.exists(file_path):
                return False

            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            prefix = f"{folder_path} |"
            entry_line = new_entry + "\n"
            updated = False

            for idx, line in enumerate(lines):
                if line.startswith(prefix):
                    lines[idx] = entry_line
                    updated = True
                    break

            if not updated:
                lines.append(entry_line)

            with open(file_path, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            return True
        except Exception as e:
            print(f"Warning: Could not update log file {file_path}: {e}")
            return False

    def clear_failure(self, folder_path: str):
        """Remove a folder from the failed lookup log."""
        if folder_path not in self.failed_folders:
            return

        try:
            if os.path.exists(self.failed_log_file):
                with open(self.failed_log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                prefix = f"{folder_path} |"
                with open(self.failed_log_file, 'w', encoding='utf-8') as f:
                    for line in lines:
                        if line.startswith(prefix):
                            continue
                        f.write(line)
        except Exception as e:
            print(f"Warning: Could not prune failed log entry for {folder_path}: {e}")
        finally:
            self.failed_folders.discard(folder_path)

    def is_successful(self, folder_path: str) -> bool:
        """Check if folder was successfully processed before"""
        return folder_path in self.successful_folders

    def is_failed(self, folder_path: str) -> bool:
        """Check if folder previously failed to retrieve artwork."""
        return folder_path in self.failed_folders

    def is_fallback(self, folder_path: str) -> bool:
        """Check if folder previously produced only fallback artwork."""
        return folder_path in self.fallback_folders

    def log_success(self, folder_path: str, artist: str, album: str, output_file: str):
        """Log a successful processing"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{folder_path} | {artist} | {album} | {output_file} | {timestamp}"

        try:
            self._ensure_log_header(
                self.log_file,
                [
                    "# Artwork Downloader Success Log",
                    "# Only folders with successfully downloaded artwork are logged",
                    "# Format: Full Folder Path | Artist | Album | Output File | Timestamp",
                    "# " + "=" * 80
                ]
            )

            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(log_entry + "\n")

            # Update in-memory cache
            self.successful_folders.add(folder_path)
            self.clear_fallback(folder_path)

        except Exception as e:
            print(f"Warning: Could not write to log file: {e}")

    def log_failure(self, folder_path: str, artist: str, album: str, reason: str):
        """Log a failed lookup attempt."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{folder_path} | {artist} | {album} | {reason} | {timestamp}"

        try:
            self._ensure_log_header(
                self.failed_log_file,
                [
                    "# Artwork Downloader Failed Lookups Log",
                    "# Entries recorded here could not be matched to Apple Music",
                    "# Use --retry to reprocess them; entries stay skipped otherwise",
                    "# Format: Folder Identifier | Artist | Album | Reason | Timestamp",
                    "# " + "=" * 80
                ]
            )

            entry_written = False

            if folder_path in self.failed_folders:
                entry_written = self._replace_log_entry(self.failed_log_file, folder_path, log_entry)

            if not entry_written:
                with open(self.failed_log_file, 'a', encoding='utf-8') as f:
                    f.write(log_entry + "\n")
                entry_written = True

            if entry_written:
                self.failed_folders.add(folder_path)
        except Exception as e:
            print(f"Warning: Could not write to failed log file: {e}")

    def clear_fallback(self, folder_path: str):
        """Remove a folder from the fallback log."""
        if folder_path not in self.fallback_folders:
            return

        try:
            if os.path.exists(self.fallback_log_file):
                with open(self.fallback_log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                prefix = f"{folder_path} |"
                with open(self.fallback_log_file, 'w', encoding='utf-8') as f:
                    for line in lines:
                        if line.startswith(prefix):
                            continue
                        f.write(line)
        except Exception as e:
            print(f"Warning: Could not prune fallback log entry for {folder_path}: {e}")
        finally:
            self.fallback_folders.discard(folder_path)

    def log_fallback(self, folder_path: str, artist: str, album: str, output_file: str, reason: str):
        """Log an entry that only produced fallback artwork."""
        if not folder_path:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{folder_path} | {artist} | {album} | {output_file} | {reason} | {timestamp}"

        try:
            self._ensure_log_header(
                self.fallback_log_file,
                [
                    "# Artwork Downloader Fallback Log",
                    "# Entries recorded here only produced partial Apple matches",
                    "# Use --retry-fallbacks (or --fallback-only) to reprocess them",
                    "# Format: Folder Identifier | Artist | Album | Output File | Reason | Timestamp",
                    "# " + "=" * 80
                ]
            )

            entry_written = False

            if folder_path in self.fallback_folders:
                entry_written = self._replace_log_entry(self.fallback_log_file, folder_path, log_entry)

            if not entry_written:
                with open(self.fallback_log_file, 'a', encoding='utf-8') as f:
                    f.write(log_entry + "\n")
                entry_written = True

            if entry_written:
                self.fallback_folders.add(folder_path)
        except Exception as e:
            print(f"Warning: Could not write to fallback log file: {e}")


def process_directory(directory: str, verbose: bool = False, throttle: float = 0,
                      ignore_log: bool = False, overwrite: bool = False,
                      retry_failed: bool = False, retry_only: bool = False,
                      retry_fallbacks: bool = False, fallback_only: bool = False,
                      dry_run: bool = False, allow_artist_only_match: bool = False,
                      dedupe_artwork: bool = True,
                      canonical_artwork_stem: str = DEFAULT_CANONICAL_ARTWORK_STEM):
    """
    Process all subfolders in directory and download artwork for each.

    Args:
        directory: Root directory containing artist-album folders
        verbose: Enable verbose output
        throttle: Seconds to wait if rate-limited
        ignore_log: Ignore previous successful processing log
        overwrite: Overwrite existing xfolder.jpg files
        retry_failed: Reprocess folders recorded in the failed lookup log
        allow_artist_only_match: Permit saving artist-only matches when album/title fails
        dedupe_artwork: Normalize cover filenames and delete obvious duplicates after saving new art
        canonical_artwork_stem: Preferred filename stem for definitive artwork (from TOML)

    Returns:
        dict: Statistics about processed folders
    """
    directory = os.path.abspath(directory)
    canonical_artwork_stem = _normalize_canonical_stem(canonical_artwork_stem)

    if not os.path.exists(directory):
        print(f"ERROR: Directory '{directory}' does not exist")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    if not os.path.isdir(directory):
        print(f"ERROR: '{directory}' is not a directory")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    downloader = AppleMusicArtworkDownloader(
        verbose=verbose,
        throttle=throttle,
        allow_artist_only_match=allow_artist_only_match
    )
    logger = ProcessingLogger(directory)

    if retry_only:
        retry_failed = True
    if fallback_only:
        retry_fallbacks = True

    folders_to_process = _collect_processable_folders(directory)
    total = len(folders_to_process)

    if total == 0:
        print(f"Found 0 folders matching naming rules under '{directory}'")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    success = 0
    fallback_successes = 0
    fallback_successes = 0
    failed = 0
    skipped = 0

    if total == 1 and folders_to_process[0][1] == directory:
        print(f"Found 1 folder to process (root '{directory}')")
    else:
        print(f"Found {total} folder(s) matching naming rules under '{directory}'")
    if os.path.exists(logger.log_file):
        print(f"Success log found: {logger.log_file}")
        print(f"Previously successful: {len(logger.successful_folders)} folder(s)")
    print()

    rate_limit_error = None

    def log_action(idx: int, folder_name: str, message: str | None = None) -> None:
        rate_notice = ""
        if downloader.rate_limit_escalated:
            rate_notice = f" {_format_rate_limit_tag(downloader.current_delay)}"
        prefix = f"[{idx}/{total}]" + rate_notice
        prefix = f"{prefix} {folder_name}".strip()
        if message:
            print(f"{prefix} -> {message}")
        else:
            print(prefix)

    for i, (folder, folder_path) in enumerate(folders_to_process, 1):
        is_failed_entry = logger.is_failed(folder_path)
        is_fallback_entry = logger.is_fallback(folder_path)

        if fallback_only and not is_fallback_entry:
            skipped += 1
            continue

        if not fallback_only and not retry_fallbacks and is_fallback_entry:
            if verbose:
                log_action(
                    i,
                    folder,
                    f"SKIPPED: partial-match entry (see {logger.fallback_log_file}); use --retry-fallbacks to reprocess"
                )
            skipped += 1
            continue

        if retry_only and not is_failed_entry:
            skipped += 1
            continue

        if not retry_only and not ignore_log and logger.is_successful(folder_path):
            if verbose:
                log_action(i, folder, "SKIPPED: previously successful; see log")
            skipped += 1
            continue

        if not retry_failed and is_failed_entry:
            if verbose:
                log_action(
                    i,
                    folder,
                    f"SKIPPED: previously failed lookup (see {logger.failed_log_file}); use --retry to reprocess"
                )
            skipped += 1
            continue

        artist, album, metadata_source, used_parent_metadata = derive_artist_album_from_path(folder_path)
        metadata_from_tags = False

        if (not artist or not album) and os.path.isdir(folder_path):
            tag_artist, tag_album, tag_source = _derive_metadata_from_tags(folder_path, verbose=verbose)
            if tag_artist and tag_album:
                artist = tag_artist
                album = tag_album
                metadata_source = tag_source
                used_parent_metadata = False
                metadata_from_tags = True

        output_path = os.path.join(folder_path, "xfolder.jpg")
        if os.path.exists(output_path) and not overwrite:
            if verbose:
                log_action(i, folder, "SKIPPED: xfolder.jpg already exists; use --overwrite to replace if desired")
            if artist and album:
                logger.log_success(folder_path, artist, album, output_path)
                logger.clear_failure(folder_path)
            skipped += 1
            continue

        if not artist or not album:
            if used_parent_metadata:
                log_action(i, folder, "SKIPPED: unable to derive artist/album even after checking parent folder")
            else:
                log_action(i, folder, "SKIPPED: invalid folder format; expected 'Artist - Album'")
            failed += 1
            continue

        if verbose:
            if metadata_from_tags and metadata_source:
                log_action(i, folder, f"Parsed via {metadata_source}: {artist} - {album}")
            elif used_parent_metadata and metadata_source:
                log_action(i, folder, f"Parsed via parent '{metadata_source}': {artist} - {album}")
            else:
                log_action(i, folder, f"Parsed: {artist} - {album}")

        if not os.path.exists(folder_path):
            log_action(i, folder, f"SKIPPED: folder '{folder_path}' does not exist")
            failed += 1
            continue

        if dry_run:
            info_msg = f"\n       {artist} - {album}\n"
            if metadata_from_tags and metadata_source:
                info_msg += f" (derived from {metadata_source})"
            elif used_parent_metadata and metadata_source:
                info_msg += f" (derived from '{metadata_source}')"
            log_action(i, folder, info_msg)
            skipped += 1
            continue

        try:
            lookup_success = downloader.save_artwork(
                artist=artist,
                album=album,
                filename=output_path
            )

            if lookup_success:
                success += 1
                final_path, used_fallback_name = _finalize_output_path(
                    output_path, downloader.last_match_type
                )
                dedupe_messages: list[str] = []
                if dedupe_artwork:
                    canonical_path, dedupe_messages = dedupe_artwork_variants(
                        folder_path,
                        similarity=DEFAULT_DEDUPE_SIMILARITY,
                        canonical_stem=canonical_artwork_stem,
                        verbose=verbose
                    )
                    if canonical_path:
                        final_path = canonical_path

                if used_fallback_name:
                    fallback_successes += 1
                    match_label = downloader.last_match_type or "partial"
                    log_action(
                        i,
                        folder,
                        f"SUCCESS: saved to {final_path} (partial Apple match logged for retry)")
                    logger.log_fallback(
                        folder_path,
                        artist,
                        album,
                        final_path,
                        f"{match_label} match"
                    )
                    logger.clear_failure(folder_path)
                else:
                    log_action(i, folder, f"SUCCESS: saved to {final_path}")
                    logger.log_success(folder_path, artist, album, final_path)
                    logger.clear_failure(folder_path)
                for message in dedupe_messages:
                    log_action(i, folder, message)
            else:
                fallback_success, fb_artist, fb_album, fallback_attempted = attempt_tag_based_fallback(
                    folder_path, downloader, output_path, verbose=verbose
                )

                if fallback_success:
                    success += 1
                    final_path, used_fallback_name = _finalize_output_path(
                        output_path, downloader.last_match_type
                    )
                    dedupe_messages: list[str] = []
                    if dedupe_artwork:
                        canonical_path, dedupe_messages = dedupe_artwork_variants(
                            folder_path,
                            similarity=DEFAULT_DEDUPE_SIMILARITY,
                            canonical_stem=canonical_artwork_stem,
                            verbose=verbose
                        )
                        if canonical_path:
                            final_path = canonical_path
                    log_action(
                        i,
                        folder,
                        f"SUCCESS: saved to {final_path} via tag fallback ({fb_artist} - {fb_album})"
                    )
                    if not used_fallback_name:
                        logger.log_success(folder_path, fb_artist, fb_album, final_path)
                        logger.clear_failure(folder_path)
                    else:
                        fallback_successes += 1
                        logger.log_fallback(
                            folder_path,
                            fb_artist,
                            fb_album,
                            final_path,
                            "tag fallback partial match"
                        )
                        logger.clear_failure(folder_path)
                        log_action(i, folder, "NOTE: Partial Apple match via tags; logged for targeted retry.")
                    for message in dedupe_messages:
                        log_action(i, folder, message)
                else:
                    failed += 1
                    reason = "Artwork not found"
                    if fallback_attempted:
                        reason += " (tag fallback unavailable or unsuccessful)"
                    log_action(i, folder, f"FAILED: Could not find artwork for {artist} - {album}")
                    logger.log_failure(folder_path, artist, album, reason)
        except RateLimitExceededError as exc:
            print("  STOPPED: Apple Music is still throttling requests. Halting batch early.")
            rate_limit_error = exc
            break

        delay = downloader.current_delay
        if delay > 0:
            time.sleep(delay)

    if rate_limit_error:
        print("Processing interrupted by rate limiting; summary reflects completed folders only.")

    print()
    if fallback_successes:
        definitive = success - fallback_successes
        print(
            f"Summary: {success} successful ({definitive} definitive, {fallback_successes} fallback), "
            f"{failed} failed, {skipped} skipped"
        )
    else:
        print(f"Summary: {success} successful, {failed} failed, {skipped} skipped")
    if os.path.exists(logger.log_file):
        print(f"Success log: {logger.log_file}")

    if rate_limit_error:
        print("Processing stopped early due to continued rate limiting. Please retry later.")
        raise rate_limit_error

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": skipped
    }


def process_directory_file(list_file: str, verbose: bool = False, throttle: float = 0,
                           overwrite: bool = False, ignore_log: bool = False,
                           retry_failed: bool = False, retry_only: bool = False,
                           retry_fallbacks: bool = False, fallback_only: bool = False,
                           dry_run: bool = False, allow_artist_only_match: bool = False,
                           dedupe_artwork: bool = True,
                           canonical_artwork_stem: str = DEFAULT_CANONICAL_ARTWORK_STEM) -> dict:
    """Process directories enumerated inside a text file."""
    list_file = os.path.abspath(list_file)
    canonical_artwork_stem = _normalize_canonical_stem(canonical_artwork_stem)

    if not os.path.exists(list_file):
        print(f"ERROR: List file '{list_file}' does not exist")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    downloader = AppleMusicArtworkDownloader(
        verbose=verbose,
        throttle=throttle,
        allow_artist_only_match=allow_artist_only_match
    )

    raw_lines = []
    with open(list_file, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            raw_lines.append(line)

    if not raw_lines:
        print(f"ERROR: List file '{list_file}' does not contain any directory entries")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    total = len(raw_lines)
    cwd = os.getcwd()
    logger = ProcessingLogger(cwd)
    success = 0
    failed = 0
    skipped = 0
    rate_limit_error = None

    if retry_only:
        retry_failed = True
    if fallback_only:
        retry_fallbacks = True

    print(f"Loaded {total} path(s) from '{list_file}'")
    print()

    entry_infos = []
    for entry in raw_lines:
        dir_path = os.path.abspath(entry)
        folder_exists = os.path.isdir(dir_path)
        artist, album, metadata_source, used_parent_metadata = derive_artist_album_from_path(dir_path)
        info = {
            "entry": entry,
            "dir_path": dir_path,
            "folder_exists": folder_exists,
            "artist": artist,
            "album": album,
            "valid": bool(artist and album),
            "metadata_source": metadata_source,
            "used_parent_metadata": used_parent_metadata,
            "metadata_from_tags": False
        }

        if not info["valid"] and folder_exists:
            tag_artist, tag_album, tag_source = _derive_metadata_from_tags(dir_path, verbose=verbose)
            if tag_artist and tag_album:
                info.update({
                    "artist": tag_artist,
                    "album": tag_album,
                    "valid": True,
                    "metadata_source": tag_source,
                    "used_parent_metadata": False,
                    "metadata_from_tags": True
                })

        if info["valid"]:
            if folder_exists:
                output_path = os.path.join(dir_path, "xfolder.jpg")
                log_key = dir_path
            else:
                filename = sanitize_filename(f"{info['artist']} - {info['album']} xfolder.jpg")
                output_path = os.path.join(cwd, filename)
                log_key = output_path
            info.update({"output_path": output_path, "log_key": log_key})
        else:
            info.update({"output_path": None, "log_key": None})

        entry_infos.append(info)

    work_items = []
    prefiltered_failures = 0
    prefiltered_fallbacks = 0
    for info in entry_infos:
        log_key = info.get("log_key")
        is_failed_entry = logger.is_failed(log_key) if log_key else False
        is_fallback_entry = logger.is_fallback(log_key) if log_key else False

        if retry_only:
            if is_failed_entry:
                pass
            else:
                skipped += 1
                continue

        if fallback_only:
            if is_fallback_entry:
                pass
            else:
                skipped += 1
                continue

        if log_key and not fallback_only and not retry_fallbacks and is_fallback_entry:
            prefiltered_fallbacks += 1
            skipped += 1
            continue

        if log_key and not retry_only and not ignore_log and logger.is_successful(log_key):
            skipped += 1
            continue
        if log_key and not retry_failed and is_failed_entry:
            prefiltered_failures += 1
            skipped += 1
            continue
        work_items.append(info)

    work_total = len(work_items)

    if prefiltered_failures:
        print(
            f"Skipped {prefiltered_failures} entr{'y' if prefiltered_failures == 1 else 'ies'} due to previous failures "
            f"(see {logger.failed_log_file}); rerun with --retry to include them."
        )
    if prefiltered_fallbacks:
        print(
            f"Skipped {prefiltered_fallbacks} partial-match entr{'y' if prefiltered_fallbacks == 1 else 'ies'} "
            f"(see {logger.fallback_log_file}); rerun with --retry-fallbacks to include them."
        )

    for idx, info in enumerate(work_items, 1):
        entry = info["entry"]
        folder_exists = info["folder_exists"]
        folder_path = info["dir_path"]
        artist = info["artist"]
        album = info["album"]
        valid = info["valid"]
        metadata_source = info.get("metadata_source")
        used_parent_metadata = info.get("used_parent_metadata")
        metadata_from_tags = info.get("metadata_from_tags")
        status_label = "Found" if folder_exists else "Missing"
        rate_notice = ""
        if downloader.rate_limit_escalated:
            rate_notice = f" {_format_rate_limit_tag(downloader.current_delay)}"
        prefix = f"[{idx}/{work_total}]" + rate_notice
        print(f"{prefix} [{status_label}] Entry: {entry}")

        if not valid:
            if used_parent_metadata:
                print("  SKIPPED: Unable to derive artist/album even after checking parent folder")
            else:
                print("  SKIPPED: Unable to parse 'Artist - Album' from folder name")
            failed += 1
            continue

        if verbose:
            if metadata_from_tags and metadata_source:
                print(f"  Parsed via {metadata_source}: Artist='{artist}', Album='{album}'")
            elif used_parent_metadata and metadata_source:
                print(f"  Parsed (using parent folder '{metadata_source}'): Artist='{artist}', Album='{album}'")
            else:
                print(f"  Parsed: Artist='{artist}', Album='{album}'")

        output_path = info["output_path"]
        log_key = info["log_key"]

        if os.path.exists(output_path) and not overwrite:
            print(f"  SKIPPED: {output_path} already exists (use --overwrite to force)")
            skipped += 1
            continue

        if dry_run:
            destination = folder_exists and folder_path or cwd
            msg = (
                f"  \n         {artist}' - '{album}"
                f" -> would save to {output_path} (in {destination})"
            )
            if metadata_from_tags and metadata_source:
                msg += f"\n         (derived from {metadata_source})"
            elif used_parent_metadata and metadata_source:
                msg += f"\n         (derived from '{metadata_source}')"
            print(msg)
            skipped += 1
            continue

        try:
            lookup_success = downloader.save_artwork(
                artist=artist,
                album=album,
                filename=output_path
            )

            if lookup_success:
                success += 1
                destination = "directory" if folder_exists else "current working directory"
                final_path, used_fallback_name = _finalize_output_path(
                    output_path, downloader.last_match_type
                )
                dedupe_messages: list[str] = []
                if dedupe_artwork and folder_exists:
                    canonical_path, dedupe_messages = dedupe_artwork_variants(
                        folder_path,
                        similarity=DEFAULT_DEDUPE_SIMILARITY,
                        canonical_stem=canonical_artwork_stem,
                        verbose=verbose
                    )
                    if canonical_path:
                        final_path = canonical_path
                print(f"  SUCCESS: Artwork saved to {final_path} ({destination})")
                if not used_fallback_name:
                    logger.log_success(log_key, artist, album, final_path)
                    if log_key:
                        logger.clear_failure(log_key)
                else:
                    fallback_successes += 1
                    logger.log_fallback(
                        log_key,
                        artist,
                        album,
                        final_path,
                        f"{downloader.last_match_type or 'partial'} match"
                    )
                    if log_key:
                        logger.clear_failure(log_key)
                    print("    NOTE: Partial Apple match; entry logged separately so you can target it later.")
                for message in dedupe_messages:
                    print(f"    {message}")
            else:
                fallback_success = False
                fallback_attempted = False
                fb_artist = None
                fb_album = None

                if folder_exists:
                    fallback_success, fb_artist, fb_album, fallback_attempted = attempt_tag_based_fallback(
                        folder_path, downloader, output_path, verbose=verbose
                    )

                if fallback_success:
                    success += 1
                    destination = "directory" if folder_exists else "current working directory"
                    final_path, used_fallback_name = _finalize_output_path(
                        output_path, downloader.last_match_type
                    )
                    dedupe_messages: list[str] = []
                    if dedupe_artwork and folder_exists:
                        canonical_path, dedupe_messages = dedupe_artwork_variants(
                            folder_path,
                            similarity=DEFAULT_DEDUPE_SIMILARITY,
                            canonical_stem=canonical_artwork_stem,
                            verbose=verbose
                        )
                        if canonical_path:
                            final_path = canonical_path
                    print(
                        f"  SUCCESS: Artwork saved to {final_path} ({destination}) using tag fallback ({fb_artist} - {fb_album})"
                    )
                    if not used_fallback_name:
                        logger.log_success(log_key, fb_artist, fb_album, final_path)
                        if log_key:
                            logger.clear_failure(log_key)
                    else:
                        fallback_successes += 1
                        logger.log_fallback(
                            log_key,
                            fb_artist,
                            fb_album,
                            final_path,
                            "tag fallback partial match"
                        )
                        if log_key:
                            logger.clear_failure(log_key)
                        print("    NOTE: Partial Apple match via tags; logged so you can retry when Apple improves.")
                    for message in dedupe_messages:
                        print(f"    {message}")
                else:
                    failed += 1
                    reason = "Artwork not found"
                    if fallback_attempted:
                        reason += " (tag fallback unavailable or unsuccessful)"
                    print(f"  FAILED: Could not find artwork for {artist} - {album}")
                    if log_key:
                        logger.log_failure(log_key, artist, album, reason)
        except RateLimitExceededError as exc:
            print("  STOPPED: Apple Music is still throttling requests. Halting file processing early.")
            rate_limit_error = exc
            break

        delay = downloader.current_delay
        if delay > 0:
            time.sleep(delay)

    if rate_limit_error:
        print("Processing interrupted by rate limiting; summary reflects completed entries only.")

    print()
    if fallback_successes:
        definitive = success - fallback_successes
        print(
            f"Summary: {success} successful ({definitive} definitive, {fallback_successes} fallback), "
            f"{failed} failed, {skipped} skipped"
        )
    else:
        print(f"Summary: {success} successful, {failed} failed, {skipped} skipped")

    if rate_limit_error:
        print("Processing stopped early due to continued rate limiting. Please retry later.")
        raise rate_limit_error

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": skipped
    }


def parse_arguments(config_defaults: dict[str, Any] | None = None):
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Download high-quality artwork from Apple Music",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes of operation:
  1. Single artwork mode (default):
     Requires --artist and either --album or --title

  2. Batch directory mode:
     Use --dir to process multiple folders at once
     Folder names should be in format "Artist - Album"
     Any text in square brackets [] will be stripped from album name

  3. File-driven mode:
      Use --dirs2process FILE to read absolute folder paths from a text file
      Each line should contain one folder path (comments/blank lines ignored)
      Missing folders are saved into the current working directory as
      "Artist - Album xfolder.jpg"

Dependencies:
    Tag-based retries (used to salvage folders whose names do not parse cleanly)
    require the optional "mutagen" package. When Mutagen is not installed those
    fallbacks are skipped entirely.

Success Log:
  In batch mode, a success log file (artwork_downloader.log) is created in the --dir directory.
  Only folders with successfully downloaded artwork are logged.
  Previously logged folders are skipped unless --ignore-log is specified.

Examples:
  Single artwork mode:
    %(prog)s --artist "Taylor Swift" --album "1989"
    %(prog)s --artist "The Beatles" --title "Yesterday" --output cover.jpg

  Batch directory mode:
    %(prog)s --dir "/path/to/music/folders"
    %(prog)s --dir "/path/to/music" --verbose --throttle 0.5
    %(prog)s --dir "/path/to/music" --ignore-log  # Retry previously successful
    %(prog)s --dir "/path/to/music" --overwrite   # Overwrite existing files
    %(prog)s --dir "/path/to/music" --ignore-log --overwrite  # Force reprocess all
        """
    )

    # Create mutually exclusive group for modes
    mode_group = parser.add_mutually_exclusive_group(required=True)

    # Single artwork mode arguments
    mode_group.add_argument("--artist", "-a", help="Artist name (for single artwork mode)")
    mode_group.add_argument("--dir", "-d", help="Directory path (for batch mode)")
    mode_group.add_argument("--dirs2process", "-f", help="Text file listing directory paths to process")

    # Single artwork mode optional arguments
    parser.add_argument("--album", "-l", help="Album name (for single artwork mode)")
    parser.add_argument("--title", "-t", help="Track title (for single artwork mode)")
    parser.add_argument("--output", "-o", default="xfolder.jpg", help="Output filename for single artwork mode (default: xfolder.jpg)")

    # Batch mode optional arguments
    parser.add_argument("--ignore-log", "-i", action="store_true", help="Ignore success log and retry all folders")
    parser.add_argument("--overwrite", "-w", action="store_true", help="Overwrite existing xfolder.jpg files")
    parser.add_argument(
        "--retry",
        "-r",
        action="store_true",
        help="Retry entries recorded in getart-failed-lookups.log"
    )
    parser.add_argument(
        "--retry-only",
        action="store_true",
        help="Process only the entries listed in getart-failed-lookups.log"
    )
    parser.add_argument(
        "--retry-fallbacks",
        action="store_true",
        help="Include entries recorded in getart-fallback-lookups.log"
    )
    parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="Process only the entries listed in getart-fallback-lookups.log"
    )

    # Common arguments
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument("--throttle", type=float, default=1, help="Seconds to wait between requests (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="Print derived lookup info without downloading artwork")
    parser.add_argument(
        "--allow-artist-only-match",
        dest="allow_artist_only_match",
        action="store_true",
        help="Allow saving the first artist-only match when album/title matching fails"
    )
    parser.add_argument(
        "--no-artist-only-match",
        dest="allow_artist_only_match",
        action="store_false",
        help="Disable artist-only fallback matching (default)"
    )
    parser.add_argument(
        "--no-dedupe",
        dest="dedupe_artwork",
        action="store_false",
        help="Skip post-download artwork deduplication and canonical renaming"
    )
    parser.add_argument(
        "--dedupe",
        dest="dedupe_artwork",
        action="store_true",
        help="Force-enable artwork deduplication even if disabled in config"
    )

    defaults = dict(config_defaults or {})
    defaults.setdefault("dedupe_artwork", True)
    defaults.setdefault("canonical_artwork_stem", DEFAULT_CANONICAL_ARTWORK_STEM)
    parser.set_defaults(**defaults)

    # If no arguments provided, show extended help
    if len(sys.argv) == 1:
        parser.print_help()
        print("\nERROR: No arguments provided. Choose either --artist or --dir mode.")
        sys.exit(1)

    args = parser.parse_args()

    if getattr(args, "retry_only", False):
        args.retry = True

    if getattr(args, "fallback_only", False):
        if getattr(args, "retry_only", False):
            parser.error("--fallback-only cannot be combined with --retry-only")
        args.retry_fallbacks = True

    return args


def validate_single_mode_arguments(args):
    """Validate arguments for single artwork mode"""
    if not args.album and not args.title:
        print("ERROR: In single artwork mode, you must specify either --album or --title along with --artist.")
        print("\nExamples:")
        print("  getart.py --artist \"Taylor Swift\" --album \"1989\"")
        print("  getart.py --artist \"The Beatles\" --title \"Yesterday\"")
        sys.exit(1)

    if args.album and args.title:
        print("WARNING: Both --album and --title specified. Using album (takes precedence).")


def main():
    """Main entry point for command-line usage"""
    print(f"\n{SCRIPT_NAME} {SCRIPT_VERSION}\n")

    config_defaults = _load_preferences()
    args = parse_arguments(config_defaults)

    try:
        if args.dir:
            # Batch directory mode
            process_directory(
                directory=args.dir,
                verbose=args.verbose,
                throttle=args.throttle,
                ignore_log=args.ignore_log,
                overwrite=args.overwrite,
                retry_failed=args.retry,
                retry_only=args.retry_only,
                retry_fallbacks=args.retry_fallbacks,
                fallback_only=args.fallback_only,
                dry_run=args.dry_run,
                allow_artist_only_match=args.allow_artist_only_match,
                dedupe_artwork=args.dedupe_artwork,
                canonical_artwork_stem=args.canonical_artwork_stem
            )
        elif getattr(args, "dirs2process", None):
            # File-driven mode
            process_directory_file(
                list_file=args.dirs2process,
                verbose=args.verbose,
                throttle=args.throttle,
                overwrite=args.overwrite,
                ignore_log=args.ignore_log,
                retry_failed=args.retry,
                retry_only=args.retry_only,
                retry_fallbacks=args.retry_fallbacks,
                fallback_only=args.fallback_only,
                dry_run=args.dry_run,
                allow_artist_only_match=args.allow_artist_only_match,
                dedupe_artwork=args.dedupe_artwork,
                canonical_artwork_stem=args.canonical_artwork_stem
            )
        else:
            # Single artwork mode
            validate_single_mode_arguments(args)

            if args.dry_run:
                print(
                    f"DRY RUN: Artist='{args.artist}', "
                    f"Album='{args.album}', Title='{args.title if not args.album else None}'"
                    f" -> would save to {args.output}"
                )
                sys.exit(0)

            downloader = AppleMusicArtworkDownloader(
                verbose=args.verbose,
                throttle=args.throttle,
                allow_artist_only_match=args.allow_artist_only_match
            )

            success = downloader.save_artwork(
                artist=args.artist,
                album=args.album,
                title=args.title if not args.album else None,  # Don't pass title if album is specified
                filename=args.output
            )

            sys.exit(0 if success else 1)
    except RateLimitExceededError as exc:
        print(f"ERROR: {exc}")
        sys.exit(2)


# Helper function for easy import
def get_apple_music_artwork(artist: str, album: str = None, title: str = None,
                           verbose: bool = False, throttle: float = 1,
                           allow_artist_only_match: bool = False) -> bytes:
    """
    Convenience function for importing.

    Returns raw image bytes or None if not found.

    Args:
        artist: Artist name (required)
        album: Album name (required unless title is specified)
        title: Track title (required unless album is specified)
        verbose: Enable verbose output
        throttle: Seconds to wait if rate-limited
        allow_artist_only_match: Permit saving artist-only matches when album/title fails
    """
    if not album and not title:
        raise ValueError("You must specify either album or title")

    downloader = AppleMusicArtworkDownloader(
        verbose=verbose,
        throttle=throttle,
        allow_artist_only_match=allow_artist_only_match
    )
    return downloader.get_artwork(artist, album, title)


if __name__ == "__main__":
    main()
