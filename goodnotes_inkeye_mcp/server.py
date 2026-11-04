"""Live Goodnotes MCP server.

Reads the LIVE Goodnotes store (RocksDB + SQLite + WAL) through ``gnstore`` /
``gnlive`` and renders with ``gnreader``. Exposes notebook metadata,
live-rendered page images, and OCR/PDF text hints over MCP stdio.

Safety: the Goodnotes container is read-only. Everything this server writes
(snapshots, render cache) lives under ``$GNREADER_HOME`` or
``$GOODNOTES_INKEYE_HOME``. No network calls at runtime.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import os
import platform
import tempfile
import time
from pathlib import Path

import gnstore
import gnlive
import gnreader
import pypdfium2 as pdfium
try:
    import gntext
    GNTEXT_ERROR = ""
except Exception as _gntext_exc:  # pragma: no cover - optional extra
    gntext = None
    GNTEXT_ERROR = f"{type(_gntext_exc).__name__}: {_gntext_exc}"
from mcp.server.fastmcp import FastMCP, Image

SERVER_VERSION = "0.3.0"
DEFAULT_MAX_SIDE = 1600
MAX_SIDE = 4096
MIN_MAX_SIDE = 200
MAX_PAGES_PER_CALL = 8
CACHE_LIMIT_BYTES = 128 * 1024 * 1024
CACHE_MAX_FILES = 2000
CACHE_DIR = Path(gnstore.GNREADER_HOME) / "inkeye-cache"
CACHE_PAGES = CACHE_DIR / "pages"

INSTRUCTIONS = """\
This server reads the user's LIVE Goodnotes library, not the auto-backup PDFs.

Notebook IDs are Goodnotes document UUIDs, shown in square brackets by
list_notebooks, e.g. PAGE-ID. Names and folder
paths can change and can be ambiguous; prefer the UUID. Tools also accept a
unique part of the notebook name, and return a candidate list when ambiguous.

Page numbers are 1-based over live (non-deleted) pages. Page ids are the
36-character UUIDs returned by get_notebook_info and are different from the
RocksDB container id (page id + 1; internal detail).

Each page-image call takes a fresh incremental snapshot, so edits made minutes
ago are included. Page images include the imported PDF/raster background and
handwriting, text boxes and variable-width strokes in Goodnotes z-order. The
image label reports snapshot age, the page's last-modified time and any render
warnings (for example unsupported item types that were skipped). Use
get_page_image to verify what is actually written; OCR is unreliable and is
only a hint. get_page_text returns exact typed-text items from the schema
lane's gntext extractor plus a clearly-labelled OCR hint.
"""

mcp = FastMCP("goodnotes-inkeye", instructions=INSTRUCTIONS, log_level="WARNING")


# ---------------------------------------------------------------------------
# Notebook resolution / formatting
# ---------------------------------------------------------------------------

def _label(nb: dict) -> str:
    folder = (nb.get("folder_path") or "").strip("/")
    name = nb.get("name") or ""
    return f"{folder}/{name}" if folder else name


def _all_notebooks() -> list[dict]:
    return gnstore.list_notebooks(snapshot=gnstore.DEFAULT_SNAPSHOT)


def _resolve(notebook: str, nbs: list[dict] | None = None) -> dict:
    """Resolve a document UUID, exact name/label, or unique substring.

    Ambiguity is an error, not a guess; the message lists up to 15 candidates.
    No filesystem path is ever accepted or opened here.
    """
    if not isinstance(notebook, str) or not notebook.strip():
        raise ValueError("Notebook must be a non-empty id or name.")
    q = notebook.strip().strip("/")
    if len(q) > 200:
        raise ValueError("Notebook argument is too long.")
    if nbs is None:
        nbs = _all_notebooks()

    ql = q.casefold()
    qid = q.upper()
    exact_id = [nb for nb in nbs if nb["id"].upper() == qid]
    if len(exact_id) == 1:
        return exact_id[0]
    exact_name = [nb for nb in nbs if (nb.get("name") or "").casefold() == ql]
    if len(exact_name) == 1:
        return exact_name[0]
    exact_label = [nb for nb in nbs if _label(nb).casefold() == ql]
    if len(exact_label) == 1:
        return exact_label[0]

    hits = [nb for nb in nbs
            if ql in _label(nb).casefold() or ql in (nb.get("name") or "").casefold()
            or ql in nb["id"].casefold()]
    # Exact-name/label duplicates are also candidates, not a guess.
    seen = set()
    merged = []
    for nb in exact_name + exact_label + hits:
        if nb["id"] not in seen:
            seen.add(nb["id"])
            merged.append(nb)
    if len(merged) == 1:
        return merged[0]
    if not merged:
        raise ValueError(
            f"No notebook matching {notebook!r}. Use list_notebooks or find_notebooks.")
    lines = "\n".join(f"- {_label(nb)}  [{nb['id']}]  ({nb['page_count']} pages)"
                      for nb in merged[:15])
    more = f"\n... and {len(merged) - 15} more" if len(merged) > 15 else ""
    raise ValueError(
        f"{notebook!r} matches {len(merged)} notebooks; be more specific:\n{lines}{more}")


def _notebook_line(nb: dict) -> str:
    modified = nb.get("modified") or "unknown"
    return (f"- {_label(nb)}  [{nb['id']}]  pages={nb['page_count']}"
            f"  modified={modified}  source=live")


def _live_pages(nb: dict) -> list[dict]:
    return [p for p in nb.get("pages", []) if not p.get("deleted")]


def _check_page(page: int, n: int) -> int:
    if not isinstance(page, int) or isinstance(page, bool):
        raise ValueError("page must be an integer (1-based).")
    if not 1 <= page <= n:
        raise ValueError(f"Page {page} out of range; this notebook has {n} live pages (1-based).")
    return page


def _check_max_side(max_side: int) -> int:
    if not isinstance(max_side, int) or isinstance(max_side, bool):
        raise ValueError("max_side must be an integer (0 = default 1600).")
    if max_side == 0:
        return DEFAULT_MAX_SIDE
    if not MIN_MAX_SIDE <= max_side <= MAX_SIDE:
        raise ValueError(
            f"max_side must be 0 or between {MIN_MAX_SIDE} and {MAX_SIDE} pixels.")
    return max_side


# ---------------------------------------------------------------------------
# Snapshot / cache helpers
# ---------------------------------------------------------------------------

def _ensure_cache() -> None:
    CACHE_PAGES.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def _file_lock(name: str):
    """Best-effort cross-process exclusive lock under $GNREADER_HOME."""
    try:
        _ensure_cache()
        f = open(CACHE_DIR / name, "a+")
    except OSError:
        yield
        return
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _fresh_snapshot() -> Path:
    """Refresh the APFS-clone incremental snapshot; cheap when unchanged.

    A file lock keeps parallel MCP tool calls from copying the same RocksDB
    files into the snapshot at once. The lock is released before rendering.
    """
    with _file_lock(".snapshot.lock"):
        try:
            gnstore.snapshot(fresh=True, quiet=True)
            # The copy is incremental; mark the successful freshness check so
            # labels can say how recently the library was checked, even when no
            # file stats changed and the snapshot content was reused.
            try:
                (CACHE_DIR / ".snapshot-check").touch()
            except OSError:
                pass
        except Exception as exc:
            raise RuntimeError(
                f"snapshot refresh failed: {type(exc).__name__}: {exc}") from None
    return Path(gnstore.DEFAULT_SNAPSHOT)


def _snapshot_state_path() -> Path:
    return Path(gnstore.DEFAULT_SNAPSHOT) / ".gnstore-state.json"


def _snapshot_age_s() -> float | None:
    """Age since the last successful freshness check (written under cache)."""
    p = CACHE_DIR / ".snapshot-check"
    if not p.exists():
        p = _snapshot_state_path()
    if not p.exists():
        p = Path(gnstore.DEFAULT_SNAPSHOT)
    try:
        return max(0.0, time.time() - p.stat().st_mtime)
    except OSError:
        return None


def _snapshot_data_age_s() -> float | None:
    """Age of the actual copied snapshot data (changes only when source changes)."""
    p = _snapshot_state_path()
    if not p.exists():
        p = Path(gnstore.DEFAULT_SNAPSHOT)
    try:
        return max(0.0, time.time() - p.stat().st_mtime)
    except OSError:
        return None


def _snapshot_generation() -> str:
    """A key that changes whenever the incremental snapshot changed."""
    p = _snapshot_state_path()
    try:
        st = p.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        try:
            st = Path(gnstore.DEFAULT_SNAPSHOT).stat()
            return f"dir:{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            return "missing"


def _archive_fingerprint() -> str:
    """Invalidate cached pixels when the installed renderer code changes."""
    parts = []
    for p in (Path(gnreader.__file__).parent / "render.py",
              Path(gnreader.__file__).parent / "decode.py",
              Path(gntext.__file__).parent / "items.py"):
        try:
            st = p.stat()
            parts.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{p.name}:missing")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _cache_key(page_id: str, max_side: int, generation: str) -> str:
    raw = (f"{generation}|{page_id}|{max_side}|{gnlive.__version__}"
           f"|{gnreader.__version__}|{_archive_fingerprint()}").encode()
    return hashlib.sha256(raw).hexdigest()


def _cache_get(key: str) -> tuple[bytes | None, dict]:
    p = CACHE_PAGES / f"{key}.png"
    meta_p = CACHE_PAGES / f"{key}.json"
    try:
        if p.is_file() and p.stat().st_size > 0:
            meta = {}
            if meta_p.is_file():
                try:
                    meta = json.loads(meta_p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}
            return p.read_bytes(), meta
    except OSError:
        pass
    return None, {}


def _cache_put(key: str, data: bytes, meta: dict) -> None:
    """Atomic, lock-protected cache write; failure never breaks a page read."""
    try:
        _ensure_cache()
        with _file_lock(".cache.lock"):
            p = CACHE_PAGES / f"{key}.png"
            fd, tmp_name = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp",
                                             dir=str(CACHE_PAGES))
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp_name, p)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            meta_tmp = CACHE_PAGES / f".{key}.json.tmp"
            meta_tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(meta_tmp, CACHE_PAGES / f"{key}.json")
            _prune_cache()
    except OSError:
        pass


def _cache_files() -> list[Path]:
    if not CACHE_DIR.exists():
        return []
    return [p for p in CACHE_DIR.rglob("*")
            if p.is_file() and not p.name.endswith(".tmp")
            and not p.name.endswith(".lock") and not p.name.startswith(".")]


def _prune_cache() -> None:
    files = _cache_files()
    total = sum(p.stat().st_size for p in files if p.exists())
    excess_files = max(0, len(files) - CACHE_MAX_FILES)
    for p in sorted(files, key=lambda x: x.stat().st_mtime if x.exists() else 0):
        if total <= CACHE_LIMIT_BYTES and excess_files <= 0:
            break
        try:
            size = p.stat().st_size
            p.unlink()
            total -= size
            excess_files -= 1
        except OSError:
            pass
    for p in CACHE_PAGES.glob(".*.tmp"):
        try:
            if time.time() - p.stat().st_mtime > 3600:
                p.unlink()
        except OSError:
            pass


def _render_cached(nb: dict, page: int, max_side: int,
                   generation: str, snapshot: Path) -> tuple[bytes, dict]:
    pages = _live_pages(nb)
    page_id = pages[page - 1]["page_id"]
    key = _cache_key(page_id, max_side, generation)
    cached, meta = _cache_get(key)
    if cached is not None:
        return cached, {"page_id": page_id, "cached": True,
                        "warnings": meta.get("warnings", [])}
    try:
        png, info = gnlive.render_page(
            nb["id"], page, max_side=max_side, fresh=False, snapshot=str(snapshot))
    except Exception as exc:
        raise RuntimeError(
            f"render failed for {nb.get('name')!r} page {page}: "
            f"{type(exc).__name__}: {exc}") from None
    _cache_put(key, png, {"warnings": info.get("warnings", []),
                          "page_id": page_id,
                          "archive_fingerprint": _archive_fingerprint()})
    return png, info


def _warning_label(warnings) -> str:
    if not warnings:
        return "warnings: none"
    total = 0
    parts = []
    for w in warnings:
        n = int(w.get("count", 1))
        total += n
        kind = w.get("kind", "unknown")
        parts.append(kind if n == 1 else f"{kind} x{n}")
    unit = "item" if total == 1 else "items"
    return f"warnings: {total} {unit} skipped ({', '.join(parts)})"


def _age_text(age: float | None) -> str:
    return f"{age:.1f}s" if age is not None else "unknown"


def _image_label(nb: dict, pageno: int, total: int, page: dict, info: dict,
                 age: float | None) -> str:
    modified = page.get("modified") or "unknown"
    fresh = f"rendered from library snapshot taken {_age_text(age)} ago"
    return (f"{_label(nb)}, page {pageno}/{total} "
            f"(page_id {info['page_id']}; {fresh}; page modified {modified}; "
            f"{_warning_label(info.get('warnings', []))})")


def _image(png: bytes) -> Image:
    return Image(data=png, format="png")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_notebooks(folder: str = "") -> str:
    """List live Goodnotes notebooks, with folder path, page count, modified time, source.

    Notebook IDs are 36-character Goodnotes document UUIDs shown in square
    brackets. Prefer the UUID when calling other tools; names can be renamed or
    duplicated. Page counts are live (non-deleted) pages.

    Args:
        folder: Optional folder-path prefix (case-insensitive), e.g. "GBS" or
            "folder/subfolder". Empty lists the whole live library.
    """
    _fresh_snapshot()
    nbs = _all_notebooks()
    prefix = (folder or "").strip().strip("/").casefold()
    if len(prefix) > 200:
        raise ValueError("folder argument is too long.")
    if prefix:
        nbs = [nb for nb in nbs
               if (nb.get("folder_path") or "").casefold() == prefix
               or (nb.get("folder_path") or "").casefold().startswith(prefix + "/")]
    if not nbs:
        return f"No notebooks found under folder {folder!r}."
    nbs = sorted(nbs, key=lambda nb: ((nb.get("folder_path") or "").casefold(),
                                      (nb.get("name") or "").casefold()))
    age = _snapshot_age_s()
    header = (f"{len(nbs)} notebooks from the live store (source=live, "
              f"snapshot age {age:.1f}s):" if age is not None
              else f"{len(nbs)} notebooks from the live store (source=live):")
    return "\n".join([header, ""] + [_notebook_line(nb) for nb in nbs])


@mcp.tool()
def find_notebooks(query: str) -> str:
    """Find live notebooks whose folder/name/id contains every query word.

    Case-insensitive. Each result includes the notebook UUID, folder path,
    live page count, last modified time and source. Up to 50 shown.

    Args:
        query: Words to look for, e.g. "calculus notes" or part of a UUID.
    """
    _fresh_snapshot()
    q = query or ""
    if len(q) > 200:
        raise ValueError("query is too long.")
    words = q.casefold().split()
    if not words:
        return "Empty query. Give one or more words, e.g. 'calculus notes'."
    nbs = _all_notebooks()
    hits = [nb for nb in nbs
            if all(w in f"{_label(nb)} {nb['id']}".casefold() for w in words)]
    if not hits:
        return f"No notebook matches {query!r}. Try list_notebooks to see the library."
    hits.sort(key=lambda nb: ((nb.get("folder_path") or "").casefold(),
                              (nb.get("name") or "").casefold()))
    shown = hits[:50]
    lines = [f"{len(hits)} match(es) (source=live):", ""]
    lines += [_notebook_line(nb) for nb in shown]
    if len(hits) > 50:
        lines.append(f"... and {len(hits) - 50} more; narrow the query.")
    return "\n".join(lines)


@mcp.tool()
def get_notebook_info(notebook: str) -> str:
    """Show id, folder, page count, dates and page ids for one live notebook.

    The notebook argument may be a document UUID (safest), an exact name, or a
    unique part of the name. Ambiguous input returns a candidate list instead
    of guessing. Page numbers are 1-based over live (non-deleted) pages; page
    ids are the 36-character UUIDs accepted by page-level tools.

    Args:
        notebook: Notebook UUID, exact name, or unique part of the name.
    """
    _fresh_snapshot()
    nb = gnstore.get_notebook(_resolve(notebook)["id"],
                              snapshot=gnstore.DEFAULT_SNAPSHOT)
    live = _live_pages(nb)
    deleted = len(nb.get("pages", [])) - len(live)
    lines = [
        f"name: {nb['name']}",
        f"id: {nb['id']}",
        f"folder: {nb.get('folder_path') or '(root)'}",
        f"pages: {len(live)} live" + (f" ({deleted} deleted)" if deleted else ""),
        f"created: {nb.get('created') or 'unknown'}",
        f"modified: {nb.get('modified') or 'unknown'}",
        f"source: live Goodnotes store (RocksDB notes_main.db + projection.sqlite)",
        "",
        "pages (1-based over live pages):",
    ]
    for i, p in enumerate(live, 1):
        lines.append(f"{i:3d}  {p['page_id']}  modified={p.get('modified') or 'unknown'}")
    return "\n".join(lines)


@mcp.tool()
def get_page_image(notebook: str, page: int = 1, max_side: int = 0) -> list:
    """Render one live notebook page (background + handwriting) as a PNG image.

    Takes a fresh incremental snapshot first, so very recent edits are included.
    The image is in Goodnotes z-order (bottom to top); imported PDF/raster
    backgrounds are included. Use this to read handwriting instead of trusting
    OCR. Notebook can be a UUID or a unique name part; ambiguous names error
    with candidates.

    Args:
        notebook: Notebook UUID, exact name, or unique part of the name.
        page: Page number, 1-based over live (non-deleted) pages.
        max_side: Longest side in pixels; 0 = 1600 default, range 200-4096.
    """
    _fresh_snapshot()
    age = _snapshot_age_s()
    nbs = _all_notebooks()
    nb = gnstore.get_notebook(_resolve(notebook, nbs)["id"],
                              snapshot=gnstore.DEFAULT_SNAPSHOT)
    live = _live_pages(nb)
    page = _check_page(page, len(live))
    max_side = _check_max_side(max_side)
    png, info = _render_cached(nb, page, max_side, _snapshot_generation(),
                               Path(gnstore.DEFAULT_SNAPSHOT))
    label = _image_label(nb, page, len(live), live[page - 1], info, age)
    return [label, _image(png)]


@mcp.tool()
def get_pages(notebook: str, start: int = 1, end: int = 0,
             max_side: int = 0) -> list:
    """Render up to 8 consecutive live pages as PNG images, in page order.

    One fresh snapshot is taken for the whole call. At most 8 pages are
    returned; call again with the next `start` for more. Pages are 1-based over
    live (non-deleted) pages. Notebook can be a UUID or a unique name part.

    Args:
        notebook: Notebook UUID, exact name, or unique part of the name.
        start: First page, 1-based.
        end: Last page, inclusive; 0 means as many as the 8-page cap allows.
        max_side: Longest side in pixels; 0 = 1600 default, range 200-4096.
    """
    _fresh_snapshot()
    age = _snapshot_age_s()
    nb = gnstore.get_notebook(_resolve(notebook)["id"],
                              snapshot=gnstore.DEFAULT_SNAPSHOT)
    live = _live_pages(nb)
    n = len(live)
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        raise ValueError("start must be an integer >= 1.")
    if start > n:
        raise ValueError(f"Page {start} out of range; this notebook has {n} live pages.")
    if not isinstance(end, int) or isinstance(end, bool) or end < 0:
        raise ValueError("end must be 0 or an integer >= start.")
    if end and end < start:
        raise ValueError("end must be 0 or >= start.")
    requested_end = min(end or n, n)
    last = min(requested_end, start + MAX_PAGES_PER_CALL - 1)
    max_side = _check_max_side(max_side)
    generation = _snapshot_generation()
    out: list = []
    for pageno in range(start, last + 1):
        png, info = _render_cached(nb, pageno, max_side, generation,
                                   Path(gnstore.DEFAULT_SNAPSHOT))
        out.append(_image_label(nb, pageno, n, live[pageno - 1], info, age))
        out.append(_image(png))
    if last < requested_end:
        out.append(f"Stopped at page {last}; {requested_end - last} more requested. "
                   f"Call again with start={last + 1}.")
    return out


@mcp.tool()
def get_page_text_hint(notebook: str, page: int = 1) -> str:
    """Cheap text hint for one page: unreliable Goodnotes OCR + typed + PDF text.

    The OCR section comes from Goodnotes' MyScript index and is explicitly
    UNRELIABLE: it can be wrong, incomplete, stale, or empty. Typed-text-box
    text comes from the same index and may be incomplete. Imported PDFs may
    also have a real text layer, which is extracted separately. Handwriting is
    never reliable as text — use get_page_image to verify.

    Args:
        notebook: Notebook UUID, exact name, or unique part of the name.
        page: Page number, 1-based over live (non-deleted) pages.
    """
    _fresh_snapshot()
    nb = gnstore.get_notebook(_resolve(notebook)["id"],
                              snapshot=gnstore.DEFAULT_SNAPSHOT)
    live = _live_pages(nb)
    page = _check_page(page, len(live))
    page_id = live[page - 1]["page_id"]
    hint = gnlive.page_text_hint(page_id) if hasattr(gnlive, "page_text_hint") else None
    if hint is None:
        raise RuntimeError("this gnlive build has no page_text_hint(); update gnreader")

    pdf_text = ""
    bg = gnstore.get_page_background(page_id, snapshot=gnstore.DEFAULT_SNAPSHOT)
    if bg.get("exists"):
        try:
            doc = pdfium.PdfDocument(bg["path"])
            try:
                idx = min(max(1, int(bg.get("attachment_page") or 1)) - 1, len(doc) - 1)
                tp = doc[idx].get_textpage()
                try:
                    pdf_text = tp.get_text_range().strip()
                finally:
                    tp.close()
            finally:
                doc.close()
        except Exception:
            pdf_text = ""

    def section(title: str, text: str) -> str:
        return f"{title}\n{text.strip() if text and text.strip() else '(none)'}"

    ocr = hint.get("ocr", "")
    typed = hint.get("typed", "")
    return "\n\n".join([
        f"notebook: {_label(nb)} [{nb['id']}]",
        f"page: {page}/{len(live)}  page_id: {page_id}",
        "OCR WARNING: the Goodnotes handwriting OCR below is UNRELIABLE. "
        "It can be wrong, incomplete, stale, or empty. Verify with get_page_image.",
        section("Goodnotes handwriting OCR (UNRELIABLE):", ocr),
        section("Typed text boxes (from Goodnotes index; may be incomplete):", typed),
        section("Imported PDF text layer (not handwriting; may be absent):", pdf_text),
        "Note: this hint is for search/navigation only; it is not authoritative.",
    ])


@mcp.tool()
def get_page_text(notebook: str, page: int = 1) -> str:
    """Exact typed text (positions/styles) plus a clearly-labelled OCR hint.

    Uses the `gntext` extractor on the live page records. Returns
    JSON with `typed_items` (text, kind, bbox, z-order, transform/style fields
    when present), a per-item-type census, and `ocr_hint` from Goodnotes'
    MyScript index. The OCR hint is UNRELIABLE; the typed items are exact
    decoded values, not OCR. Use get_page_image to see the rendered page.

    Args:
        notebook: Notebook UUID, exact name, or unique part of the name.
        page: Page number, 1-based over live (non-deleted) pages.
    """
    _fresh_snapshot()
    nb = gnstore.get_notebook(_resolve(notebook)["id"],
                              snapshot=gnstore.DEFAULT_SNAPSHOT)
    live = _live_pages(nb)
    page = _check_page(page, len(live))
    page_id = live[page - 1]["page_id"]
    if gntext is None:
        return ("ERROR: exact typed-text extractor unavailable: "
                f"{GNTEXT_ERROR}. Use get_page_text_hint for the unreliable hint.")
    try:
        exact = gntext.extract_store_page(
            page_id, snapshot=str(gnstore.DEFAULT_SNAPSHOT), include_nexus=True)
    except Exception as exc:
        return (f"ERROR: exact typed-text extraction failed for {page_id}: "
                f"{type(exc).__name__}: {exc}. Use get_page_text_hint instead.")
    hint = gnlive.page_text_hint(page_id)
    payload = {
        "notebook": _label(nb),
        "notebook_id": nb["id"],
        "page": page,
        "page_count": len(live),
        "page_id": page_id,
        "source": "gntext exact typed-text extraction",
        "counts": exact.get("counts", {}),
        "typed_items": exact.get("items", []),
        "ocr_hint": {
            "reliable": False,
            "text": hint.get("ocr", ""),
            "note": ("Goodnotes MyScript OCR is UNRELIABLE: it may be wrong, "
                     "incomplete, stale or empty. Typed items above are exact."),
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _capabilities() -> dict:
    caps = gnreader.capabilities()
    return {"rows": caps.get("item_kinds", {}),
            "supported": caps.get("supported", []),
            "partial": caps.get("partial", []),
            "unsupported": caps.get("unsupported", [])}


@mcp.tool()
def server_status() -> str:
    """Report snapshot freshness, library counts, versions and real capabilities.

    Versions are read dynamically from the imported packages. The capability
    list comes from ``gnreader.capabilities()``; unsupported item types are
    listed explicitly so an AI does not over-trust a page image. Image labels
    also carry per-page warnings for items/effects that were skipped.
    """
    nbs = _all_notebooks()
    pages = sum(nb.get("page_count", 0) for nb in nbs)
    folders = len({(nb.get("folder_path") or "") for nb in nbs})
    age = _snapshot_age_s()
    data_age = _snapshot_data_age_s()
    try:
        mcp_ver = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        mcp_ver = "unknown"
    files = _cache_files()
    cache_bytes = sum(p.stat().st_size for p in files if p.exists())
    caps = _capabilities()
    reader_ver = getattr(gnreader, "__version__", "unknown")
    lines = [
        "goodnotes-inkeye MCP server",
        f"server_version: {SERVER_VERSION}",
        f"python: {platform.python_version()}",
        f"mcp: {mcp_ver}",
        f"gnstore: {gnstore.__version__}",
        f"gnlive: {gnlive.__version__}",
        f"gnreader: {reader_ver}",
        f"gntext: {'present' if gntext is not None else 'unavailable: ' + GNTEXT_ERROR}",
        "",
        "source: live Goodnotes store (read-only)",
        f"snapshot: {gnstore.DEFAULT_SNAPSHOT}",
        (f"snapshot_age_s: {age:.1f} (last freshness check)" if age is not None
         else "snapshot_age_s: unknown"),
        (f"snapshot_data_age_s: {data_age:.1f} (last actual copy)" if data_age is not None
         else "snapshot_data_age_s: unknown"),
        f"library: {len(nbs)} notebooks, {pages} live pages, {folders} folder paths",
        f"cache: {cache_bytes / (1024 * 1024):.2f} MiB in {len(files)} files "
        f"(cap {CACHE_LIMIT_BYTES // (1024 * 1024)} MiB)",
        "capabilities_source: gnreader.capabilities() (packaged capabilities.json)",
        "",
        "supported:",
        *[f"- {x}" for x in caps["supported"]],
        "partial / extract-only:",
        *[f"- {x}" for x in caps["partial"]],
        "not supported yet:",
        *[f"- {x}" for x in caps["unsupported"]],
        "",
        "ocr: Goodnotes MyScript OCR is unreliable and is only a hint; use "
        "get_page_text for exact typed items and get_page_image to verify ink",
        "render warnings: get_page_image/get_pages labels list skipped items/effects",
        "warnings_source: gnreader open_records().unknown_items",
        "backup PDF server: separate; this server does not read the auto-backup PDFs",
    ]
    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
