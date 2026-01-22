# Apple Music Artwork Downloader

Download the highest-resolution (up to 9999×9999) Apple Music artwork for your library using simple command-line workflows. The script can operate on one album/track, on every folder under a root directory, or on explicit folder paths listed inside a text file. All modes are resumable, polite to Apple’s API, and rely on the Python standard library plus RapidFuzz (for scoring) and Mutagen (for tag-based fallbacks).

It does not embed images in file metadata.

## Workflow Philosophy

`getart` is designed to be a resumable, exception-driven batch job. Every run writes human-readable logs for each outcome so future runs can skip previously finished work and concentrate on the outliers:

- `getart.log` tracks definitive successes and keeps them silent on future runs unless you pass `--ignore-log`.
- `getart-failed-lookups.log` records folders Apple couldn’t match; re-run them selectively with `--retry`/`--retry-only`.
- `getart-fallback-lookups.log` captures partial Apple matches (saved as `xfolder_fallback.jpg`) so you can reprocess them later with `--retry-fallbacks`/`--fallback-only` once better metadata or catalog entries appear.

By default, only brand-new folders are processed. Adding switches like `--overwrite`, `--retry`, `--retry-fallbacks`, or their `*-only` counterparts lets you systematically whittle down failures and fuzzy matches without re-scraping your entire library.

## Features

- Retrieves cover art at 9999×9999 px (quality 100) by default.
- Intelligent matching on artist+album or artist+track names, even when folder names carry extra tags like `[24-96 FLAC]`.
- Batch directory processing with automatic logging so previously successful folders are silently skipped (log messages only appear with `--verbose`) unless you opt in.
- File-driven processing for curated folder lists, saving art either in-place or to the current working directory when folders are missing.
- Separate success, failure, and fallback logs (`getart.log`, `getart-failed-lookups.log`, and `getart-fallback-lookups.log`) keep runs resumable; use `--retry`/`--retry-fallbacks` when you want to reattempt previously failed or partial matches.
- Optional tag-based fallback: if [Mutagen](https://mutagen.readthedocs.io/) is installed, the script inspects the first audio file in a folder and cycles through its `albumartist`/`artist` tags when the folder name lookup fails.
- Fuzzy Apple matches are quarantined: the artwork is saved as `xfolder_fallback.jpg`, not logged as successful, and can be revisited later.
- [RapidFuzz](https://maxbachmann.github.io/RapidFuzz/) scoring ranks partial matches so the closest release wins whenever Apple doesn’t return an exact title hit.
- Disc-aware parsing automatically falls back to the parent folder’s `Artist - Album` name whenever a subfolder looks like `CD1`, `Disc 2`, `Blu-Ray`, or other box-set media splits.
- Built-in rate-limit handling: escalates to 5-second delays when Apple throttles and exits cleanly if throttling continues.
- `--dry-run` mode shows each folder’s derived artist/album pairing without calling Apple so you can audit naming issues quickly.

### Dry Run Auditing

Pass `--dry-run` to any mode (single lookup, `--dir`, or `--dirs2process`) to print the computed search terms and skip every network call and file write. This is handy when a batch suddenly tanks its success rate—you can dump the derived `Artist - Album` pairs, adjust folder names or tags, and then re-run for real.

```bash
python3 getart.py --dir /media/music --dry-run --verbose | tee dry-run.log
```

The summary line still appears, counting everything as skipped work, so you know the run only inspected metadata.

## Installation

### Option 1: `uv tool install` (recommended)

If you just want “whatever is on main right now,” point `uv` at the branch:

```bash
uv tool install --from git+https://github.com/audiomuze/get-art.git@main get-art
```

Re-run the same command (or `uv tool upgrade get-art --from git+https://github.com/audiomuze/get-art.git@main`) to pick up future commits. If you prefer pinned builds, this repository also publishes tagged releases that `uv` can install directly from Git. Pick a tag (for example `v0.1.3`) and run:

```bash
uv tool install --from git+https://github.com/audiomuze/get-art.git@v0.1.3 get-art
```

Note the `git+` prefix and the `@v0.1.3` suffix on the URL; this is the format `uv` expects for Git sources. Alternatively, you can point at the tagged archive directly:

```bash
uv tool install --from https://github.com/audiomuze/get-art/archive/refs/tags/v0.1.3.zip get-art
```

Both commands build an isolated environment under `~/.local/share/uv/tools/get-art` and add a `getart` executable to `~/.local/bin`, so you can run `getart --help` from anywhere. To upgrade later:

```bash
uv tool install --upgrade get-art
```

or uninstall with `uv tool uninstall get-art`.

### Option 2: Clone and run locally

```bash
git clone https://github.com/audiomuze/get-art.git
cd get-art
uv pip install -r requirements.txt  # or: pip install -r requirements.txt
python3 getart.py --help
```

Python 3.8+ is recommended. RapidFuzz and Mutagen are installed via `requirements.txt`; without Mutagen the tag-based fallback is skipped automatically.

## Command-Line Modes

Pick exactly one primary mode per invocation:

- No `--dir/--dirs2process`: single artwork lookup (requires `--artist` plus `--album` or `--title`).
- `--dir`: batch a root folder and every direct subfolder.
- `--dirs2process`: feed explicit folders from a text file.

`--dir` and `--dirs2process` are mutually exclusive; both error out if supplied together.

### 1. Single Artwork Mode (default)

Download one artwork by supplying the artist and either an album or a track title.

```bash
python3 getart.py --artist "Taylor Swift" --album "1989"
python3 getart.py --artist "The Beatles" --title "Yesterday" --output cover.jpg
```

Other options:

- `--artist/-a` (required).
- `--album/-l` or `--title/-t` (one required).
- `--output/-o` to change the default filename (`xfolder.jpg`).
- `--verbose` to print lookup details.
- `--throttle` to enforce a base delay between requests (default 1 s).
- `--dry-run` to print the would-be lookup and exit without contacting Apple Music.

### 2. Batch Directory Mode (`--dir`)

Process every subfolder inside a root directory. Each subfolder must be named `Artist - Album [extra tags]`. Existing `xfolder.jpg` files are skipped unless `--overwrite` is set. Successful folders are logged to `getart.log`, enabling resumable runs.

```bash
python3 getart.py --dir /media/music --verbose --throttle 0.5
python3 getart.py --dir /media/music --ignore-log --overwrite
```

Skips & logs:

- Successful folders are recorded in `getart.log` and are silently skipped on future runs; add `--verbose` if you want the script to print when that happens.
- Failed folders are captured in `getart-failed-lookups.log` and are also skipped unless you pass `--retry` (or `--retry-only`). Their skip notices likewise only appear when `--verbose` is on so that normal runs stay quiet.
- Partial Apple matches are recorded in `getart-fallback-lookups.log` and are skipped unless you opt in with `--retry-fallbacks` or target them explicitly via `--fallback-only`.

Flag reference:

| Flag | Purpose | Works well with | Notes |
| --- | --- | --- | --- |
| `--ignore-log` | Force reruns for folders already recorded in `getart.log`. | `--overwrite` when you want to rebuild every cover. | Optional; cannot resurrect deleted log entries. |
| `--overwrite` | Replace an existing `xfolder.jpg`. | `--ignore-log` to ensure each folder is reconsidered. | Does not affect `xfolder_fallback.jpg`. |
| `--retry` | Include folders captured in `getart-failed-lookups.log`. | `--retry-only` when you only want failed entries. | Safe to combine with `--ignore-log`/`--overwrite`. |
| `--retry-only` | Process only the failed-log entries. | `--retry` (implicitly enabled). | Automatically flips on `--retry` and skips everything else. |
| `--retry-fallbacks` | Include folders captured in `getart-fallback-lookups.log`. | `--fallback-only` when you want to focus on partial matches. | Default runs skip these entries to avoid rewriting the same fallback art. |
| `--fallback-only` | Process only the fallback-log entries. | Automatically implies `--retry-fallbacks`. | Mutually exclusive with `--retry-only`; perfect for polishing fuzzy matches. |

Options:

- `--dir/-d PATH` (required for this mode).
- `--ignore-log` to reprocess folders that were logged as successful.
- `--overwrite` to replace existing `xfolder.jpg` files.
- `--retry` to reprocess entries listed in `getart-failed-lookups.log` (stored alongside `getart.log` in the target directory).
- `--retry-only` to ignore every other folder and process just the paths listed in `getart-failed-lookups.log`.
- `--dry-run` to print each folder’s derived artist/album pairing (counts as skipped work so no files are written).
- Logging file is stored inside the target directory. The script never creates or deletes folders; it only writes `xfolder.jpg` files when the target folder already exists.

### 3. File-Driven Mode (`--dirs2process`)

Read absolute folder paths from a text file (one per line). Comments (`# ...`) and blank lines are ignored.

```bash
python3 getart.py --dirs2process /tmp/folders-to-process.txt --verbose
```

Flag reference:

| Flag | Purpose | Works well with | Notes |
| --- | --- | --- | --- |
| `--ignore-log` | Re-run folders already logged as successful. | `--overwrite` if you want to replace artwork. | Log file lives where you launch the command. |
| `--overwrite` | Replace an existing `xfolder.jpg`. | `--ignore-log` or `--retry`. | Only affects folders that actually exist. |
| `--retry` | Include folders captured in `getart-failed-lookups.log`. | `--retry-only` (implicit). | Log lives alongside `getart.log` in your current working dir. |
| `--retry-only` | Restrict processing to the failed log. | `--retry` (implicit). | Ignores every list entry that isn’t in `getart-failed-lookups.log`. |
| `--retry-fallbacks` | Include folders captured in `getart-fallback-lookups.log`. | `--fallback-only` (implicit). | Fallback log sits next to `getart.log` in your current working dir. |
| `--fallback-only` | Restrict processing to the fallback log. | `--retry-fallbacks` (implicit). | Mutually exclusive with `--retry-only`. |

Behavior:

- If a listed folder exists, artwork is saved inside that folder as `xfolder.jpg` (respecting `--overwrite`).
- If a folder is missing, artwork is saved to the directory you launched the script from using the filename `Artist - Album xfolder.jpg` (illegal filename characters are sanitized automatically).
- Successful entries are logged to `getart.log` in the directory where you launched the script, so future runs can skip them unless you pass `--ignore-log`. Skip notifications only print when `--verbose` is set.
- `--dry-run` echoes each entry’s derived artist/album combo and the destination path so you can confirm naming before any lookup happens.
- Failed lookups are logged to `getart-failed-lookups.log` next to `getart.log` in the directory you launched the script from, and are skipped automatically unless you pass `--retry`. As with batch mode, the skip notice is quiet unless `--verbose` is active.
- Partial Apple matches populate `getart-fallback-lookups.log` beside the other logs so you can revisit them with `--retry-fallbacks`/`--fallback-only` without reprocessing every entry.
- Pair `--retry-only` with `--dirs2process` when you want the file-driven mode to run exclusively on paths that are already captured in `getart-failed-lookups.log`.
- No directories are created when entries are missing.

## Tag-Based Fallback (Optional)

When Mutagen is installed, both batch and file-driven modes automatically fall back to embedded tags whenever the initial Apple lookup fails. The script grabs the first supported audio file in the target folder, reads its `albumartist`/`artist` and `album` tags, builds every distinct combination, and retries the lookup for each combo until one succeeds (or all fail). If Apple only returns a partial overlap during this process, the resulting artwork is written as `xfolder_fallback.jpg` and recorded in `getart-fallback-lookups.log` so you can retry later with `--retry-fallbacks`. No additional flags are required; if Mutagen isn’t available or the folder lacks tagged files, the behavior remains unchanged.

## Rate Limiting & Retries

- Initial requests use your chosen `--throttle` (default 1s).
- On the first 403/429 response, the script waits 5 seconds, switches to 5-second inter-request delays, and retries.
- If Apple continues throttling, the script exits early with status code 2 so you can rerun later.

## Example List File

```
/tunes/Alice Merton - Visions [2448.0 kHz]
/tunes/Avery Anna - Breakup Over Breakfast [2448.0 kHz]
/tunes/Cavetown - Running With Scissors
/tunes/Charli Moon - Open Roads & Heartstrings [2448.0 kHz]
```

Each basename is parsed as `Artist - Album`; square-bracket and parenthetical suffixes are stripped from the album title before searching Apple Music.

## Attribution

- The HTTP/query portion of `AppleMusicArtworkDownloader` (iTunes Search requests, response handling, and image URL rewriting) is adapted from the excellent [regosen/get_cover_art](https://github.com/regosen/get_cover_art) project (MIT License). Additional features in this repository build upon that foundation.

## Tips

- Folder names must contain a literal ` - ` separating artist and album. Additional tags can live in `[...]` or `(...)` and will be ignored during lookup.
- Disc/box-set folders such as `CD1`, `Disc 2`, or `Blu-Ray` inherit artist/album metadata from their parent folder automatically, so you can feed nested structures directly.
- Use `--title` only when you’re targeting single tracks that lack an album directory.
- Combine `--ignore-log` with `--overwrite` to rebuild all artwork files from scratch.
- The helper function `get_apple_music_artwork()` can be imported into other scripts if you prefer direct Python API usage.

## Exit Codes

- `0`: Success.
- `1`: Artwork not found or invalid arguments.
- `2`: Apple Music throttled requests even after the enforced backoff; try again later.

## License

Released under the MIT License; see [LICENSE](LICENSE) for details. Portions of the HTTP/query stack retain the upstream MIT notice from [regosen/get_cover_art](https://github.com/regosen/get_cover_art).
