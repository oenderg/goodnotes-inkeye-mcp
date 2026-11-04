# goodnotes-inkeye-mcp

An MCP stdio server for reading a **live** Goodnotes library. It exposes
notebook metadata, page images rendered from the current strokes, exact typed
text, and a clearly-labelled OCR/PDF text hint.

It is a thin transport over the `gnreader` distribution (`gnstore`, `gnlive`,
`gntext` and `gnreader`). Nothing is uploaded; there are no network calls at
runtime.

## How it works

1. `gnstore` copies the live RocksDB (`notes_main.db`), its WAL and the SQLite
   projection into a writable snapshot. The Goodnotes container is only read.
2. `gnlive` loads the page's imported PDF/image background and decodes the live
   page records with `gnreader`.
3. Strokes, text, images and fills are drawn in `NotesZIndex` order.
   Highlighters use a per-channel multiply and preserve ink.
4. The PNG and a JSON sidecar are cached under `$GNREADER_HOME` or
   `$GOODNOTES_INKEYE_HOME`; the cache is capped and pruned.

## Requirements

- macOS with the Goodnotes application installed.
- Python 3.10+.
- The application that launches the MCP server needs **Full Disk Access** to
  read `~/Library/Containers/com.goodnotesapp.x`.
- A writable home for snapshots/cache. `GNREADER_HOME` defaults to
  `~/.cache/gnreader`; `GOODNOTES_INKEYE_HOME` is also honored.

## Install

Private repository:

```sh
python -m venv ~/.local/share/goodnotes-inkeye/venv
~/.local/share/goodnotes-inkeye/venv/bin/pip install \
  "git+https://github.com/oenderg/goodnotes-inkeye-mcp.git"
```

The `gnreader` dependency is declared as
`gnreader @ git+https://github.com/oenderg/gnreader.git`. For local
development, install the local checkout first and then install this package
with `--no-deps`:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ../gnreader
pip install -e . --no-deps
pip install "mcp>=1.12,<2" pypdfium2 Pillow
```

## Register with Claude

```sh
claude mcp add goodnotes-inkeye -s user -- \
  ~/.local/share/goodnotes-inkeye/venv/bin/goodnotes-inkeye-mcp
```

The console script starts the stdio server. The equivalent JSON is:

```json
{
  "mcpServers": {
    "goodnotes-inkeye": {
      "command": "<venv>/bin/goodnotes-inkeye-mcp",
      "args": []
    }
  }
}
```

Use the real absolute path to the venv on your machine. Do not point the
command at a checkout of the source tree unless the checkout is installed.

## Tools

| Tool | Returns |
|---|---|
| `list_notebooks(folder='')` | Folder path, UUID, live page count, modified date, `source=live`. Optional folder prefix. |
| `find_notebooks(query)` | Up to 50 notebooks matching all query words against folder/name/UUID. |
| `get_notebook_info(notebook)` | UUID, folder, live/deleted page counts, dates, page ids. |
| `get_page_image(notebook, page, max_side=0)` | `[label, Image]` PNG, 1-based live page, fresh snapshot. |
| `get_pages(notebook, start, end, max_side)` | Up to 8 PNG pages from one snapshot. |
| `get_page_text(notebook, page)` | Exact typed-text items from `gntext` plus a clearly-labelled unreliable OCR hint. |
| `get_page_text_hint(notebook, page)` | Cheap OCR/PDF text hint; explicitly unreliable. |
| `server_status()` | Snapshot age, library counts, package versions, capabilities, cache use. |

Notebook arguments accept a document UUID, exact name, or unique name part.
Ambiguous input returns a candidate list instead of guessing. Page numbers are
1-based over live (non-deleted) pages.

## Privacy and safety

- The Goodnotes container and backup folder are never modified.
- Snapshots and render caches are the only writes; they live under the
  configured home, never inside the package or the container.
- No network calls at runtime.
- OCR is MyScript output and may be wrong, incomplete or stale. Use
  `get_page_text` for exact typed items and `get_page_image` to verify ink.

## Known limits

The full list comes from `gnreader.capabilities()` and is printed by
`server_status()`.

- No write support.
- Legacy item kinds `f3`, `f4`, `f6`, `f22` are unsupported.
- Nexus/Yjs layout is approximate; shape fill/stroke, math and sticky notes are
  partial.
- Whiteboard notebooks have no page canvas and render blank.
- Missing built-in/sticker backgrounds render white.
- Eraser masks are decoded but not applied.
- Highlighter bar ends are square.
- Cache entries are invalidated by package version and installed-renderer code
  fingerprint.

## Updates

Upgrade the package in the server venv, then start a **new Claude session** so
the MCP process restarts and loads the new code. A long-running server keeps
the old modules in memory.

## License

Private repository. All rights reserved; no license is granted.
