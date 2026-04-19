# OLM Reader

A fast, self-contained viewer for Outlook for Mac (`.olm`) archive files. Browse, search, and download attachments from your archived emails directly in the browser — no dependencies beyond Python 3.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue) ![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen) ![License: MIT](https://img.shields.io/badge/license-MIT-yellow)

## Features

- **Full-text search** across subject, body, sender, and recipients (SQLite FTS5)
- **Sent / received** distinction — automatically detected from your email address
- **Advanced filters** — filter by sender, recipient, or presence of attachments
- **Multi-archive support** — load up to 4 (or more) `.olm` files into a single unified index
- **Attachment viewing and download** — open or save attachments directly from the browser
- **Fast indexing** — parallel XML parsing with a persistent cache; a 2 GB archive indexes in minutes and reopens instantly
- **Folder sidebar** — browse by folder with message counts
- **No dependencies** — pure Python standard library, no pip installs required

## Requirements

- Python 3.8 or newer
- macOS (tested), should also work on Linux/Windows

## Usage

```bash
python3 olm_reader.py path/to/archive.olm
```

With multiple archives:

```bash
python3 olm_reader.py archive1.olm archive2.olm archive3.olm archive4.olm
```

With your email address (enables sent/received detection):

```bash
python3 olm_reader.py --my-email you@example.com archive.olm
```

The script will index the archive(s) on first run (this may take a few minutes for large files), then open `http://localhost:8765` in your browser automatically. On subsequent runs the cached index loads instantly.

## Command-line options

| Option | Description |
|---|---|
| `--my-email EMAIL` | Your email address, used to distinguish sent from received mail |
| `--port PORT` | HTTP port (default: `8765`) |
| `--workers N` | Parallel indexing threads (default: number of CPU cores) |
| `--reindex` | Force a full re-index, ignoring the cache |

## How it works

OLM files are ZIP archives containing XML files for each email and a shared `com.microsoft.__Attachments/` directory per folder. On first run, OLM Reader:

1. Scans all XML files in parallel using a thread pool
2. Parses email metadata and body (HTML + plain text)
3. Writes everything into a SQLite database with an FTS5 full-text index
4. Caches the database in `~/.cache/olm_reader/` keyed by a fingerprint of the source files

On subsequent runs the fingerprint is checked; if the `.olm` files haven't changed, the cache is reused immediately.

## Attachment availability

OLM exports made by Outlook for Mac do not always include the actual attachment files, especially for large documents (PDFs, PowerPoint, etc.). Calendar invites (`.ics`) and small files are typically present. When an attachment listed in the email metadata is not found in the archive, it is shown as *"non disponibile nell'archivio"* rather than a broken download link.

## Cache location

```
~/.cache/olm_reader/<fingerprint>.db
```

Each unique combination of OLM files gets its own cache file. To clear all caches:

```bash
rm -rf ~/.cache/olm_reader/
```

## License

MIT
