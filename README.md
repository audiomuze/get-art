# Apple Music Artwork Downloader

Download the highest-resolution (up to 9999×9999) Apple Music artwork for your library using simple command-line workflows. The script can operate on one album/track, on every folder under a root directory, or on explicit folder paths listed inside a text file. All modes are resumable, polite to Apple’s API, and require only the Python standard library.

## Features

- Retrieves cover art at 9999×9999 px (quality 100) by default.
- Intelligent matching on artist+album or artist+track names, even when folder names carry extra tags like `[24-96 FLAC]`.
- Batch directory processing with automatic logging so previously successful folders are skipped unless you opt in.
- File-driven processing for curated folder lists, saving art either in-place or to the current working directory when folders are missing.
- Built-in rate-limit handling: escalates to 5-second delays when Apple throttles and exits cleanly if throttling continues.

## Installation

```bash
git clone https://github.com/yourname/get-art.git
cd get-art
python3 getart.py --help
```

Python 3.8+ is recommended. No third-party packages are required.

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
- No directories are created when entries are missing.

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

## Tips

- Folder names must contain a literal ` - ` separating artist and album. Additional tags can live in `[...]` or `(...)` and will be ignored during lookup.
- Use `--title` only when you’re targeting single tracks that lack an album directory.
- Combine `--ignore-log` with `--overwrite` to rebuild all artwork files from scratch.
- The helper function `get_apple_music_artwork()` can be imported into other scripts if you prefer direct Python API usage.

## Exit Codes

- `0`: Success.
- `1`: Artwork not found or invalid arguments.
- `2`: Apple Music throttled requests even after the enforced backoff; try again later.

## License

MIT-style license (adapt or reuse freely). Contributions welcome!
