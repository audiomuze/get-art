# Apple Music Artwork Downloader

Download the highest-resolution (up to 9999×9999) Apple Music artwork for your library using simple command-line workflows. The script can operate on one album/track, on every folder under a root directory, or on explicit folder paths listed inside a text file. All modes are resumable, polite to Apple’s API, and rely on the Python standard library plus RapidFuzz (for scoring) and Mutagen (for tag-based fallbacks).

It does not embed images in file metadata.

## Features

- Retrieves cover art at 9999×9999 px (quality 100) by default.
- Intelligent matching on artist+album or artist+track names, even when folder names carry extra tags like `[24-96 FLAC]`.
- Batch directory processing with automatic logging so previously successful folders are skipped unless you opt in.
- File-driven processing for curated folder lists, saving art either in-place or to the current working directory when folders are missing.
- Separate success and failure logs (`getart.log` and `getart-failed-lookups.log`) keep runs resumable; use `--retry` when you want to reattempt previously failed lookups.
- Optional tag-based fallback: if [Mutagen](https://mutagen.readthedocs.io/) is installed, the script inspects the first audio file in a folder and cycles through its `albumartist`/`artist` tags when the folder name lookup fails.
- Fuzzy Apple matches are quarantined: the artwork is saved as `xfolder_fallback.jpg`, not logged as successful, and can be revisited later.
- [RapidFuzz](https://maxbachmann.github.io/RapidFuzz/) scoring ranks partial matches so the closest release wins whenever Apple doesn’t return an exact title hit.
- Disc-aware parsing automatically falls back to the parent folder’s `Artist - Album` name whenever a subfolder looks like `CD1`, `Disc 2`, `Blu-Ray`, or other box-set media splits.
- Built-in rate-limit handling: escalates to 5-second delays when Apple throttles and exits cleanly if throttling continues.

## Installation

### Option 1: `uv tool install` (recommended)

If you just want “whatever is on main right now,” point `uv` at the branch:

```bash
uv tool install --from git+https://github.com/audiomuze/get-art.git@main get-art
```

Re-run the same command (or `uv tool upgrade get-art --from git+https://github.com/audiomuze/get-art.git@main`) to pick up future commits. If you prefer pinned builds, this repository also publishes tagged releases that `uv` can install directly from Git. Pick a tag (for example `v0.1.2`) and run:

```bash
uv tool install --from git+https://github.com/audiomuze/get-art.git@v0.1.2 get-art
```

Note the `git+` prefix and the `@v0.1.2` suffix on the URL; this is the format `uv` expects for Git sources. Alternatively, you can point at the tagged archive directly:

```bash
uv tool install --from https://github.com/audiomuze/get-art/archive/refs/tags/v0.1.2.zip get-art
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

### 1. Single Artwork Mode (default)

Download one artwork by supplying the artist and either an album or a track title.

```bash
python3 getart.py --artist "Taylor Swift" --album "1989"
python3 getart.py --artist "The Beatles" --title "Yesterday" --output cover.jpg
```

Options:

- `--artist/-a` (required).
- `--album/-l` or `--title/-t` (one required).
- `--output/-o` to change the default filename (`xfolder.jpg`).
- `--verbose` to print lookup details.
- `--throttle` to enforce a base delay between requests (default 1 s).

### 2. Batch Directory Mode (`--dir`)

Process every subfolder inside a root directory. Each subfolder must be named `Artist - Album [extra tags]`. Existing `xfolder.jpg` files are skipped unless `--overwrite` is set. Successful folders are logged to `getart.log`, enabling resumable runs.

```bash
python3 getart.py --dir /media/music --verbose --throttle 0.5
python3 getart.py --dir /media/music --ignore-log --overwrite
```

Options:

- `--dir/-d PATH` (required for this mode).
- `--ignore-log` to reprocess folders that were logged as successful.
- `--overwrite` to replace existing `xfolder.jpg` files.
- `--retry` to reprocess entries listed in `getart-failed-lookups.log` (stored alongside `getart.log` in the target directory).
- `--retry-only` to ignore every other folder and process just the paths listed in `getart-failed-lookups.log`.
- Logging file is stored inside the target directory. The script never creates or deletes folders; it only writes `xfolder.jpg` files when the target folder already exists.

### 3. File-Driven Mode (`--dirs2process`)

Read absolute folder paths from a text file (one per line). Comments (`# ...`) and blank lines are ignored.

```bash
python3 getart.py --dirs2process /tmp/folders-to-process.txt --verbose
```

Behavior:

- If a listed folder exists, artwork is saved inside that folder as `xfolder.jpg` (respecting `--overwrite`).
- If a folder is missing, artwork is saved to the directory you launched the script from using the filename `Artist - Album xfolder.jpg` (illegal filename characters are sanitized automatically).
- Successful entries are logged to `getart.log` in the directory where you launched the script, so future runs can skip them unless you pass `--ignore-log`.
- Failed lookups are logged to `getart-failed-lookups.log` next to `getart.log` in the directory you launched the script from, and are skipped automatically unless you pass `--retry`.
- Pair `--retry-only` with `--dirs2process` when you want the file-driven mode to run exclusively on paths that are already captured in `getart-failed-lookups.log`.
- No directories are created when entries are missing.

## Tag-Based Fallback (Optional)

When Mutagen is installed, both batch and file-driven modes automatically fall back to embedded tags whenever the initial Apple lookup fails. The script grabs the first supported audio file in the target folder, reads its `albumartist`/`artist` and `album` tags, builds every distinct combination, and retries the lookup for each combo until one succeeds (or all fail). If Apple only returns a partial overlap during this process, the resulting artwork is written as `xfolder_fallback.jpg` and purposely left out of `getart.log` so you can retry later. No additional flags are required; if Mutagen isn’t available or the folder lacks tagged files, the behavior remains unchanged.

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
