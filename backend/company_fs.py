"""The company file system — a shared, persistent drive every agent can use.

Most agent tools reach OUTWARD (Opsera, Apify, Daytona, Composio) or act on the
company roster (HR). These act on the company's own *artifacts*: a single virtual
drive, backed by the `files` table in the same SQLite store (backend/store.py),
that survives restarts and is shared by everyone. An Engineer can drop a spec at
`/specs/api.md`, a Designer registers the logo it generated, and the Marketer
reads both — the work finally accumulates instead of vanishing with each run.

Like HR's tools, these open the store directly (it's one local file at a fixed
path), so they stay stateless LangChain tools with nothing to thread through.
They're namespaced `drive_*` so they never collide with the exec layer's
`read_file`/`write_file` when an agent happens to have both.

Safety: unlike the exec layer there is NO host access here — every path is a
virtual key inside the DB, so these are always-on for every agent, profiled or
not. The worst an agent can do is overwrite a teammate's file (recoverable: the
drive keeps created_at, and a move/delete is explicit).
"""
from __future__ import annotations

import mimetypes
import sys
from pathlib import Path

from .store import AgentStore, FileRow

# Per-tool output caps so one giant file (or a huge listing) can't flood the
# model's context window. Reads past the cap are truncated with a marker.
_READ_CAP = 20_000
_LIST_CAP = 300
# Largest text file we inline when attaching from disk; bigger ones are linked by
# reference instead so the DB row stays light.
_ATTACH_TEXT_CAP = 200_000

# Extension → file "kind". Kind drives how each viewer renders a file: text is
# shown inline, image/video/audio get players, pdf embeds in the browser, and
# document/binary fall back to download / open-in-OS. Anything unknown is binary.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".ico", ".tiff"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi", ".wmv"}
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
_DOC_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
             ".odp", ".rtf", ".pages", ".numbers", ".key", ".epub"}
_TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
              ".xml", ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py",
              ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb",
              ".php", ".sh", ".bash", ".zsh", ".sql", ".ini", ".toml", ".cfg",
              ".conf", ".log"}
# A few MIME overrides where the stdlib guess is missing or unhelpful.
_MIME = {".md": "text/markdown", ".svg": "image/svg+xml", ".webp": "image/webp",
         ".m4v": "video/x-m4v", ".mkv": "video/x-matroska"}


def classify(name: str) -> tuple[str, str | None]:
    """Map a filename to (kind, mime). Kind is one of: text, image, video, audio,
    pdf, document, binary — the vocabulary every drive viewer renders against."""
    ext = Path(name).suffix.lower()
    if ext in _IMAGE_EXTS:
        kind = "image"
    elif ext in _VIDEO_EXTS:
        kind = "video"
    elif ext in _AUDIO_EXTS:
        kind = "audio"
    elif ext == ".pdf":
        kind = "pdf"
    elif ext in _DOC_EXTS:
        kind = "document"
    elif ext in _TEXT_EXTS:
        kind = "text"
    else:
        kind = "binary"
    mime = _MIME.get(ext) or mimetypes.guess_type(name)[0]
    return kind, mime


def _slugify(text: str, n: int = 40) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
    return "-".join(filter(None, keep.split("-")))[:n]


def register_link(url: str | None, dest: str, author_id: str | None = None,
                  author_name: str = "", kind: str = "webapp") -> str | None:
    """Pin a LIVE URL into the drive as a previewable entry, returning its virtual
    path (or None on failure). The URL lives in the row's `content`; kind 'webapp'
    tells every viewer to embed it in an iframe — this is what makes a deployed
    site (Vercel), a Daytona preview, or a Google Doc show up as a live, rendered
    preview in the company drive instead of a bare link buried in a chat reply.
    """
    if not url or not dest:
        return None
    try:
        f = AgentStore().fs_write(dest, url, author_id=author_id,
                                  author_name=author_name or "CEO", kind=kind)
    except Exception:
        return None
    return f.path


def register_asset(disk_path: str | None, author_id: str | None = None,
                   author_name: str = "") -> str | None:
    """Catalogue a generated on-disk asset (image/video/audio) into the company
    drive by reference, returning its virtual path (or None if nothing to add).

    This is what makes generated media show up on the shared drive instead of
    only as a loose file path in one agent's reply. It files under
    /generated/<images|videos|audio> (else /generated), keyed by the asset's own
    filename (already timestamped + slugged, so collisions are rare). Best-effort:
    any failure returns None rather than disrupting generation.
    """
    if not disk_path:
        return None
    p = Path(disk_path)
    kind, mime = classify(p.name)
    folder = {"image": "/generated/images", "video": "/generated/videos",
              "audio": "/generated/audio"}.get(kind, "/generated")
    vpath = f"{folder}/{p.name}"
    try:
        AgentStore().fs_attach(vpath, str(p), kind=kind, author_id=author_id,
                               author_name=author_name or "CEO", mime=mime)
    except Exception:
        return None
    return vpath


def _badge(f: FileRow) -> str:
    """One-line directory-listing row for a file."""
    tag = f.kind if f.kind != "text" else f"{f.size}c"
    return f"  {f.path}  [{tag}] by {f.author_name} · updated {f.updated_at}"


def local_disk_path(store, path: str = "") -> str | None:
    """Resolve a company-drive virtual path to a REAL on-disk path, or None.

    Text files are mirrored by fs_write to workspaces/<slug>/drive/<path>; binary
    assets carry their own `disk_path`. A folder resolves to its index.html when
    present (so a built site opens rendered); '' resolves to the drive root folder.
    """
    import os
    drive_root = os.path.join(os.path.dirname(store.db_path), "drive")
    norm = (path or "").strip().lstrip("/")
    disk = os.path.join(drive_root, norm) if norm else drive_root
    if os.path.isdir(disk):
        idx = os.path.join(disk, "index.html")
        if os.path.exists(idx):
            disk = idx
    if os.path.exists(disk):
        return os.path.abspath(disk)
    row = store.fs_get("/" + norm) if norm else None
    dp = getattr(row, "disk_path", None) if row else None
    return os.path.abspath(dp) if dp and os.path.exists(dp) else None


def load_fs_tools(author_id: str | None = None, author_name: str = "CEO") -> list:
    """LangChain `drive_*` tools bound to one author (the calling agent).

    `author_id`/`author_name` stamp who wrote each file so the drive shows
    provenance. Pass the agent's id + name; the CEO/human writes default to 'CEO'.
    """
    from langchain_core.tools import tool

    store = AgentStore()

    @tool
    def drive_write(path: str, content: str) -> str:
        """Save a text file to the SHARED company drive at a virtual path like
        '/specs/api.md' or '/marketing/launch-brief.md'. Use this to record any
        artifact your teammates or the CEO may need later — specs, drafts, notes,
        code, reports, plans. Writing an existing path overwrites it. Organize
        with folders (slashes). This drive persists across sessions and is visible
        to every agent, so prefer it over burying results only in your reply."""
        try:
            f = store.fs_write(path, content, author_id=author_id,
                               author_name=author_name)
        except ValueError as exc:
            return f"[error: {exc}]"
        return f"Saved {f.path} ({f.size} chars) to the company drive."

    @tool
    def drive_read(path: str) -> str:
        """Read a text file from the shared company drive by its virtual path.
        Use this to pick up specs, briefs, or data a teammate left for you before
        you start your own task."""
        f = store.fs_get(path)
        if f is None:
            return (f"No file at {path!r}. Use drive_list or drive_search to find "
                    f"the right path.")
        if f.content is None:
            where = f.disk_path or "(no on-disk path)"
            return (f"{f.path} is a {f.kind} asset, not text — stored on disk at "
                    f"{where}. Reference it by path; there's no text to read.")
        body = f.content
        if len(body) > _READ_CAP:
            body = body[:_READ_CAP] + f"\n…[truncated {len(body) - _READ_CAP} chars]"
        return f"# {f.path}  (by {f.author_name}, updated {f.updated_at})\n{body}"

    @tool
    def drive_list(folder: str = "") -> str:
        """List files on the shared company drive. Pass a folder like '/specs' to
        see just that directory, or leave it blank to see the whole drive. Call
        this to discover what work already exists before duplicating it."""
        files = store.fs_list(folder=folder or None)
        if not files:
            where = f"in {folder!r}" if folder else "on the company drive"
            return f"No files {where} yet."
        shown = files[:_LIST_CAP]
        head = (f"{len(files)} file(s)"
                + (f" in {AgentStore._norm_path(folder)}" if folder else
                   " on the company drive") + ":")
        lines = [head] + [_badge(f) for f in shown]
        if len(files) > _LIST_CAP:
            lines.append(f"  …[{len(files) - _LIST_CAP} more]")
        return "\n".join(lines)

    @tool
    def drive_search(query: str) -> str:
        """Search the shared company drive by a keyword found in a file's path or
        its text content. Use this when you know roughly what you need but not the
        exact path."""
        hits = store.fs_search(query)
        if not hits:
            return f"No files match {query!r}."
        return (f"{len(hits)} match(es) for {query!r}:\n"
                + "\n".join(_badge(f) for f in hits))

    @tool
    def drive_attach(disk_path: str, dest_path: str = "") -> str:
        """File a document or asset you produced on disk into the shared company
        drive — a PDF, CSV, spreadsheet, dataset, archive, audio clip, etc. Use
        this for anything you built with code/exec or saved locally that the CEO
        or teammates should be able to view or download (drive_write is only for
        text you author directly). `dest_path` is where to file it, e.g.
        '/reports/q3.pdf'; leave blank to keep the name under /uploads. Text-like
        files (.csv/.json/.md/code/…) are stored inline so they're readable
        everywhere; PDFs, Office docs, media, and other binaries are linked by
        reference (viewable in the browser drive, downloadable anywhere)."""
        p = Path(disk_path)
        if not p.is_file():
            return f"[no file at {disk_path!r} on disk to attach]"
        dest = dest_path.strip() or f"/uploads/{p.name}"
        kind, mime = classify(p.name)
        if kind == "text":
            try:
                text = p.read_text(errors="replace")
            except Exception as exc:
                return f"[could not read {disk_path}: {exc}]"
            if len(text) <= _ATTACH_TEXT_CAP:
                f = store.fs_write(dest, text, author_id=author_id,
                                   author_name=author_name, kind="text", mime=mime)
                return f"Filed {f.path} ({f.size} chars) on the company drive."
            # Too big to inline — keep it light and link by reference instead.
            kind = "binary"
        try:
            f = store.fs_attach(dest, str(p), kind=kind, author_id=author_id,
                                author_name=author_name, mime=mime)
        except ValueError as exc:
            return f"[error: {exc}]"
        return f"Filed {f.path} ({kind}, {f.size} bytes) on the company drive."

    @tool
    def drive_link(url: str, name: str = "", dest_path: str = "") -> str:
        """Pin a LIVE URL into the company drive so the CEO can preview and open it
        — a web app or site you deployed (a Vercel link, a Daytona preview URL), a
        Google Doc/Sheet you created, a dashboard, any working link. It appears in
        the drive as a previewable app, embedded live in the browser drive (just
        like Lovable). Use this whenever you produce something with a URL so it
        doesn't get lost in chat. `name` labels it; `dest_path` (e.g. '/apps/landing')
        sets where it's filed — default derives one from the name or URL."""
        u = (url or "").strip()
        if not u.startswith(("http://", "https://")):
            return f"[need a full http(s) URL to pin; got {url!r}]"
        dest = dest_path.strip() or ("/apps/" + (_slugify(name) or _slugify(u) or "app"))
        f = store.fs_write(dest, u, author_id=author_id, author_name=author_name,
                           kind="webapp")
        return (f"Pinned a live app at {f.path} → {u}. The CEO can preview it in "
                f"the company drive.")

    @tool
    def drive_delete(path: str) -> str:
        """Delete a file from the shared company drive. This is permanent and
        affects everyone, so only remove files you're sure are obsolete."""
        ok = store.fs_delete(path)
        return (f"Deleted {AgentStore._norm_path(path)}." if ok
                else f"No file at {path!r} to delete.")

    return [drive_write, drive_read, drive_list, drive_search, drive_attach,
            drive_link, drive_delete]


# --- CLI: browse the drive from the terminal --------------------------------

def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _print_tree(store: AgentStore) -> None:
    files = store.fs_list()
    if not files:
        print("The company drive is empty.")
        return
    print(f"Company drive — {len(files)} file(s):")
    for f in files:
        size = f.kind if f.kind != "text" else f"{f.size}c"
        print(f"  {f.path:<40} [{size:>6}]  {f.author_name:<14} {f.updated_at}")


def main(argv: list[str]) -> int:
    _load_env()
    store = AgentStore()
    if len(argv) == 1 or argv[1] in {"list", "ls", "tree"}:
        _print_tree(store)
        return 0
    cmd = argv[1]
    if cmd == "read" and len(argv) >= 3:
        f = store.fs_get(argv[2])
        if f is None:
            print(f"No file at {argv[2]!r}")
            return 1
        if f.content is None:
            print(f"[{f.kind} asset on disk: {f.disk_path}]")
            return 0
        print(f.content)
        return 0
    if cmd == "search" and len(argv) >= 3:
        for f in store.fs_search(argv[2]):
            print(f"  {f.path}  (by {f.author_name})")
        return 0
    if cmd == "write" and len(argv) >= 4:
        f = store.fs_write(argv[2], argv[3])
        print(f"Saved {f.path} ({f.size} chars).")
        return 0
    if cmd == "rm" and len(argv) >= 3:
        print("Deleted." if store.fs_delete(argv[2]) else "Not found.")
        return 0
    print("Usage: python -m backend.company_fs [list | read <path> | "
          "search <q> | write <path> <text> | rm <path>]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
