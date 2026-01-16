#!/usr/bin/env python3
"""
Apple Music Artwork Downloader
Retrieves high-quality artwork from Apple Music using artist/album information.
"""

import json
import time
import os
import re
from urllib.request import Request, urlopen, HTTPError
from urllib.parse import quote, urlparse
import argparse
import sys
from datetime import datetime


class RateLimitExceededError(RuntimeError):
    """Raised when Apple Music continues throttling after enforced backoff."""


class AppleMusicArtworkDownloader:
    """Self-contained Apple Music artwork downloader"""

    def __init__(self, verbose: bool = False, throttle: float = 1):
        """
        Initialize the downloader.

        Args:
            verbose: Enable detailed logging
            throttle: Seconds to wait if rate-limited
        """
        self.verbose = verbose
        self.throttle = throttle

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

        if self.verbose:
            print(f"Initialized with size={self.ART_SIZE}, quality={self.ART_QUALITY}")

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
        """Query iTunes Search API for music metadata"""
        token = album or title or ""
        entity = "album" if album else "musicTrack"
        query_term = f"{artist} {token}".strip()

        # Build search URL
        url = f"https://itunes.apple.com/search?term={quote(query_term)}&media=music&entity={entity}"

        if self.verbose:
            print(f"Searching iTunes API: {artist} - {token}")

        try:
            raw_json = self._urlopen_safe(url).decode("utf8")
            return json.loads(raw_json)
        except json.JSONDecodeError as e:
            if self.verbose:
                print(f"Failed to parse JSON response: {e}")
            return {}
        except RateLimitExceededError:
            raise
        except Exception as e:
            if self.verbose:
                print(f"Error querying iTunes: {e}")
            return {}

    def _find_best_artwork_url(self, results: list, artist: str, album: str = None,
                               title: str = None) -> str:
        """Find the best matching artwork URL from search results"""
        if not results:
            return None

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

        def format_art_url(raw_url: str) -> str:
            if not raw_url:
                return None
            return raw_url.replace('100x100bb', self.file_suffix)

        def artist_matches(result_artist_lower: str) -> bool:
            return is_overlap(artist_lower, result_artist_lower)

        if album_lower:
            partial_album_match = None
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
                    return art_url

                if not partial_album_match and is_overlap(album_lower, result_album_lower):
                    partial_album_match = art_url

                if not first_artist_match:
                    first_artist_match = art_url

            return partial_album_match or first_artist_match

        # No album specified; match on artist and optionally title
        partial_title_match = None
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
                    return art_url

                if not partial_title_match and is_overlap(title_lower, result_title_lower):
                    partial_title_match = art_url

            if not first_artist_match:
                first_artist_match = art_url

        return partial_title_match or first_artist_match

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
        art_url = self._find_best_artwork_url(results, artist, album, title)

        if not art_url:
            if self.verbose:
                print("No suitable artwork URL found")
            return None

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
    artist = parts[0].strip()
    album = parts[1].strip()

    # Remove anything in square brackets (including the brackets)
    album = re.sub(r'\s*\[.*?\]\s*', ' ', album).strip()

    # Also remove anything in parentheses if you want
    album = re.sub(r'\s*\(.*?\)\s*', ' ', album).strip()

    # Clean up multiple spaces
    album = re.sub(r'\s+', ' ', album)

    return artist, album


def sanitize_filename(name: str) -> str:
    """Make filename safe for most filesystems while preserving readability."""
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    return sanitized or "xfolder.jpg"


class ProcessingLogger:
    """Log successfully processed folders"""

    LOG_FILENAME = "getart.log"

    def __init__(self, log_dir: str):
        """
        Initialize logger with directory path.

        Args:
            log_dir: Directory where log file will be stored
        """
        self.log_dir = os.path.abspath(log_dir)
        self.log_file = os.path.join(self.log_dir, self.LOG_FILENAME)
        self.successful_folders = self._load_log()

    def _load_log(self) -> set:
        """Load successfully processed folders from log file"""
        successful = set()
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            # Extract full path from log entry
                            parts = line.split('|')
                            if len(parts) >= 2:
                                folder_path = parts[0].strip()
                                successful.add(folder_path)
            except Exception as e:
                print(f"Warning: Could not read log file: {e}")
        return successful

    def is_successful(self, folder_path: str) -> bool:
        """Check if folder was successfully processed before"""
        return folder_path in self.successful_folders

    def log_success(self, folder_path: str, artist: str, album: str, output_file: str):
        """Log a successful processing"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{folder_path} | {artist} | {album} | {output_file} | {timestamp}"

        try:
            # Create log file if it doesn't exist and write header
            if not os.path.exists(self.log_file):
                with open(self.log_file, 'w', encoding='utf-8') as f:
                    f.write("# Artwork Downloader Success Log\n")
                    f.write("# Only folders with successfully downloaded artwork are logged\n")
                    f.write("# Format: Full Folder Path | Artist | Album | Output File | Timestamp\n")
                    f.write("# " + "=" * 80 + "\n")

            # Append the new entry
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(log_entry + "\n")

            # Update in-memory cache
            self.successful_folders.add(folder_path)

        except Exception as e:
            print(f"Warning: Could not write to log file: {e}")


def process_directory(directory: str, verbose: bool = False, throttle: float = 0,
                      ignore_log: bool = False, overwrite: bool = False):
    """
    Process all subfolders in directory and download artwork for each.

    Args:
        directory: Root directory containing artist-album folders
        verbose: Enable verbose output
        throttle: Seconds to wait if rate-limited
        ignore_log: Ignore previous successful processing log
        overwrite: Overwrite existing xfolder.jpg files

    Returns:
        dict: Statistics about processed folders
    """
    directory = os.path.abspath(directory)

    if not os.path.exists(directory):
        print(f"ERROR: Directory '{directory}' does not exist")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    if not os.path.isdir(directory):
        print(f"ERROR: '{directory}' is not a directory")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    downloader = AppleMusicArtworkDownloader(verbose=verbose, throttle=throttle)
    logger = ProcessingLogger(directory)

    # Get all subdirectories
    subfolders = []
    for item in os.listdir(directory):
        item_path = os.path.join(directory, item)
        if os.path.isdir(item_path):
            subfolders.append(item)

    total = len(subfolders)
    success = 0
    failed = 0
    skipped = 0

    print(f"Found {total} subfolder(s) in '{directory}'")
    if os.path.exists(logger.log_file):
        print(f"Success log found: {logger.log_file}")
        print(f"Previously successful: {len(logger.successful_folders)} folder(s)")
    print("-" * 60)

    rate_limit_error = None

    for i, folder in enumerate(subfolders, 1):
        folder_path = os.path.join(directory, folder)
        print(f"[{i}/{total}] Processing: {folder}")

        # 1. Check if folder is in success log
        if not ignore_log and logger.is_successful(folder_path):
            print(f"  SKIPPED: Previously successfully processed (see log)")
            skipped += 1
            continue

        # 2. Check if xfolder.jpg exists
        output_path = os.path.join(folder_path, "xfolder.jpg")
        if os.path.exists(output_path) and not overwrite:
            print(f"  SKIPPED: xfolder.jpg already exists (use --overwrite to force)")
            # Log it as successful since xfolder.jpg exists
            artist, album = parse_folder_name(folder)
            if artist and album:
                logger.log_success(folder_path, artist, album, output_path)
            skipped += 1
            continue

        # 3. Parse artist and album from folder name
        artist, album = parse_folder_name(folder)

        if not artist or not album:
            print(f"  SKIPPED: Invalid folder format. Expected 'Artist - Album'")
            failed += 1  # Count as failed since we can't even try
            continue

        if verbose:
            print(f"  Parsed: Artist='{artist}', Album='{album}'")

        # Validate that the folder exists before writing
        if not os.path.exists(folder_path):
            print(f"  SKIPPED: Folder '{folder_path}' does not exist")
            failed += 1
            continue

        # Download and save artwork
        try:
            if downloader.save_artwork(
                artist=artist,
                album=album,
                filename=output_path
            ):
                success += 1
                print(f"  SUCCESS: Artwork saved to {output_path}")
                # Only log successful completions
                logger.log_success(folder_path, artist, album, output_path)
            else:
                failed += 1
                print(f"  FAILED: Could not find artwork for {artist} - {album}")
                # Do NOT log failures - they can be retried next run
        except RateLimitExceededError as exc:
            print("  STOPPED: Apple Music is still throttling requests. Halting batch early.")
            rate_limit_error = exc
            break

        # Add a small delay between requests to be polite
        delay = downloader.current_delay
        if delay > 0:
            time.sleep(delay)

    if rate_limit_error:
        print("Processing interrupted by rate limiting; summary reflects completed folders only.")

    print("-" * 60)
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
                           overwrite: bool = False, ignore_log: bool = False) -> dict:
    """Process directories enumerated inside a text file."""
    list_file = os.path.abspath(list_file)

    if not os.path.exists(list_file):
        print(f"ERROR: List file '{list_file}' does not exist")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    downloader = AppleMusicArtworkDownloader(verbose=verbose, throttle=throttle)

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

    print(f"Loaded {total} path(s) from '{list_file}'")
    print("-" * 60)

    for idx, entry in enumerate(raw_lines, 1):
        dir_path = os.path.abspath(entry)
        folder_exists = os.path.isdir(dir_path)
        folder_name = os.path.basename(dir_path.rstrip('/\\'))
        status_label = "Found" if folder_exists else "Missing"
        print(f"[{idx}/{total}] [{status_label}] Entry: {entry}")

        artist, album = parse_folder_name(folder_name)
        if not artist or not album:
            print("  SKIPPED: Unable to parse 'Artist - Album' from folder name")
            failed += 1
            continue

        if verbose:
            print(f"  Parsed: Artist='{artist}', Album='{album}'")

        if folder_exists:
            output_path = os.path.join(dir_path, "xfolder.jpg")
            log_key = dir_path
        else:
            filename = sanitize_filename(f"{artist} - {album} xfolder.jpg")
            output_path = os.path.join(cwd, filename)
            log_key = output_path

        if not ignore_log and logger.is_successful(log_key):
            print("  SKIPPED: Previously successfully processed (see log)")
            skipped += 1
            continue

        if os.path.exists(output_path) and not overwrite:
            print(f"  SKIPPED: {output_path} already exists (use --overwrite to force)")
            skipped += 1
            continue

        try:
            if downloader.save_artwork(
                artist=artist,
                album=album,
                filename=output_path
            ):
                success += 1
                destination = "directory" if folder_exists else "current working directory"
                print(f"  SUCCESS: Artwork saved to {output_path} ({destination})")
                logger.log_success(log_key, artist, album, output_path)
            else:
                failed += 1
                print(f"  FAILED: Could not find artwork for {artist} - {album}")
        except RateLimitExceededError as exc:
            print("  STOPPED: Apple Music is still throttling requests. Halting file processing early.")
            rate_limit_error = exc
            break

        delay = downloader.current_delay
        if delay > 0:
            time.sleep(delay)

    if rate_limit_error:
        print("Processing interrupted by rate limiting; summary reflects completed entries only.")

    print("-" * 60)
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


def parse_arguments():
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

    # Common arguments
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument("--throttle", type=float, default=1, help="Seconds to wait between requests (default: 1)")

    # If no arguments provided, show extended help
    if len(sys.argv) == 1:
        parser.print_help()
        print("\nERROR: No arguments provided. Choose either --artist or --dir mode.")
        sys.exit(1)

    return parser.parse_args()


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
    args = parse_arguments()

    try:
        if args.dir:
            # Batch directory mode
            process_directory(
                directory=args.dir,
                verbose=args.verbose,
                throttle=args.throttle,
                ignore_log=args.ignore_log,
                overwrite=args.overwrite
            )
        elif getattr(args, "dirs2process", None):
            # File-driven mode
            process_directory_file(
                list_file=args.dirs2process,
                verbose=args.verbose,
                throttle=args.throttle,
                overwrite=args.overwrite,
                ignore_log=args.ignore_log
            )
        else:
            # Single artwork mode
            validate_single_mode_arguments(args)

            downloader = AppleMusicArtworkDownloader(
                verbose=args.verbose,
                throttle=args.throttle
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
                           verbose: bool = False, throttle: float = 1) -> bytes:
    """
    Convenience function for importing.

    Returns raw image bytes or None if not found.

    Args:
        artist: Artist name (required)
        album: Album name (required unless title is specified)
        title: Track title (required unless album is specified)
        verbose: Enable verbose output
        throttle: Seconds to wait if rate-limited
    """
    if not album and not title:
        raise ValueError("You must specify either album or title")

    downloader = AppleMusicArtworkDownloader(verbose=verbose, throttle=throttle)
    return downloader.get_artwork(artist, album, title)


if __name__ == "__main__":
    main()
