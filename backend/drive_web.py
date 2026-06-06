"""Browse the company drive in a web browser — a zero-dependency viewer.

The shared drive lives in SQLite (backend/store.py `files` table). The in-game
panel (game/drive_panel.py) shows it inside the office; this serves the same
drive over HTTP so you can open it in a normal browser, follow folders, read
text files, and view generated images/videos full-size.

Built on Python's stdlib `http.server` — no Flask/FastAPI, matching the store's
"stdlib only, no extra dependency" ethos. It is READ-ONLY and binds to localhost
by default. The /raw endpoint only serves a disk path that is actually registered
in the drive (looked up by virtual path), so it can't be used to read arbitrary
files off the machine.

CLI:
    python -m backend.drive_web                 # serve at http://127.0.0.1:8787
    python -m backend.drive_web --port 9000
    python -m backend.drive_web --host 0.0.0.0  # expose on the LAN (use with care)
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import AgentStore

DEFAULT_PORT = 8787

# Single-page browse UI. Plain HTML/CSS/JS, no build step — it just fetches the
# JSON endpoints below. Teal accents to echo the in-game drive panel.
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Company Drive</title>
<style>
  :root { --bg:#12161e; --panel:#1a2030; --row:#222838; --rowsel:#2c463f;
          --accent:#36c8b9; --text:#dbe6ef; --muted:#8a96aa; --head:#f5d678; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 system-ui,sans-serif; background:var(--bg); color:var(--text); }
  header { background:#368c82; padding:12px 18px; font-size:18px; font-weight:600;
           display:flex; align-items:center; gap:12px; }
  header .count { font-size:13px; font-weight:400; color:#dff5f0; }
  header button { margin-left:auto; background:#2c6b63; color:#fff; border:0;
           padding:6px 14px; border-radius:6px; cursor:pointer; }
  #wrap { display:flex; height:calc(100vh - 50px); }
  #side { width:340px; flex:none; border-right:1px solid #2a3346; overflow:auto; padding:10px; }
  #search { width:100%; padding:8px; margin-bottom:8px; border-radius:6px;
            border:1px solid #2a3346; background:var(--panel); color:var(--text); }
  .file { padding:7px 9px; border-radius:6px; cursor:pointer; margin-bottom:3px;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .file:hover { background:var(--row); }
  .file.sel { background:var(--rowsel); border-left:3px solid var(--accent); padding-left:6px; }
  .file .tag { color:var(--muted); font-size:11px; margin-right:6px; }
  #main { flex:1; overflow:auto; padding:22px 26px; }
  #path { color:var(--head); font-size:18px; word-break:break-all; }
  #meta { color:var(--muted); font-size:12px; margin:4px 0 18px; }
  pre { background:var(--panel); padding:16px; border-radius:8px; white-space:pre-wrap;
        word-break:break-word; }
  img,video { max-width:100%; border-radius:8px; background:#000; }
  audio { width:100%; }
  iframe.pdf, iframe.site { width:100%; height:calc(100vh - 230px); border:0;
        border-radius:8px; background:#fff; }
  a.dl { display:inline-block; margin-top:10px; color:var(--accent); }
  .bar { display:flex; gap:10px; margin-bottom:10px; }
  .btn { background:var(--accent); color:#0b1a18; border:0; padding:7px 14px;
        border-radius:6px; cursor:pointer; font:600 13px system-ui; text-decoration:none; }
  .btn.alt { background:var(--row); color:var(--text); }
  .empty { color:var(--muted); margin-top:40px; }
</style></head>
<body>
<header>Company Drive <span class="count" id="count"></span>
  <button onclick="load()">Refresh</button></header>
<div id="wrap">
  <div id="side">
    <input id="search" placeholder="Filter files…" oninput="render()">
    <div id="list"></div>
  </div>
  <div id="main"><div class="empty">Select a file to view it.</div></div>
</div>
<script>
let FILES = [], SEL = null;
async function load() {
  const r = await fetch('api/files'); FILES = await r.json();
  document.getElementById('count').textContent = FILES.length + ' file(s)';
  render();
}
function render() {
  const q = document.getElementById('search').value.toLowerCase();
  const list = document.getElementById('list'); list.innerHTML = '';
  FILES.filter(f => f.path.toLowerCase().includes(q)).forEach(f => {
    const d = document.createElement('div');
    d.className = 'file' + (f.path === SEL ? ' sel' : '');
    const tag = {image:'IMG', video:'VID', audio:'AUD', pdf:'PDF',
                 webapp:'APP', link:'APP'}[f.kind] || 'TXT';
    d.innerHTML = '<span class="tag">'+tag+'</span>' + esc(f.path);
    d.onclick = () => view(f.path);
    list.appendChild(d);
  });
}
function esc(s){ return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function toggleSrc(){
  const fr = document.getElementById('frame'), src = document.getElementById('src');
  const showing = src.style.display === 'none';
  src.style.display = showing ? 'block' : 'none';
  fr.style.display = showing ? 'none' : 'block';
}
async function view(path) {
  SEL = path; render();
  const r = await fetch('api/file?path=' + encodeURIComponent(path));
  const f = await r.json();
  const main = document.getElementById('main');
  const raw = 'raw?path=' + encodeURIComponent(path);
  const site = 'site' + path;                       // path begins with '/'
  const dl = '<a class="dl" href="'+raw+'" download>Download</a>';
  const isHtml = /\\.html?$/i.test(path);
  let body;
  if (f.kind === 'webapp' || f.kind === 'link') {
    // A live URL (deployed site, Daytona preview, Google Doc…) — embed it live.
    const url = f.content || '';
    body = '<div class="bar"><a class="btn" href="'+url+'" target="_blank">↗ Open app</a>'
      + '<span style="color:var(--muted);align-self:center">'+esc(url)+'</span></div>'
      + '<iframe class="site" src="'+url+'"></iframe>';
  }
  else if (isHtml) {
    // Render the actual page (relative assets resolve under /site/<folder>/…),
    // with a toggle back to the source and a full-tab open.
    body = '<div class="bar"><a class="btn" href="'+site+'" target="_blank">↗ Open in new tab</a>'
      + '<button class="btn alt" onclick="toggleSrc()">View source</button></div>'
      + '<iframe class="site" id="frame" src="'+site+'"></iframe>'
      + '<pre id="src" style="display:none">'+esc(f.content||'')+'</pre>';
  }
  else if (f.kind === 'image') body = '<img src="'+raw+'">';
  else if (f.kind === 'video') body = '<video src="'+raw+'" controls></video>';
  else if (f.kind === 'audio') body = '<audio src="'+raw+'" controls></audio>' + dl;
  else if (f.kind === 'pdf') body = '<iframe class="pdf" src="'+raw+'"></iframe>' + dl;
  else if (f.content !== null && f.content !== undefined) body = '<pre>'+esc(f.content)+'</pre>';
  else body = '<p>'+f.kind+' file — your browser can\\'t render this inline.</p>' + dl;
  main.innerHTML = '<div id="path">'+esc(f.path)+'</div>'
    + '<div id="meta">'+f.kind+' · '+f.size+(f.kind==='text'?' chars':' bytes')
    + ' · by '+esc(f.author_name)+' · updated '+f.updated_at+'</div>' + body;
}
load();
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    # `store` is injected onto the server instance in serve().
    def _store(self) -> AgentStore:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # quiet by default
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    @staticmethod
    def _row_meta(f) -> dict:
        return {"path": f.path, "name": f.name, "kind": f.kind, "size": f.size,
                "author_name": f.author_name, "updated_at": f.updated_at}

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        store = self._store()

        if route in ("/", "/index.html"):
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if route == "/api/files":
            self._json([self._row_meta(f) for f in store.fs_list()])
            return

        if route == "/api/file":
            f = store.fs_get((qs.get("path") or [""])[0])
            if f is None:
                self._json({"error": "not found"}, code=404)
                return
            meta = self._row_meta(f)
            meta["content"] = f.content       # None for binary assets
            self._json(meta)
            return

        if route == "/raw":
            # Serve a registered asset's bytes from disk. We resolve the disk path
            # ONLY via the drive record, so an arbitrary ?path= can't escape.
            f = store.fs_get((qs.get("path") or [""])[0])
            if f is None or not f.disk_path:
                self._send(404, b"not found", "text/plain")
                return
            p = Path(f.disk_path)
            if not p.is_file():
                self._send(404, b"asset missing on disk", "text/plain")
                return
            ctype = f.mime or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            self._send(200, p.read_bytes(), ctype)
            return

        if route == "/site" or route.startswith("/site/"):
            # Serve a drive file as a real web resource so saved HTML *renders*
            # (and its relative <link>/<script>/<img> resolve to sibling files on
            # the drive under the same /site/<folder>/ prefix). Inline text files
            # are served as their own bytes; referenced assets stream from disk.
            f = store.fs_get(route[len("/site"):] or "/")
            if f is None:
                self._send(404, b"not found", "text/plain")
                return
            ctype = f.mime or mimetypes.guess_type(f.name)[0] or "text/html"
            if f.content is not None:
                if ctype.startswith("text/") and "charset" not in ctype:
                    ctype += "; charset=utf-8"
                self._send(200, f.content.encode("utf-8"), ctype)
            elif f.disk_path and Path(f.disk_path).is_file():
                self._send(200, Path(f.disk_path).read_bytes(), ctype)
            else:
                self._send(404, b"asset missing", "text/plain")
            return

        self._send(404, b"not found", "text/plain")


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          db_path: str | None = None) -> None:
    """Start the drive viewer and block until interrupted."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.store = AgentStore(db_path) if db_path else AgentStore()  # type: ignore[attr-defined]
    shown = "127.0.0.1" if host in ("", "0.0.0.0") else host
    print(f"Company Drive → http://{shown}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Browse the company drive in a browser.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default localhost; 0.0.0.0 exposes on LAN)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=None, help="path to company.db (default: project db)")
    args = ap.parse_args(argv[1:])
    serve(host=args.host, port=args.port, db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
