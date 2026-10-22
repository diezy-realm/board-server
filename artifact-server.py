#!/usr/bin/env python3
"""Artifact server + manager for Tailscale serve.

Read: index (folder tree, date-desc, filters) + render md->HTML + print-to-PDF.
Write (synced to the native artifact store on disk):
  - move artifact to a folder/subfolder
  - edit artifact content (versioned)
  - create new file (artifact) / new folder, at a chosen path
  - delete artifact (soft-delete to .trash)
  - import uploaded .md/.html/.json file
Artifacts are flat files (<id>/meta.json + current.html + versions/) plus
artifact_folders.json -- there is NO separate DB/cache, so writing the same
schema IS the sync. Bind 127.0.0.1; expose via `tailscale serve`.

ponytail: markdown renderer is a minimal regex pass (known ceiling, not
CommonMark). Write ops guard-railed: soft-delete, versioned edits, folder-json
backup before mutate.
"""
import html as _html
import json
import os
import re
import secrets
import shutil
import socketserver
import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, quote, parse_qs

HOST = "127.0.0.1"
PORT = int(os.environ.get("ARTIFACT_PORT", "8787"))
HOME = Path.home()
CREW = HOME / ".kiro" / "crew"
ART_DIR = CREW / "artifacts"
DOCS_DIR = CREW / "published" / "docs"
FOLDERS_JSON = CREW / "artifact_folders.json"
TRASH = ART_DIR / ".trash"
BOARD_DIR = HOME / "Apps" / "board"  # Scrum board SSOT (git-local, not pushed); read-only view here

import subprocess as _sp

def bridge_health():
    """Health of the board->agy bridge (systemd timer) + board task tallies.
    Read-only: queries `systemctl --user` + `journalctl --user`, no mutation."""
    def _sc(*args):
        try:
            return _sp.run(["systemctl", "--user", *args], capture_output=True,
                           text=True, timeout=5).stdout.strip()
        except Exception:
            return ""
    timers = {u: (_sc("is-active", f"{u}.timer") or "unknown") for u in TIMERS}
    active = "active" if all(v == "active" for v in timers.values()) else \
        ", ".join(f"{u}={v}" for u, v in timers.items() if v != "active")
    next_trigger = ""
    try:
        lt = _sp.run(["systemctl", "--user", "list-timers", "agy-bridge.timer",
                      "--no-pager", "--all"], capture_output=True, text=True, timeout=5).stdout
        for ln in lt.splitlines():
            if "agy-bridge.timer" in ln:
                next_trigger = " ".join(ln.split()[:3])  # NEXT datetime columns
                break
    except Exception:
        pass
    last_line, last_when = "", ""
    try:
        j = _sp.run(["journalctl", "--user", "-u", "agy-bridge.service",
                     "--no-pager", "-n", "4", "-o", "short-iso"],
                    capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        for ln in reversed(j):
            if "env[" in ln or "scan board" in ln or "selesai" in ln or "blocked" in ln:
                parts = ln.split(" ", 1)
                last_when = parts[0] if parts else ""
                last_line = ln.split("]:", 1)[-1].strip() if "]:" in ln else ln
                break
    except Exception:
        pass
    # task tallies for agy
    todo = inprog = blocked = done = review = 0
    if BOARD_DIR.is_dir():
        for f in BOARD_DIR.rglob("*.json"):
            if f.parent.name != "tasks" or "archive" in f.relative_to(BOARD_DIR).parts:
                continue
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if t.get("assignee") != "agy":
                continue
            st = t.get("status")
            if st == "todo": todo += 1
            elif st == "in-progress": inprog += 1
            elif st == "blocked": blocked += 1
            elif st == "review": review += 1
            elif st == "done": done += 1
    healthy = (active == "active")
    return {
        "healthy": healthy, "timer_active": active, "next_trigger": next_trigger,
        "last_when": last_when, "last_line": last_line[:200],
        "timers": timers,
        "tasks": {"todo": todo, "in_progress": inprog, "blocked": blocked, "review": review, "done": done},
    }

import sqlite3 as _sqlite
BRIDGE_DB = HOME / "Apps" / "board" / "tools" / "agy-bridge" / "bridge_events.db"
# Timers editable from the board UI (unit name -> label). Whitelist: the unit name is user input.
TIMERS = {
    "agy-bridge": "agy run (todo)",
    "board-unblock": "blocked→todo",
    "board-review": "review→todo/done",
}
def _timer_file(unit):
    return HOME / ".config" / "systemd" / "user" / f"{unit}.timer"

def bridge_events(limit=30):
    """Recent completion/retry/unblock events for the bell dropdown (newest first)."""
    out = []
    if not BRIDGE_DB.exists():
        return out
    try:
        c = _sqlite.connect(f"file:{BRIDGE_DB}?mode=ro", uri=True, timeout=5)
        for r in c.execute("SELECT ts,kind,task_id,title,report,detail FROM events "
                            "ORDER BY id DESC LIMIT ?", (limit,)):
            out.append({"ts": r[0], "kind": r[1], "task_id": r[2],
                        "title": r[3], "report": r[4], "detail": r[5]})
        c.close()
    except Exception:
        pass
    return out

def bridge_interval_get(unit="agy-bridge"):
    """Current timer interval in minutes, parsed from the .timer unit."""
    if unit not in TIMERS:
        return 5
    try:
        txt = _timer_file(unit).read_text(encoding="utf-8")
        m = re.search(r"OnUnitActiveSec\s*=\s*(\d+)\s*(min|m|s)?", txt)
        if m:
            val = int(m.group(1)); unit = (m.group(2) or "s")
            return val if unit in ("min", "m") else max(1, val // 60)
    except Exception:
        pass
    return 5

def bridge_interval_set(minutes, unit="agy-bridge"):
    """Rewrite OnUnitActiveSec in the .timer unit (1..1440 min) and reload systemd.
    Returns (ok, msg)."""
    if unit not in TIMERS:
        return False, "timer tidak dikenal"
    try:
        minutes = int(minutes)
    except Exception:
        return False, "interval tidak valid"
    if minutes < 1 or minutes > 1440:
        return False, "interval harus antara 1 menit dan 1 hari (1440 menit)"
    try:
        timer_file = _timer_file(unit)
        txt = timer_file.read_text(encoding="utf-8")
        new = re.sub(r"OnUnitActiveSec\s*=.*", f"OnUnitActiveSec={minutes}min", txt)
        if "OnUnitActiveSec" not in new:
            new = new.replace("[Timer]", f"[Timer]\nOnUnitActiveSec={minutes}min", 1)
        timer_file.write_text(new, encoding="utf-8")
        _sp.run(["systemctl", "--user", "daemon-reload"], timeout=10)
        _sp.run(["systemctl", "--user", "restart", f"{unit}.timer"], timeout=10)
        return True, f"{TIMERS[unit]}: interval diset ke {minutes} menit"
    except Exception as e:
        return False, f"gagal set interval: {e}"

def board_report_html(rpt_id):
    """Render a board report (~/Apps/board/**/reports/RPT-NNN.md) to HTML. View-only."""
    if not re.match(r"^RPT-\d+$", rpt_id or ""):
        return None
    for f in BOARD_DIR.rglob(f"{rpt_id}.md"):
        if f.parent.name == "reports":
            try:
                return md_to_html(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                return None
    return None

def board_task_retry(task_id):
    """Manual retry: flip a blocked agy task back to todo (resets backoff gate)."""
    if not re.match(r"^TASK-\d+$", task_id or ""):
        return False, "id task tidak valid"
    for f in BOARD_DIR.rglob("*.json"):
        if f.parent.name != "tasks":
            continue
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if t.get("id") != task_id:
            continue
        if t.get("status") not in ("blocked", "failed"):
            return False, f"{task_id} tidak blocked (status={t.get('status')})"
        t["status"] = "todo"; t["updated"] = now_iso()
        notes = (t.get("notes") or "")
        t["notes"] = (notes + f" | manual-retry {now_iso()[:16]}").strip(" |")
        f.write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, f"{task_id} -> todo (retry manual)"
    return False, "task tidak ditemukan"

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def new_id(n=16):
    return secrets.token_hex(n // 2)

# ---------- minimal markdown -> HTML ----------
def _inline(s):
    s = _html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" rel="noopener">\1</a>', s)
    return s

def md_to_html(md):
    lines = md.replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)
    in_fence, fence_buf, list_stack = False, [], []
    def close_lists():
        while list_stack:
            out.append(f"</{list_stack.pop()}>")
    while i < n:
        line = lines[i]
        if line.strip().startswith("```"):
            if not in_fence:
                close_lists(); in_fence = True; fence_buf = []
            else:
                out.append("<pre><code>" + _html.escape("\n".join(fence_buf)) + "</code></pre>"); in_fence = False
            i += 1; continue
        if in_fence:
            fence_buf.append(line); i += 1; continue
        stripped = line.strip()
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i+1]):
            close_lists()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in header) + "</tr></thead><tbody>")
            i += 2
            while i < n and "|" in lines[i]:
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>"); i += 1
            out.append("</tbody></table>"); continue
        if re.match(r"^\s*(---|\*\*\*|___)\s*$", line):
            close_lists(); out.append("<hr>"); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_lists(); lvl = len(m.group(1)); out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>"); i += 1; continue
        if stripped.startswith(">"):
            close_lists(); out.append("<blockquote>" + _inline(stripped[1:].strip()) + "</blockquote>"); i += 1; continue
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            if not list_stack or list_stack[-1] != "ul":
                close_lists(); out.append("<ul>"); list_stack.append("ul")
            out.append(f"<li>{_inline(m.group(2))}</li>"); i += 1; continue
        m = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
        if m:
            if not list_stack or list_stack[-1] != "ol":
                close_lists(); out.append("<ol>"); list_stack.append("ol")
            out.append(f"<li>{_inline(m.group(2))}</li>"); i += 1; continue
        if not stripped:
            close_lists(); i += 1; continue
        close_lists(); out.append(f"<p>{_inline(stripped)}</p>"); i += 1
    if in_fence:
        out.append("<pre><code>" + _html.escape("\n".join(fence_buf)) + "</code></pre>")
    close_lists()
    return "\n".join(out)

# ---------- styles ----------
PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  color:#1a1a1a;background:#f6f7f9;-webkit-text-size-adjust:100%}
.wrap{max-width:860px;margin:0 auto;padding:16px}
details>summary{list-style:none}details>summary::-webkit-details-marker{display:none}details>summary::marker{content:""}
header{position:sticky;top:0;background:#111827;color:#fff;padding:10px 14px;z-index:20;
  display:flex;align-items:center;gap:10px;flex-wrap:wrap}
header a{color:#93c5fd;text-decoration:none}
header .t{font-weight:600;font-size:16px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btn{display:inline-block;background:#2563eb;color:#fff;border:0;border-radius:8px;
  padding:8px 13px;font-size:14px;text-decoration:none;cursor:pointer}
.btn.g{background:#059669}.btn.r{background:#dc2626}.btn.n{background:#374151}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;margin:8px 0;
  box-shadow:0 1px 2px rgba(0,0,0,.04);display:flex;align-items:flex-start;gap:10px}
.card .body{flex:1;min-width:0}
.card a.title{font-size:16px;font-weight:600;color:#111827;text-decoration:none}
.card .acts{display:flex;gap:6px;flex-shrink:0}
.icon{border:1px solid #e5e7eb;background:#f9fafb;border-radius:7px;padding:5px 8px;font-size:13px;
  cursor:pointer;text-decoration:none;color:#374151;line-height:1}
.icon.r{color:#dc2626;border-color:#fecaca;background:#fef2f2}
.meta{color:#6b7280;font-size:13px;margin-top:4px}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;margin-right:6px}
.badge.md{background:#dbeafe;color:#1e40af}.badge.widget{background:#dcfce7;color:#166534}
.badge.doc{background:#fef3c7;color:#92400e}.badge.pin{background:#fce7f3;color:#9d174d}
.content{background:#fff;border-radius:12px;padding:20px;margin-top:12px}
.content h1{font-size:1.6em;border-bottom:2px solid #eee;padding-bottom:.3em}
.content h2{font-size:1.3em;border-bottom:1px solid #eee;padding-bottom:.2em}
.content code{background:#f3f4f6;padding:2px 5px;border-radius:4px;font-size:.9em}
.content pre{background:#1f2937;color:#e5e7eb;padding:14px;border-radius:8px;overflow-x:auto}
.content pre code{background:none;color:inherit;padding:0}
.content table{border-collapse:collapse;width:100%;display:block;overflow-x:auto;font-size:14px}
.content th,.content td{border:1px solid #e5e7eb;padding:7px 10px;text-align:left}.content th{background:#f9fafb}
.content blockquote{border-left:4px solid #93c5fd;margin:0;padding:4px 14px;color:#4b5563;background:#f8fafc}
.content img{max-width:100%}
.search{width:100%;padding:11px 14px;border:1px solid #d1d5db;border-radius:10px;font-size:15px;margin:8px 0 4px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 8px}
.filters select{flex:1;min-width:120px;padding:9px 10px;border:1px solid #d1d5db;border-radius:9px;font-size:14px;background:#fff;color:#111827}
.btn-reset{padding:9px 14px;border:1px solid #d1d5db;border-radius:9px;background:#f3f4f6;color:#374151;font-size:14px;cursor:pointer}
.treebar{display:flex;gap:8px;margin:0 0 10px;flex-wrap:wrap}
.folder{margin:6px 0}
.folder>.grp-body{margin-left:10px;border-left:2px solid #e5e7eb;padding-left:8px}
summary.grp-h{font-size:14px;color:#374151;margin:4px 0;padding:8px 12px;background:#eef2f7;border-radius:8px;
  display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
.grp-h .arrow{display:inline-block;width:0;height:0;border-left:6px solid #64748b;border-top:5px solid transparent;border-bottom:5px solid transparent;transition:transform .15s}
details[open]>summary.grp-h .arrow{transform:rotate(90deg)}
.grp-h .cnt{margin-left:auto;background:#2563eb;color:#fff;border-radius:999px;font-size:11px;padding:1px 8px}
.grp-h .facts{display:flex;gap:4px;margin-left:6px}
.empty{color:#6b7280;text-align:center;padding:40px}
.dd{position:relative;display:inline-block}
.dd-menu{display:none;position:absolute;right:0;top:110%;background:#fff;border:1px solid #e5e7eb;border-radius:9px;
  box-shadow:0 4px 14px rgba(0,0,0,.12);z-index:30;min-width:150px;overflow:hidden}
.dd-menu a{display:block;padding:10px 14px;color:#111827;text-decoration:none;font-size:14px}
.dd-menu a:hover{background:#f3f4f6}
.dd.open .dd-menu{display:block}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;align-items:center;justify-content:center;padding:16px}
.modal.open{display:flex}
.modal-box{background:#fff;border-radius:14px;max-width:720px;width:100%;max-height:90vh;overflow:auto;padding:18px}
.modal-box h3{margin:0 0 12px}
.modal-box label{display:block;font-size:13px;color:#374151;margin:10px 0 4px;font-weight:600}
.modal-box input,.modal-box select,.modal-box textarea{width:100%;padding:10px;border:1px solid #d1d5db;border-radius:9px;font-size:14px;font-family:inherit}
.modal-box textarea{min-height:300px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#111827;color:#fff;padding:10px 18px;border-radius:10px;z-index:200;display:none}
@media print{header,.noprint,.acts,.treebar,.filters,.search{display:none}.content{box-shadow:none;border:0;padding:0}body{background:#fff}}
"""

WIDGET_VARS = """
:root{--background:#0f172a;--bg:#0f172a;--surface:#1e293b;--surface-raised:#273449;--card:#1e293b;--card-bg:#1e293b;
--panel:#1e293b;--border:#334155;--border-color:#334155;--text:#e5e7eb;--text-primary:#f1f5f9;--text-secondary:#cbd5e1;
--text-muted:#94a3b8;--text-dim:#94a3b8;--muted:#94a3b8;--fg:#e5e7eb;--foreground:#e5e7eb;--primary:#60a5fa;--accent:#60a5fa;
--link:#93c5fd;--success:#4ade80;--warning:#fbbf24;--danger:#f87171;--error:#f87171;--info:#60a5fa;--code-bg:#0b1220;--radius:10px;}
html,body{background:var(--background);color:var(--text)}a{color:var(--link)}
"""

def shell(title, body, extra_head="", show_pdf=False, pdf_href=""):
    pdf = f'<a class="btn noprint" href="{pdf_href}">⬇ PDF</a>' if show_pdf else ""
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(title)}</title><style>{PAGE_CSS}</style>{extra_head}</head><body>
<header><a href="/">← Index</a><span class="t">{_html.escape(title)}</span>{pdf}</header>
<div class="wrap">{body}</div>
<div class="toast" id="toast"></div></body></html>"""

def wrap_widget(title, raw, aid=""):
    edit = f'&nbsp;&nbsp;<a href="/edit/artifact/{quote(aid)}" style="color:#fbbf24;text-decoration:none">✎ Edit</a>' if aid else ""
    bar = (f'<div style="position:sticky;top:0;z-index:9999;background:#111827;color:#fff;'
           f'padding:10px 16px;font:600 15px -apple-system,sans-serif">'
           f'<a href="/" style="color:#93c5fd;text-decoration:none">← Index</a>{edit}'
           f'&nbsp;&nbsp;<span style="opacity:.85">{_html.escape(title)}</span></div>')
    style = f"<style>{WIDGET_VARS}</style>"
    low = raw.lower()
    if "<head" in low:
        out = re.sub(r"(<head[^>]*>)", r"\1" + style, raw, count=1, flags=re.I)
    elif "<body" in low:
        out = re.sub(r"(<body[^>]*>)", r"\1" + style, raw, count=1, flags=re.I)
    else:
        out = style + raw
    if "<body" in out.lower():
        out = re.sub(r"(<body[^>]*>)", r"\1" + bar, out, count=1, flags=re.I)
    else:
        out = bar + out
    return out

# ---------- folders ----------
def read_folders():
    try:
        return json.loads(FOLDERS_JSON.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []

def write_folders(rows):
    if FOLDERS_JSON.exists():
        bak = FOLDERS_JSON.with_suffix(".json.bak")
        try: shutil.copy2(FOLDERS_JSON, bak)
        except Exception: pass
    FOLDERS_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

def folder_map():
    rows = read_folders()
    by_id = {r["id"]: r for r in rows if r.get("id")}
    def path_of(fid):
        parts, seen = [], set()
        while fid and fid in by_id and fid not in seen:
            seen.add(fid); parts.append(by_id[fid].get("name") or fid); fid = by_id[fid].get("parent_id") or ""
        return " / ".join(reversed(parts))
    # path -> id (for resolving a chosen folder path to an id)
    path2id = {}
    for r in rows:
        path2id[path_of(r["id"])] = r["id"]
    return by_id, path_of, path2id

def ensure_folder_path(path):
    """Resolve/create a '/'-joined folder path, return its folder id ('' for root)."""
    path = (path or "").strip()
    if not path or path == "(Tanpa Folder)":
        return ""
    rows = read_folders()
    by_id = {r["id"]: r for r in rows}
    def path_of(fid):
        parts, seen = [], set()
        while fid and fid in by_id and fid not in seen:
            seen.add(fid); parts.append(by_id[fid].get("name") or fid); fid = by_id[fid].get("parent_id") or ""
        return " / ".join(reversed(parts))
    name2 = {path_of(r["id"]): r["id"] for r in rows}
    parts = [p.strip() for p in path.split("/") if p.strip()]
    parent, cur = "", ""
    changed = False
    for i, p in enumerate(parts):
        cur = " / ".join(parts[:i+1])
        if cur in name2:
            parent = name2[cur]; continue
        fid = new_id(12)
        rows.append({"icon": "📁", "id": fid, "name": p, "order": len(rows), "parent_id": parent})
        by_id[fid] = rows[-1]; name2[cur] = fid; parent = fid; changed = True
    if changed:
        write_folders(rows)
    return parent

# ---------- artifacts ----------
def read_meta(aid):
    mp = ART_DIR / aid / "meta.json"
    if not mp.is_file():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None

def write_meta(aid, meta):
    (ART_DIR / aid / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

def load_artifacts():
    _, path_of, _ = folder_map()
    items = []
    if ART_DIR.is_dir():
        for d in sorted(ART_DIR.iterdir()):
            if d.name.startswith("."):
                continue
            m = read_meta(d.name)
            if m is None:
                continue
            fid = m.get("folder_id") or ""
            fpath = path_of(fid) if fid else ""
            items.append({
                "id": d.name, "name": m.get("name") or d.name,
                "kind": "md" if m.get("kind") in ("md", "markdown") else m.get("kind", "widget"),
                "created": m.get("created_at") or "", "date": (m.get("created_at") or "")[:10],
                "pinned": bool(m.get("pinned")), "tags": m.get("tags") or [], "src": "artifact",
                "folder": fpath or "(Tanpa Folder)",
            })
    if DOCS_DIR.is_dir():
        for f in sorted(DOCS_DIR.rglob("*")):
            if f.suffix.lower() in (".md", ".html", ".htm") and f.is_file():
                rel = f.relative_to(DOCS_DIR).as_posix()
                sub = str(f.parent.relative_to(DOCS_DIR)).replace(".", "")
                try:
                    mt = datetime.datetime.fromtimestamp(f.stat().st_mtime).isoformat()
                except Exception:
                    mt = ""
                items.append({"id": rel, "name": f.stem,
                    "kind": "md" if f.suffix.lower()==".md" else "widget",
                    "created": mt, "date": mt[:10], "pinned": False, "tags": [], "src": "doc",
                    "folder": ("docs / " + sub) if sub else "docs"})
    items.sort(key=lambda x: x["created"], reverse=True)
    items.sort(key=lambda x: x["pinned"], reverse=True)
    return items

def read_artifact(src, aid):
    if src == "artifact":
        d = ART_DIR / aid; cp = d / "current.html"
        if not cp.is_file(): return None
        m = read_meta(aid) or {}
        k = m.get("kind", "widget")
        return {"name": m.get("name") or aid, "kind": "md" if k in ("md","markdown") else k,
                "raw": cp.read_text(encoding="utf-8", errors="replace")}
    else:
        f = DOCS_DIR / aid
        if ".." in aid or not f.is_file(): return None
        return {"name": f.stem, "kind": "md" if f.suffix.lower()==".md" else "widget",
                "raw": f.read_text(encoding="utf-8", errors="replace")}

# ----- write ops (each returns (ok, msg)) -----
_ID_RE = re.compile(r"^[0-9a-zA-Z][0-9a-zA-Z._-]*$")
def _valid_id(aid):
    return bool(aid) and ".." not in aid and "/" not in aid and bool(_ID_RE.match(aid)) and (ART_DIR / aid).is_dir()

def op_create_artifact(name, kind, folder_path, content=""):
    aid = new_id(16)
    d = ART_DIR / aid; (d / "versions").mkdir(parents=True, exist_ok=True)
    (d / "current.html").write_text(content, encoding="utf-8")
    (d / "versions" / "v1.html").write_text(content, encoding="utf-8")
    fid = ensure_folder_path(folder_path)
    meta = {"auto_registered": False, "created_at": now_iso(), "description": "",
            "events": [{"by": "manager", "ts": now_iso(), "type": "created", "version": 1}],
            "folder_id": fid, "image": None, "kind": "markdown" if kind=="md" else kind,
            "kind_auto": False, "name": name or "Untitled", "pinned": False, "slug": aid,
            "source": "manual", "tags": [], "updated_at": now_iso(), "version": 1,
            "version_kinds": {"1": "markdown" if kind=="md" else kind}}
    write_meta(aid, meta)
    return True, aid

def op_create_folder(name, parent_path):
    fid = ensure_folder_path(((parent_path + " / ") if parent_path and parent_path!="(Tanpa Folder)" else "") + name)
    return True, fid

def op_move(aid, folder_path):
    if not _valid_id(aid): return False, "invalid id"
    m = read_meta(aid)
    if m is None: return False, "not found"
    m["folder_id"] = ensure_folder_path(folder_path)
    m["updated_at"] = now_iso()
    m.setdefault("events", []).append({"by":"manager","ts":now_iso(),"type":"moved","version":m.get("version",1)})
    write_meta(aid, m); return True, "moved"

def op_edit(aid, content):
    if not _valid_id(aid): return False, "invalid id"
    m = read_meta(aid)
    if m is None: return False, "not found"
    d = ART_DIR / aid; (d/"versions").mkdir(exist_ok=True)
    ver = int(m.get("version", 1)) + 1
    (d/"versions"/f"v{ver}.html").write_text(content, encoding="utf-8")
    (d/"current.html").write_text(content, encoding="utf-8")
    m["version"] = ver; m["updated_at"] = now_iso()
    vk = m.get("version_kinds") or {}; vk[str(ver)] = m.get("kind","widget"); m["version_kinds"]=vk
    m.setdefault("events", []).append({"by":"manager","ts":now_iso(),"type":"edited","version":ver})
    write_meta(aid, m); return True, f"saved v{ver}"

def op_delete(aid):
    if not _valid_id(aid): return False, "invalid id"
    d = ART_DIR / aid
    if not d.is_dir(): return False, "not found"
    TRASH.mkdir(exist_ok=True)
    dest = TRASH / f"{aid}.{int(datetime.datetime.now().timestamp())}"
    shutil.move(str(d), str(dest)); return True, "deleted (soft)"

def op_import(filename, data_bytes, folder_path):
    name = Path(filename).stem
    ext = Path(filename).suffix.lower()
    try:
        content = data_bytes.decode("utf-8")
    except Exception:
        return False, "file bukan teks (utf-8)"
    kind = "md" if ext == ".md" else ("widget" if ext in (".html",".htm") else "md")
    return op_create_artifact(name, kind, folder_path, content)

# ---------- index ----------
def _card_html(it):
    href = f'/view/{it["src"]}/{quote(it["id"])}'
    kind = it["kind"]
    badge_cls = "md" if kind=="md" else ("doc" if it["src"]=="doc" else "widget")
    label = "MD" if kind=="md" else ("DOC" if it["src"]=="doc" else "HTML")
    badges = f'<span class="badge {badge_cls}">{label}</span>'
    if it["pinned"]:
        badges += '<span class="badge pin">★</span>'
    meta = " · ".join(x for x in [it["date"], " ".join(it["tags"][:3])] if x)
    fmt = "md" if kind=="md" else ("doc" if it["src"]=="doc" else "html")
    sk = _html.escape((it["name"]+" "+" ".join(it["tags"])+" "+it["folder"]).lower())
    acts = ""
    if it["src"] == "artifact":
        acts = (f'<div class="acts noprint">'
                f'<a class="icon" href="/edit/artifact/{quote(it["id"])}" title="Edit">✎</a>'
                f'<a class="icon" href="#" title="Pindahkan" onclick="mv(\'{it["id"]}\');return false">↪</a>'
                f'<a class="icon r" href="#" title="Hapus" onclick="del(\'{it["id"]}\',\'{_html.escape(it["name"])}\');return false">🗑</a>'
                f'</div>')
    return (f'<div class="card" data-s="{sk}" data-date="{_html.escape(it["date"])}" data-fmt="{fmt}" data-folder="{_html.escape(it["folder"])}">'
            f'<div class="body"><a class="title" href="{href}">{_html.escape(it["name"])}</a>'
            f'<div class="meta">{badges}{_html.escape(meta)}</div></div>{acts}</div>')

def _folder_actions(path):
    p = _html.escape(path)
    return (f'<span class="dd" onclick="event.preventDefault();event.stopPropagation();this.classList.toggle(\'open\')">'
            f'<a class="icon" title="Buat">＋</a>'
            f'<span class="dd-menu">'
            f'<a href="#" onclick="newFile(\'{p}\');return false">📄 New File</a>'
            f'<a href="#" onclick="newFolder(\'{p}\');return false">📁 New Folder</a>'
            f'</span></span>')

def render_index():
    items = load_artifacts()
    def new_node(): return {"children": {}, "items": []}
    root, no_folder = new_node(), new_node()
    for it in items:
        if it["folder"] == "(Tanpa Folder)":
            no_folder["items"].append(it); continue
        node = root
        for p in [x.strip() for x in it["folder"].split("/") if x.strip()]:
            node = node["children"].setdefault(p, new_node())
        node["items"].append(it)
    def sub_count(nd): return len(nd["items"]) + sum(sub_count(c) for c in nd["children"].values())
    def render_node(name, node, path, depth):
        full = (path + " / " + name) if path else name
        inner = []
        for cn in sorted(node["children"].keys(), key=str.lower):
            inner.append(render_node(cn, node["children"][cn], full, depth+1))
        inner.extend(_card_html(it) for it in node["items"])
        return (f'<details class="folder" data-folder="{_html.escape(full)}" open>'
                f'<summary class="grp-h" style="margin-left:{depth*12}px">'
                f'<span class="arrow"></span>📁 {_html.escape(name)}'
                f'<span class="cnt">{sub_count(node)}</span>'
                f'<span class="facts noprint">{_folder_actions(full)}</span></summary>'
                f'<div class="grp-body">{"".join(inner)}</div></details>')
    sections = [render_node(n, root["children"][n], "", 0) for n in sorted(root["children"].keys(), key=str.lower)]
    if no_folder["items"]:
        sections.append(f'<details class="folder" data-folder="(Tanpa Folder)" open>'
            f'<summary class="grp-h"><span class="arrow"></span>📂 (Tanpa Folder)'
            f'<span class="cnt">{len(no_folder["items"])}</span>'
            f'<span class="facts noprint">{_folder_actions("(Tanpa Folder)")}</span></summary>'
            f'<div class="grp-body">{"".join(_card_html(it) for it in no_folder["items"])}</div></details>')
    all_dates = sorted({it["date"] for it in items if it["date"]}, reverse=True)
    all_folders = sorted({it["folder"] for it in items}, key=lambda n:(n=="(Tanpa Folder)", n.lower()))
    date_opts = '<option value="">Semua tanggal</option>'+"".join(f'<option value="{d}">{d}</option>' for d in all_dates)
    fmt_sel = "".join(f'<option value="{v}">{t}</option>' for v,t in
        [("","Semua format"),("md","Markdown"),("html","Widget/HTML"),("doc","Dokumen")])
    folder_sel = '<option value="">Semua folder</option>'+"".join(f'<option value="{_html.escape(f)}">{_html.escape(f)}</option>' for f in all_folders)
    # folder options for move/create modals (all known folder paths + root)
    _,_,path2id = folder_map()
    all_paths = [""]+sorted([p for p in path2id if p], key=str.lower)
    move_opts = "".join(f'<option value="{_html.escape(p)}">{_html.escape(p) if p else "(root / Tanpa Folder)"}</option>' for p in all_paths)

    body = f"""
<input class="search" id="q" placeholder="🔍 Cari ({len(items)} item)..." oninput="applyF()">
<div class="filters">
  <select id="fDate" onchange="applyF()">{date_opts}</select>
  <select id="fFmt" onchange="applyF()">{fmt_sel}</select>
  <select id="fFolder" onchange="applyF()">{folder_sel}</select>
  <button class="btn-reset noprint" onclick="resetF()">Reset</button>
</div>
<div class="treebar noprint">
  <a class="btn" href="/board" style="background:#7c3aed">📋 Board</a>
  <button class="btn-reset" onclick="setAll(true)">Expand semua</button>
  <button class="btn-reset" onclick="setAll(false)">Collapse semua</button>
  <button class="btn g" onclick="newFile('')">＋ New File</button>
  <button class="btn n" onclick="newFolder('')">📁 New Folder</button>
  <button class="btn n" onclick="openImport()">⬆ Import</button>
</div>
<div id="list">{''.join(sections) if sections else '<div class="empty">Belum ada artifact.</div>'}</div>
{render_notes_section()}
<div id="nmodal" class="modal"><div class="modal-box">
  <h3 id="nmt"></h3><div id="nmb"></div>
</div></div>

<div class="modal" id="modal"><div class="modal-box">
  <h3 id="mTitle"></h3><div id="mBody"></div>
  <div class="modal-actions">
    <button class="btn-reset" onclick="closeModal()">Batal</button>
    <button class="btn" id="mOk" onclick="submitModal()">OK</button>
  </div>
</div></div>

<script>
var MOVE_OPTS = `{move_opts}`;
function cardVisible(c,q,fd,ff,fo){{
  if(c.dataset.id)return c.dataset.s.indexOf(q)>-1;  // note card: search-only, date/fmt/folder filters don't apply (not an artifact)
  return c.dataset.s.indexOf(q)>-1 && (!fd||c.dataset.date===fd) && (!ff||c.dataset.fmt===ff)
    && (!fo||c.dataset.folder===fo||c.dataset.folder.indexOf(fo+' / ')===0);}}
function applyF(){{
  var q=q_.value.toLowerCase(),fd=fDate.value,ff=fFmt.value,fo=fFolder.value,filtering=q||fd||ff||fo;
  document.querySelectorAll('.card').forEach(function(c){{c.style.display=cardVisible(c,q,fd,ff,fo)?'':'none';}});
  document.querySelectorAll('details.folder').forEach(function(d){{
    var vis=Array.prototype.filter.call(d.querySelectorAll('.card'),function(c){{return c.style.display!=='none';}}).length;
    d.style.display=vis>0?'':'none';var cnt=d.querySelector('.cnt');if(cnt)cnt.textContent=vis;if(filtering)d.open=true;}});}}
function resetF(){{q_.value='';fDate.value='';fFmt.value='';fFolder.value='';applyF();}}
function setAll(o){{document.querySelectorAll('details.folder').forEach(function(d){{d.open=o;}});}}
var q_=document.getElementById('q'),fDate=document.getElementById('fDate'),fFmt=document.getElementById('fFmt'),fFolder=document.getElementById('fFolder');
var modal=document.getElementById('modal'),mTitle=document.getElementById('mTitle'),mBody=document.getElementById('mBody');
var _submit=null;
function openModal(t,html,fn){{mTitle.textContent=t;mBody.innerHTML=html;_submit=fn;modal.classList.add('open');}}
function closeModal(){{modal.classList.remove('open');_submit=null;}}
function submitModal(){{if(_submit)_submit();}}
function toast(m){{var t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(function(){{t.style.display='none';}},2200);}}
function post(url,data,cb){{var fd=new FormData();for(var k in data)fd.append(k,data[k]);
  fetch(url,{{method:'POST',body:fd}}).then(function(r){{return r.json();}}).then(cb).catch(function(e){{toast('Error: '+e);}});}}
function newFile(path){{openModal('New File','<label>Nama</label><input id="x1" placeholder="Judul file">'
  +'<label>Format</label><select id="x2"><option value="md">Markdown (.md)</option><option value="html">Widget/HTML</option></select>'
  +'<label>Folder</label><select id="x3">'+MOVE_OPTS+'</select>'
  +'<label>Isi</label><textarea id="x4" placeholder="# Judul\\n\\nIsi..."></textarea>',function(){{
    var f=document.getElementById('x3');f.value=path;
    post('/api/create',{{name:document.getElementById('x1').value,kind:document.getElementById('x2').value,
      folder:f.value,content:document.getElementById('x4').value}},function(r){{closeModal();toast(r.msg||'dibuat');location.reload();}});}});
  setTimeout(function(){{document.getElementById('x3').value=path;}},0);}}
function newFolder(path){{openModal('New Folder','<label>Nama folder</label><input id="x1" placeholder="nama-folder">'
  +'<label>Di dalam</label><select id="x2">'+MOVE_OPTS+'</select>',function(){{
    post('/api/mkdir',{{name:document.getElementById('x1').value,parent:document.getElementById('x2').value}},
      function(r){{closeModal();toast(r.msg||'dibuat');location.reload();}});}});
  setTimeout(function(){{document.getElementById('x2').value=path;}},0);}}
function mv(id){{openModal('Pindahkan ke folder','<label>Folder tujuan</label><select id="x1">'+MOVE_OPTS+'</select>',function(){{
  post('/api/move',{{id:id,folder:document.getElementById('x1').value}},function(r){{closeModal();toast(r.msg||'dipindah');location.reload();}});}});}}
function del(id,name){{if(!confirm('Hapus "'+name+'"? (soft-delete, bisa dipulihkan dari .trash)'))return;
  post('/api/delete',{{id:id}},function(r){{toast(r.msg||'dihapus');location.reload();}});}}
function openImport(){{openModal('Import file','<label>Pilih file (.md/.html)</label><input type="file" id="x1" accept=".md,.html,.htm,.txt">'
  +'<label>Folder tujuan</label><select id="x2">'+MOVE_OPTS+'</select>',function(){{
    var f=document.getElementById('x1');if(!f.files.length){{toast('pilih file dulu');return;}}
    var fd=new FormData();fd.append('file',f.files[0]);fd.append('folder',document.getElementById('x2').value);
    fetch('/api/import',{{method:'POST',body:fd}}).then(function(r){{return r.json();}}).then(function(r){{closeModal();toast(r.msg||'diimport');location.reload();}});}});}}
{NOTES_MODAL_JS}
</script>"""
    return shell("Artifact Library", body, extra_head=f"<style>{BOARD_EXTRA_CSS}</style>")

def render_editor(aid):
    m = read_meta(aid)
    if m is None:
        return shell("Not found", '<div class="empty">Artifact tidak ditemukan.</div>')
    raw = (ART_DIR/aid/"current.html").read_text(encoding="utf-8", errors="replace")
    name = _html.escape(m.get("name") or aid)
    body = f"""
<h3>✎ Edit: {name}</h3>
<p class="meta">Perubahan disimpan sebagai versi baru (versioned). Format: {m.get('kind')}</p>
<textarea id="ed" style="width:100%;min-height:60vh;font-family:ui-monospace,monospace;font-size:13px;padding:12px;border:1px solid #d1d5db;border-radius:10px">{_html.escape(raw)}</textarea>
<div class="treebar"><button class="btn g" onclick="save()">💾 Simpan</button>
<a class="btn-reset" href="/view/artifact/{quote(aid)}">Lihat</a>
<a class="btn-reset" href="/">← Index</a></div>
<div class="toast" id="toast"></div>
<script>
function save(){{var fd=new FormData();fd.append('id','{aid}');fd.append('content',document.getElementById('ed').value);
  fetch('/api/edit',{{method:'POST',body:fd}}).then(function(r){{return r.json();}}).then(function(r){{
    var t=document.getElementById('toast');t.textContent=r.msg||'tersimpan';t.style.display='block';
    setTimeout(function(){{t.style.display='none';}},2000);}});}}
</script>"""
    return shell(f"Edit {m.get('name') or aid}", body)

# ---------- board (Scrum tracking view) ----------
BOARD_STAGES = ["stories", "backlog", "prd", "specs", "tasks", "reports"]
STAGE_LABEL = {"stories": "Story", "backlog": "Backlog", "prd": "PRD",
               "specs": "Spec", "tasks": "Task", "reports": "Report"}
# status Kanban columns (union across types, ordered left->right)
STATUS_COLS = ["todo", "in-progress", "blocked", "review", "done"]
STATUS_LABEL = {"todo": "To Do", "in-progress": "In Progress", "blocked": "Blocked",
                "review": "Review", "done": "Done", "draft": "To Do", "ready": "To Do",
                "in-backlog": "To Do", "refined": "To Do", "approved": "Review",
                "implemented": "Done", "in-review": "Review", "partial": "Review", "failed": "Blocked"}

def _board_fm(text):
    """Parse YAML front-matter (reuse same shape as validate.parse_frontmatter)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out

# Shared board/notes CSS (used by render_board AND render_notes) -- was a
# local string inside render_board only; extracted so the Research tab
# actually gets .bboard/.bcol/.bcard styling instead of rendering unstyled text.
BOARD_EXTRA_CSS = """
/* Container expansion for Board: full responsive desktop canvas */
.wrap:has(.bboard), .wrap.bwrap {
  max-width: 1600px !important;
  width: 95% !important;
  margin: 0 auto;
  padding: 16px 20px !important;
}

/* Board layout: flexible columns, smooth horizontal scrolling */
.bboard {
  display: flex;
  gap: 14px;
  overflow-x: auto;
  padding: 4px 2px 20px;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: thin;
  scrollbar-color: #cbd5e1 transparent;
}
.bboard::-webkit-scrollbar { height: 6px; }
.bboard::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
.bboard::-webkit-scrollbar-track { background: transparent; }

/* Column wrapper: modern rounded container with subtle border */
.bcol {
  flex: 0 0 280px;
  min-width: 280px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 8px 6px;
  box-shadow: 0 1px 3px rgba(0,0,0,.02);
  display: flex;
  flex-direction: column;
}
.bcol-h {
  font-weight: 700;
  font-size: 12.5px;
  color: #334155;
  padding: 6px 10px 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  text-transform: uppercase;
  letter-spacing: .04em;
}
.bcol-h::before {
  content: '';
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  background: #94a3b8;
}
.bcol[data-col="todo"] .bcol-h::before { background: #64748b; }
.bcol[data-col="in-progress"] .bcol-h::before { background: #3b82f6; box-shadow: 0 0 0 2px rgba(59,130,246,0.2); }
.bcol[data-col="blocked"] .bcol-h::before { background: #ef4444; }
.bcol[data-col="review"] .bcol-h::before { background: #8b5cf6; }
.bcol[data-col="done"] .bcol-h::before { background: #10b981; }

.bcnt {
  margin-left: auto;
  background: #e2e8f0;
  color: #475569;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  padding: 2px 8px;
  min-width: 22px;
  text-align: center;
}

/* Column body: cards container with minimum height */
.bcol-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 50px;
  padding: 2px;
  transition: background .2s, border-color .2s;
}
.bcol-body.drag-over {
  background: #eef2ff;
  border: 2px dashed #6366f1;
  border-radius: 10px;
}

/* Card: modern elevated style with subtle hover lift */
.bcard {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 11px 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,.03);
  transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
}
.bcard:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0,0,0,.06);
  border-color: #cbd5e1;
  cursor: grab;
}
.bcard.drag { cursor: grab; }
.bcard.dragging {
  opacity: .4;
  cursor: grabbing;
  transform: scale(0.98);
  box-shadow: 0 2px 6px rgba(0,0,0,.06);
}
.bcard.bcard-static { cursor: pointer; }

/* Card elements: ID, title, top row */
.bcard-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.bid {
  font: 600 11.5px ui-monospace, SFMono-Regular, Menlo, monospace;
  color: #4f46e5;
  background: #eef2ff;
  border: 1px solid #e0e7ff;
  border-radius: 6px;
  padding: 1px 7px;
  display: inline-block;
  letter-spacing: -0.01em;
}
.bsprint-tag {
  font: 500 11px ui-monospace, monospace;
  color: #64748b;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  padding: 1px 5px;
}
.btitle {
  font-size: 13.5px;
  color: #0f172a;
  margin: 6px 0 10px;
  line-height: 1.45;
  font-weight: 500;
}
.brow {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  align-items: center;
}

/* Chips and badges */
.bchip {
  font-size: 11px;
  background: #f1f5f9;
  color: #475569;
  border-radius: 999px;
  padding: 2px 7px;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  border: 1px solid #e2e8f0;
  font-weight: 500;
}
.bchip.bchip-agy {
  background: #fef2f2;
  color: #b91c1c;
  border-color: #fecaca;
  font-weight: 600;
}
.bchip.bchip-kiro {
  background: #f0fdf4;
  color: #15803d;
  border-color: #bbf7d0;
  font-weight: 600;
}
.bchip.role-backend {
  background: #eff6ff;
  color: #1d4ed8;
  border-color: #bfdbfe;
  font-weight: 600;
}
.bchip.role-frontend {
  background: #fdf4ff;
  color: #a21caf;
  border-color: #f5d0fe;
  font-weight: 600;
}
.bchip.role-design {
  background: #fffbeb;
  color: #b45309;
  border-color: #fde68a;
  font-weight: 600;
}
.bchip.role-qa {
  background: #ecfdf5;
  color: #047857;
  border-color: #a7f3d0;
  font-weight: 600;
}
.bstage {
  font-size: 10px;
  border-radius: 999px;
  padding: 2px 7px;
  color: #fff;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .02em;
}
.s-stories{background:#8b5cf6}.s-backlog{background:#0284c7}.s-prd{background:#d97706}
.s-specs{background:#059669}.s-tasks{background:#e11d48}.s-reports{background:#4f46e5}
.bstage.bstage-arsip{background:#64748b;font-size:10px}

/* Retry button */
.btn-retry {
  margin-left: auto;
  background: #6366f1;
  color: #ffffff;
  border: none;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  cursor: pointer;
  transition: transform .15s ease, background-color .15s ease;
}
.btn-retry:hover {
  background: #4f46e5;
  transform: translateY(-1px);
}

/* Empty state */
.bempty {
  color: #94a3b8;
  text-align: center;
  padding: 24px 8px;
  font-size: 13px;
  font-style: italic;
}

/* Progress sprint ribbon: horizontal track */
.sprogwrap {
  display: flex;
  flex-direction: row;
  gap: 12px;
  overflow-x: auto;
  padding: 4px 2px 14px;
  margin: 4px 0 12px;
  scrollbar-width: thin;
  scrollbar-color: #cbd5e1 transparent;
  -webkit-overflow-scrolling: touch;
}
.sprogwrap::-webkit-scrollbar { height: 6px; }
.sprogwrap::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }

.sprog {
  flex: 0 0 240px;
  min-width: 240px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  text-decoration: none;
  color: inherit;
  box-shadow: 0 1px 3px rgba(0,0,0,.03);
  transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
}
.sprog:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(0,0,0,.06);
  border-color: #cbd5e1;
}
.sprog.ssel {
  border-color: #6366f1;
  background: #fafbff;
  box-shadow: 0 0 0 2px rgba(99,102,241,.18), 0 4px 12px rgba(99,102,241,.08);
}
.sprog-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}
.sprog-name {
  font-weight: 700;
  font-size: 13.5px;
  color: #0f172a;
  letter-spacing: -0.01em;
}
.sstat-badge {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  padding: 2px 7px;
  border-radius: 999px;
  text-transform: uppercase;
}
.sstat-done {
  background: #ecfdf5;
  color: #047857;
  border: 1px solid #a7f3d0;
}
.sstat-active {
  background: #eff6ff;
  color: #1d4ed8;
  border: 1px solid #bfdbfe;
  position: relative;
  padding-left: 14px;
}
.sstat-active::before {
  content: '';
  position: absolute;
  left: 5px;
  top: 50%;
  transform: translateY(-50%);
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #2563eb;
  animation: sprog-pulse 2s infinite;
}
@keyframes sprog-pulse {
  0% { box-shadow: 0 0 0 0 rgba(37,99,235,0.5); }
  70% { box-shadow: 0 0 0 5px rgba(37,99,235,0); }
  100% { box-shadow: 0 0 0 0 rgba(37,99,235,0); }
}
.sstat-planning {
  background: #f1f5f9;
  color: #475569;
  border: 1px solid #e2e8f0;
}
.sprog-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #64748b;
  margin-bottom: 8px;
  font-weight: 500;
}
.sbar {
  height: 5px;
  background: #e2e8f0;
  border-radius: 999px;
  margin: 2px 0 8px;
  overflow: hidden;
}
.sfill {
  height: 100%;
  background: linear-gradient(90deg, #6366f1, #8b5cf6);
  border-radius: 999px;
  transition: width .3s ease;
}
.sgoal {
  font-size: 11.5px;
  color: #475569;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Toolbar: clean modern controls */
.treebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 0 0 14px;
  flex-wrap: wrap;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 8px 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,.02);
}
.tb-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.tb-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-left: auto;
  position: relative;
}
.segmented-control {
  display: inline-flex;
  background: #f1f5f9;
  border-radius: 8px;
  padding: 3px;
  gap: 2px;
}
.segmented-control .btn {
  padding: 5px 12px;
  font-size: 12.5px;
  font-weight: 600;
  border-radius: 6px;
  transition: all .15s ease;
  line-height: 1.4;
}
.segmented-control .btn.g {
  background: #ffffff;
  color: #0f172a;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
}
.segmented-control .btn.n {
  background: transparent;
  color: #64748b;
}
.segmented-control .btn.n:hover {
  color: #0f172a;
  background: rgba(255,255,255,.5);
}
.filter-group {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.filter-group select {
  background: #ffffff;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12.5px;
  color: #1e293b;
  font-weight: 500;
  outline: none;
  cursor: pointer;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.filter-group select:focus {
  border-color: #6366f1;
  box-shadow: 0 0 0 2px rgba(99,102,241,.15);
}
.item-count-chip {
  font-size: 11.5px;
  color: #64748b;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 999px;
  padding: 4px 10px;
  font-weight: 600;
}
.bridge-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 500;
  color: #475569;
  cursor: default;
  white-space: nowrap;
}
.timer-group {
  display: inline-flex;
  align-items: center;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 2px 4px;
  gap: 4px;
}
.timer-group select, .timer-group input {
  background: #ffffff;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  padding: 4px 6px;
  font-size: 11.5px;
  color: #1e293b;
}
.timer-group input {
  width: 48px;
}
.btn-timer-set {
  background: #059669;
  color: #ffffff;
  border: none;
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s ease;
}
.btn-timer-set:hover {
  background: #047857;
}
.bell-btn {
  position: relative;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 8px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  transition: background .15s ease, border-color .15s ease;
}
.bell-btn:hover {
  background: #f8fafc;
  border-color: #cbd5e1;
}
.bell-icon {
  font-size: 16px;
  line-height: 1;
}
#bellBadge {
  position: absolute;
  top: -4px;
  right: -4px;
  background: #ef4444;
  color: #ffffff;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  padding: 0 5px;
  line-height: 15px;
  min-width: 15px;
  text-align: center;
  box-shadow: 0 1px 2px rgba(0,0,0,.2);
}
.bhint {
  font-size: 12px;
  color: #64748b;
  margin: 4px 0 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}

/* Notes tab: section header above each group's card row (artifact-library style) */
.nsec-h{font-weight:700;font-size:13px;color:#475569;text-transform:uppercase;letter-spacing:.05em;margin:18px 0 8px;display:flex;align-items:center;gap:8px;border-bottom:1px solid #e2e8f0;padding-bottom:6px}
.nsec-h .bcnt{background:#94a3b8}
.ngrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
@media (max-width:768px){.ngrid{grid-template-columns:repeat(auto-fill,minmax(100%,1fr));gap:10px}.ncard{padding:12px}}
@media (max-width:480px){.ngrid{gap:8px}.ncard{padding:10px}}
.ncard{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;box-shadow:0 2px 4px rgba(0,0,0,.04);transition:transform .15s ease,box-shadow .15s ease,border-color .15s;cursor:pointer;display:flex;flex-direction:column;gap:6px}
.ncard:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.08);border-color:#cbd5e1}
.ncard:focus-visible{outline:2px solid #6366f1;outline-offset:2px;border-color:#6366f1}
.ncard .ntitle{font-size:15px;font-weight:600;color:#0f172a;line-height:1.35}
.ncard .nfname{font:600 11px ui-monospace,monospace;color:#94a3b8}
/*
WCAG AA compliant badge colors (contrast ratio ≥4.5:1):
- .bchip-addendum: bg #fef3c7 (light yellow), fg #92400e (dark brown) = 12.6:1 ✓
- .bchip-ro: bg #f1f5f9 (light gray), fg #64748b (slate) = 5.3:1 ✓ (bare minimum)
- .bchip-addendum on .bchip-ro combo: dark brown on light gray = 10.2:1 ✓
*/
.bchip.bchip-addendum{background:#fef3c7;color:#92400e;border-color:#fde68a}
.bchip.bchip-ro{background:#eef2f7;color:#475569;border-color:#e2e8f0}
"""

def load_board():
    """Scan ~/Apps/board -> list of items with stage, status, sprint, assignee.
    Reads disk every call (auto-load, no cache), same contract as artifacts."""
    items, sprints = [], {}
    if not BOARD_DIR.is_dir():
        return items, sprints
    # Valid artifact ID prefix per stage (mirrors validate.py STAGES) -- skips
    # stray files like README.md that live inside a stage folder but aren't artifacts.
    _ID_PREFIX = {"stories": "US", "backlog": "BL", "prd": "PRD",
                  "specs": "SPEC", "tasks": "TASK", "reports": "RPT"}
    for f in sorted(BOARD_DIR.rglob("*")):
        if not f.is_file():
            continue
        stage = f.parent.name
        if stage not in BOARD_STAGES:
            continue
        is_task = (stage == "tasks")
        if is_task != (f.suffix == ".json"):
            continue
        prefix = _ID_PREFIX.get(stage)
        if prefix and not re.match(rf"^{prefix}-\d{{3,}}$", f.stem):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if is_task:
            try:
                meta = json.loads(text)
            except Exception:
                meta = {}
        else:
            meta = _board_fm(text)
        # locate sprint (sprints/sprint-NN/...) vs product-backlog vs archive
        parts = f.relative_to(BOARD_DIR).parts
        archived = parts[0] == "archive"
        if parts and parts[0] == "sprints" and len(parts) > 1:
            sprint = parts[1]
        elif archived:
            sprint = next((p for p in parts if p.startswith("sprint-")), "archive")
        else:
            sprint = "product-backlog"
        status_raw = (meta.get("status") or "todo").lower()
        items.append({
            "id": meta.get("id") or f.stem, "title": meta.get("title") or f.stem,
            "stage": stage, "status": status_raw, "assignee": meta.get("assignee") or "",
            "role": (meta.get("role") or "").lower(),
            "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
            "sprint": sprint, "rel": f.relative_to(BOARD_DIR).as_posix(), "archived": archived,
        })
    # sprint goals/status (active sprints + archived sprints)
    sprint_dirs = list((BOARD_DIR / "sprints").glob("sprint-*")) if (BOARD_DIR / "sprints").is_dir() else []
    sprint_dirs += list((BOARD_DIR / "archive").glob("sprint-*")) if (BOARD_DIR / "archive").is_dir() else []
    for sd in sorted(sprint_dirs):
        sp = sd / "SPRINT.md"
        g = {"goal": "", "category": "", "status": "active"}
        if sp.is_file():
            fm = _board_fm(sp.read_text(encoding="utf-8", errors="replace"))
            g.update({k: fm.get(k, g[k]) for k in g})
        g["archived"] = (sd.parent.name == "archive")
        sprints[sd.name] = g
    return items, sprints

def _board_card(it, drag=False):
    # Assignee chip dengan styling berbeda untuk agy vs kirocrew
    asg_cls = "bchip-agy" if it.get("assignee") == "agy" else ("bchip-kiro" if it.get("assignee") == "kirocrew" else "")
    asg = f'<span class="bchip{" " + asg_cls if asg_cls else ""}">{_html.escape(it["assignee"] or "unassigned")}</span>' if it["assignee"] else ""
    stg = f'<span class="bstage s-{it["stage"]}">{STAGE_LABEL.get(it["stage"], it["stage"])}</span>'
    arch = '<span class="bstage bstage-arsip">arsip</span>' if it.get("archived") else ""
    # Role flag / badge (Backend, Frontend, Design, QA)
    role_flag = ""
    role = it.get("role") or ""
    if role:
        role_label = {"backend": "🛠 BE", "frontend": "🎨 FE", "design": "✨ Design", "qa": "🧪 QA"}.get(role, role.upper())
        role_flag = f'<span class="bchip role-{_html.escape(role)}">{_html.escape(role_label)}</span>'
    # only tasks are drag-movable (status is a task concept); draggable only in status view
    can_drag = drag and it["stage"] == "tasks" and not it.get("archived")
    dattr = (f' draggable="true" ondragstart="dragStart(event,\'{_html.escape(it["id"])}\')"'
             f' ondragend="dragEnd(event)"') if can_drag else ""
    cls = "bcard drag" if can_drag else "bcard"
    # tasks AND reports open a modal; others (story/backlog/prd/spec) stay static
    clickable = ' onclick="openCard(this)"' if it["stage"] in ("tasks", "reports") else ""
    cur = ' style="cursor:pointer"' if it["stage"] in ("tasks", "reports") else ""
    retry = ""
    if it["stage"] == "tasks" and it.get("status") in ("blocked", "failed") and it.get("assignee") == "agy":
        retry = f'<button class="btn-retry" onclick="retryTask(\'{_html.escape(it["id"])}\',event)">🔄 retry</button>'
    sprint_tag = f'<span class="bsprint-tag">{_html.escape(it["sprint"])}</span>' if it.get("sprint") and it["sprint"] != "product-backlog" else ""
    return (f'<div class="{cls}"{cur} data-asg="{_html.escape(it["assignee"])}" data-role="{_html.escape(role)}"'
            f' data-sprint="{_html.escape(it["sprint"])}"'
            f' data-id="{_html.escape(it["id"])}" data-stage="{it["stage"]}" data-arch="{"1" if it.get("archived") else ""}"{dattr}{clickable}>'
            f'<div class="bcard-top"><span class="bid">{_html.escape(it["id"])}</span>{sprint_tag}</div>'
            f'<div class="btitle">{_html.escape(it["title"])}</div>'
            f'<div class="brow">{stg}{role_flag}{asg}{arch}{retry}</div></div>')

def render_board(view="status", sprint_filter="", show_archived=False):
    items, sprints = load_board()
    # split active vs archived; default board shows active only
    if show_archived:
        items = [it for it in items if it.get("archived")]
    else:
        items = [it for it in items if not it.get("archived")]
    # sprint filter dropdown
    all_sprints = sorted({it["sprint"] for it in items})
    if sprint_filter:
        items = [it for it in items if it["sprint"] == sprint_filter]
    # progress per sprint (match archived scope)
    prog = []
    for sn in sorted(sprints.keys()):
        if bool(sprints[sn].get("archived")) != bool(show_archived):
            continue
        ts = [it for it in items if it["sprint"] == sn and it["stage"] == "tasks"]
        done = sum(1 for it in ts if it["status"] == "done")
        total = len(ts)
        pct = int(done * 100 / total) if total else 0
        g = sprints[sn]
        s_status = (g.get("status") or "active").lower()
        sel = " ssel" if sn == sprint_filter else ""
        href = f'/board?view={view}&sprint={quote(sn)}' + ("&archived=1" if show_archived else "")
        prog.append(
            f'<a class="sprog{sel}" href="{href}" title="Lihat task {sn}">'
            f'<div class="sprog-top">'
            f'<span class="sprog-name">{_html.escape(sn)}</span>'
            f'<span class="sstat-badge sstat-{s_status}">{_html.escape(s_status.upper())}</span>'
            f'</div>'
            f'<div class="sprog-meta"><span class="scat">{_html.escape(g.get("category") or "Sprint")}</span><span class="stask-count">{done}/{total} done</span></div>'
            f'<div class="sbar"><div class="sfill" style="width:{pct}%"></div></div>'
            f'<div class="sgoal">{_html.escape(g.get("goal") or "")}</div></a>'
        )
    prog_html = f'<div class="sprogwrap">{"".join(prog)}</div>' if prog else ""

    # columns depend on view
    if view == "stage":
        cols = BOARD_STAGES
        col_label = lambda c: STAGE_LABEL[c]
        bucket = lambda it: it["stage"]
    else:  # status
        cols = STATUS_COLS
        col_label = lambda c: STATUS_LABEL[c]
        def bucket(it):
            s = it["status"]
            return s if s in STATUS_COLS else {"draft":"todo","ready":"todo","in-backlog":"todo",
                "refined":"todo","approved":"review","implemented":"done","in-review":"review",
                "partial":"review","failed":"blocked"}.get(s, "todo")

    is_status = (view != "stage")
    # Status view = Kanban status, konsep yang hanya berlaku untuk task (unit eksekusi agy);
    # story/backlog/spec/report adalah artefak perencanaan/laporan, sudah punya kolom sendiri
    # di view Alur -- disembunyikan di sini agar kolom status tidak tercampur type lain.
    view_items = [it for it in items if it["stage"] == "tasks"] if is_status else items
    colmap = {c: [] for c in cols}
    for it in view_items:
        b = bucket(it)
        if b in colmap:
            colmap[b].append(it)
    col_html = []
    for c in cols:
        cards = colmap[c]
        drop = (f' ondragover="dragOver(event)" ondrop="dropCard(event,\'{c}\')"'
                f' ondragleave="dragLeave(event)"') if is_status else ""
        col_html.append(
            f'<div class="bcol" data-col="{c}"><div class="bcol-h">{col_label(c)}<span class="bcnt">{len(cards)}</span></div>'
            f'<div class="bcol-body"{drop}>{"".join(_board_card(it, drag=is_status) for it in cards) or "<div class=bempty>—</div>"}</div></div>')

    sprint_opts = '<option value="">Semua sprint</option>' + "".join(
        f'<option value="{_html.escape(s)}"{" selected" if s==sprint_filter else ""}>{_html.escape(s)}</option>'
        for s in all_sprints)
    tog = lambda v, lbl: (f'<a class="btn {"g" if v==view else "n"}" '
                          f'href="/board?view={v}{"&sprint="+quote(sprint_filter) if sprint_filter else ""}{"&archived=1" if show_archived else ""}">{lbl}</a>')
    arch_tog = (f'<a class="btn {"g" if show_archived else "n"}" '
                f'href="/board?view={view}{"&archived=1" if not show_archived else ""}">{"📦 Arsip" if not show_archived else "📦 Arsip ✓"}</a>')
    asgs = sorted({it["assignee"] for it in items if it["assignee"]})
    asg_opts = '<option value="">Semua assignee</option>' + "".join(f'<option value="{_html.escape(a)}">{_html.escape(a)}</option>' for a in asgs)
    roles = [r for r in ["backend", "frontend", "design", "qa"] if any(it.get("role")==r for it in items)]
    role_labels = {"backend": "🛠 Backend", "frontend": "🎨 Frontend", "design": "✨ Design", "qa": "🧪 QA"}
    role_opts = '<option value="">Semua peran</option>' + "".join(f'<option value="{_html.escape(r)}">{_html.escape(role_labels.get(r, r.upper()))}</option>' for r in roles)

    body = f"""
<div class="treebar noprint">
  <div class="tb-left">
    <div class="segmented-control">
      {tog("status","🗂 Status")}{tog("stage","🪜 Alur")}{arch_tog}<a class="btn n" href="/#notes-root">📚 Research</a>
    </div>
    <div class="filter-group">
      <select id="bsprint" onchange="var v=this.value;location.href='/board?view={view}'+(v?'&sprint='+encodeURIComponent(v):'')+'{"&archived=1" if show_archived else ""}'">{sprint_opts}</select>
      <select id="basg" onchange="applyFilters()">{asg_opts}</select>
      <select id="brole" onchange="applyFilters()">{role_opts}</select>
      <span class="item-count-chip">{len(view_items)} item{" · ARSIP" if show_archived else ""}</span>
    </div>
  </div>
  <div class="tb-right">
    <span id="bridgeHealth" class="bridge-pill" title="Kesehatan bridge agy">⏳ cek bridge…</span>
    <span class="timer-group" title="Interval cron bridge">
      <select id="ivTimer" onchange="loadInterval()">{"".join(f'<option value="{u}">{l}</option>' for u,l in TIMERS.items())}</select>
      <input id="ivVal" type="number" min="1" step="1" value="5">
      <select id="ivUnit"><option value="minute">min</option><option value="hours">hr</option><option value="days">d</option></select>
      <button class="btn-timer-set" onclick="setInterval_()">Set</button>
    </span>
    <span id="bellWrap" class="bell-btn" onclick="bellClick(event)" title="Notifikasi task selesai">
      <span class="bell-icon">🔔</span>
      <span id="bellBadge" style="display:none">0</span>
    </span>
    <div id="bellPanel" style="display:none;position:absolute;right:0;top:calc(100% + 8px);width:360px;max-height:420px;overflow:auto;background:#1e293b;color:#f1f5f9;border:1px solid #334155;border-radius:12px;box-shadow:0 12px 36px rgba(0,0,0,.25);z-index:9999">
      <div style="padding:12px 14px;border-bottom:1px solid #334155;font-weight:600;font-size:13px;display:flex;justify-content:space-between;align-items:center">
        <span>Task selesai (H+7)</span>
        <span id="bellClear" style="font-size:11px;color:#94a3af;cursor:pointer;padding:2px 6px;border-radius:4px;background:#334155">bersihkan badge</span>
      </div>
      <div id="bellList" style="padding:4px"></div>
    </div>
  </div>
</div>
{prog_html}
<div class="bhint noprint">{"💡 Seret kartu Task antar-kolom untuk ubah status (tersimpan ke board + sync artifact)." if is_status else "🪜 Mode Alur: kolom = tahap produk (read-only). Beralih ke Status untuk drag-ubah status."}</div>
<div class="bboard">{"".join(col_html)}</div>
<div class="toast" id="toast"></div>
<script>
var _w = document.querySelector('.wrap');
if (_w) _w.classList.add('bwrap');
function applyFilters(){{
  var a = (document.getElementById('basg')||{{}}).value || '';
  var r = (document.getElementById('brole')||{{}}).value || '';
  document.querySelectorAll('.bcard').forEach(function(c){{
    var matchA = !a || c.dataset.asg === a;
    var matchR = !r || c.dataset.role === r;
    c.style.display = (matchA && matchR) ? '' : 'none';
  }});
  document.querySelectorAll('.bcol').forEach(function(col){{
    var n = Array.prototype.filter.call(col.querySelectorAll('.bcard'), function(c){{return c.style.display !== 'none';}}).length;
    col.querySelector('.bcnt').textContent = n;
  }});
}}
function filterAsg(a){{applyFilters();}}
function toast(m){{var t=document.getElementById('toast');if(!t)return;t.textContent=m;t.style.display='block';setTimeout(function(){{t.style.display='none';}},2200);}}
var _dragId=null;
function dragStart(e,id){{_dragId=id;e.dataTransfer.effectAllowed='move';try{{e.dataTransfer.setData('text/plain',id);}}catch(_){{}} e.target.classList.add('dragging');}}
function dragEnd(e){{e.target.classList.remove('dragging');document.querySelectorAll('.bcol-body.drag-over').forEach(function(x){{x.classList.remove('drag-over');}});}}
function dragOver(e){{e.preventDefault();e.dataTransfer.dropEffect='move';e.currentTarget.classList.add('drag-over');}}
function dragLeave(e){{e.currentTarget.classList.remove('drag-over');}}
function dropCard(e,status){{e.preventDefault();e.currentTarget.classList.remove('over');
  var id=_dragId||e.dataTransfer.getData('text/plain');if(!id)return;
  var card=document.querySelector('.bcard[data-id="'+id+'"]');
  if(card){{e.currentTarget.appendChild(card);recount();}}
  var fd=new FormData();fd.append('id',id);fd.append('status',status);
  fetch('/api/board/status',{{method:'POST',body:fd}}).then(function(r){{return r.json();}}).then(function(r){{
    toast(r.ok?('✓ '+r.msg):('✗ '+r.msg));if(!r.ok)setTimeout(function(){{location.reload();}},800);}})
    .catch(function(){{toast('Error jaringan');setTimeout(function(){{location.reload();}},800);}});}}
function recount(){{document.querySelectorAll('.bcol').forEach(function(col){{
  var body=col.querySelector('.bcol-body');var e=body.querySelector('.bempty');
  var n=body.querySelectorAll('.bcard').length;if(e)e.style.display=n?'none':'';
  col.querySelector('.bcnt').textContent=n;}});}}
// ---- bridge health + bell dropdown + interval selector + retry ----
var _lastDone=null, _bellCount=0, _bellOpen=false;
function bellClick(e){{if(e)e.stopPropagation();_bellOpen=!_bellOpen;
  var p=document.getElementById('bellPanel');if(!p)return;
  p.style.display=_bellOpen?'block':'none';
  if(_bellOpen){{loadEvents();_bellCount=0;var b=document.getElementById('bellBadge');if(b)b.style.display='none';
    if(window.Notification&&Notification.permission!=='granted'){{try{{Notification.requestPermission();}}catch(_){{}}}}}}}}
document.addEventListener('click',function(e){{var w=document.getElementById('bellWrap'),p=document.getElementById('bellPanel');
  if(_bellOpen&&p&&w&&!p.contains(e.target)&&!w.contains(e.target)){{_bellOpen=false;p.style.display='none';}}}});
function _ico(k){{return k==='done'?'✅':k==='retry'?'🔄':k==='unblock'?'🔓':k==='review'?'🔎':k==='ci-fix'?'🔧':k==='blocked'?'⛔':'•';}}
function loadEvents(){{fetch('/api/bridge/events').then(function(r){{return r.json();}}).then(function(d){{
  var el=document.getElementById('bellList');if(!el)return;var ev=d.events||[];
  if(!ev.length){{el.innerHTML='<div style="padding:14px;color:#9ca3af;font-size:13px">Belum ada event.</div>';return;}}
  el.innerHTML=ev.map(function(e){{
    var when=(e.ts||'').replace('T',' ').slice(0,16);
    var rpt=e.report?(' · <a href="/view/doc/'+encodeURIComponent(e.report)+'" style="color:#93c5fd" target="_blank">'+e.report+'</a>'):'';
    return '<div style="padding:8px 10px;border-bottom:1px solid #1f2937">'+
      '<div style="font-size:13px">'+_ico(e.kind)+' <b>'+(e.task_id||'')+'</b> '+(e.title||'').replace(/</g,'&lt;')+'</div>'+
      '<div style="font-size:11px;color:#9ca3af">'+when+' · '+e.kind+rpt+'</div></div>';}}).join('');}})
  .catch(function(){{var el=document.getElementById('bellList');if(el)el.innerHTML='<div style="padding:14px;color:#f87171">Gagal memuat event.</div>';}});}}
var _bc=document.getElementById('bellClear');if(_bc)_bc.onclick=function(ev){{ev.stopPropagation();_bellCount=0;var b=document.getElementById('bellBadge');if(b)b.style.display='none';}};
function _bell(n){{_bellCount+=n;var b=document.getElementById('bellBadge');if(b){{b.textContent=_bellCount;b.style.display=_bellCount>0?'':'none';}}
  if(window.Notification&&Notification.permission==='granted'){{try{{new Notification('Board: '+n+' task selesai',{{body:'Tim agy/kiro menyelesaikan task baru.'}});}}catch(_){{}}}}
  toast('🔔 '+n+' task baru selesai');if(_bellOpen)loadEvents();}}
function pollBridge(){{fetch('/api/bridge/health').then(function(r){{return r.json();}}).then(function(h){{
  var el=document.getElementById('bridgeHealth');if(!el)return;
  var t=h.tasks||{{}}, ic=h.healthy?'🟢':'🔴';
  el.textContent=ic+' bridge '+(h.timer_active||'?')+' · todo '+(t.todo||0)+' · ⏳'+(t.in_progress||0)+' · ⛔'+(t.blocked||0)+' · 🔎'+(t.review||0)+' · ✅'+(t.done||0);
  el.title='Last: '+(h.last_when||'?')+' — '+(h.last_line||'')+'\\nNext: '+(h.next_trigger||'?');
  var d=t.done||0; if(_lastDone!==null && d>_lastDone){{_bell(d-_lastDone);}} _lastDone=d;
}}).catch(function(){{var el=document.getElementById('bridgeHealth');if(el){{el.textContent='⚠ bridge: tak terjangkau';}}}});}}
function loadInterval(){{fetch('/api/bridge/interval?unit='+encodeURIComponent(document.getElementById('ivTimer').value)).then(function(r){{return r.json();}}).then(function(d){{
  var v=document.getElementById('ivVal'), u=document.getElementById('ivUnit');if(!v||!u)return;
  var mins=d.minutes||5;
  if(mins%1440===0){{v.value=mins/1440;u.value='days';}}
  else if(mins%60===0){{v.value=mins/60;u.value='hours';}}
  else {{v.value=mins;u.value='minute';}}
}});}}
function setInterval_(){{var v=document.getElementById('ivVal'), u=document.getElementById('ivUnit');if(!v)return;
  var n=parseInt(v.value,10);if(!n||n<1){{toast('✗ masukkan angka ≥ 1');return;}}
  var mult={{minute:1,hours:60,days:1440}}[u.value]||1; var mins=n*mult;
  var fd=new FormData();fd.append('minutes',mins);fd.append('unit',document.getElementById('ivTimer').value);
  fetch('/api/bridge/interval',{{method:'POST',body:fd}}).then(function(r){{return r.json();}}).then(function(r){{
    toast(r.ok?('✓ '+r.msg):('✗ '+r.msg));if(r.ok)setTimeout(pollBridge,800);}});}}
function retryTask(id,ev){{if(ev)ev.stopPropagation();
  var fd=new FormData();fd.append('id',id);
  fetch('/api/board/retry',{{method:'POST',body:fd}}).then(function(r){{return r.json();}}).then(function(r){{
    toast(r.ok?('🔄 '+r.msg):('✗ '+r.msg));if(r.ok)setTimeout(function(){{location.reload();}},700);}});}}
if(window.Notification&&Notification.permission==='default'){{try{{Notification.requestPermission();}}catch(_){{}}}}
pollBridge();loadInterval();setInterval(pollBridge,30000);
// ---- task view/edit/delete modal ----
var _tStatus=['todo','in-progress','blocked','review','done'];
function _esc(s){{return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
function openTask(card){{
  if(card.classList.contains('dragging'))return;
  var id=card.dataset.id, archived=card.dataset.arch==='1';
  fetch('/api/board/task?id='+encodeURIComponent(id)).then(function(r){{return r.json();}}).then(function(r){{
    if(!r.ok){{toast('✗ '+r.msg);return;}}
    var t=r.task; _showTaskView(t, archived);
  }}).catch(function(){{toast('Error jaringan');}});
}}
// Dispatch: report cards -> read-only report viewer; task cards -> task modal.
// Edit/delete allowed ONLY for a task that is NOT archived, NOT in stage(alur)
// view, and whose status is actionable (blocked/failed/todo/in-progress).
// Done/review tasks and all reports are view-only (DoD/CI/acceptance).
function openCard(card){{
  if(card.classList.contains('dragging'))return;
  var stage=card.dataset.stage, id=card.dataset.id, archived=card.dataset.arch==='1';
  if(stage==='reports'){{return _showReportView(id);}}
  var isStage = (document.querySelector('.treebar .btn.g')||{{}}).textContent||'';
  var stageView = isStage.indexOf('Alur')>=0;
  fetch('/api/board/task?id='+encodeURIComponent(id)).then(function(r){{return r.json();}}).then(function(r){{
    if(!r.ok){{toast('✗ '+r.msg);return;}}
    var t=r.task||{{}};
    var editable = !archived && !stageView && ['blocked','failed','todo','in-progress'].indexOf((t.status||'').toLowerCase())>=0;
    _showTaskView(t, !editable);
  }}).catch(function(){{toast('Error jaringan');}});
}}
function _showReportView(id){{
  _bmodal('Report '+id, '<div class=smeta style="padding:20px">Memuat…</div>');
  fetch('/api/board/report?id='+encodeURIComponent(id)).then(function(r){{return r.json();}}).then(function(d){{
    if(!d.ok){{_bmodal('Report '+id, '<p class=smeta style="padding:20px">'+(d.msg||'Gagal memuat')+'</p>');return;}}
    var html='<div style="max-height:60vh;overflow:auto;font-size:14px;line-height:1.55">'+d.html+'</div>'
      +'<div class="modal-actions"><button class="btn n" onclick="closeBModal()">Tutup</button></div>';
    _bmodal('Report '+id+' (view-only)', html);
  }}).catch(function(){{_bmodal('Report '+id, '<p class=smeta style="padding:20px">Gagal memuat report.</p>');}});
}}
function _bmodal(title,html){{
  var m=document.getElementById('bmodal');
  if(!m){{m=document.createElement('div');m.id='bmodal';m.className='modal';m.innerHTML='<div class="modal-box"><h3 id="bmt"></h3><div id="bmb"></div></div>';document.body.appendChild(m);
    m.addEventListener('click',function(e){{if(e.target===m)closeBModal();}});}}
  document.getElementById('bmt').textContent=title;document.getElementById('bmb').innerHTML=html;m.classList.add('open');
}}
function closeBModal(){{var m=document.getElementById('bmodal');if(m)m.classList.remove('open');}}
function _showTaskView(t, archived){{
  var isDone=(t.status||'').toLowerCase()==='done';
  var mark=isDone?'✅ ':'☐ ';
  var acc=(t.acceptance||[]).map(function(a){{return '<li style="list-style:none;margin-left:-18px">'+mark+_esc(a)+'</li>';}}).join('')||'<li class=smeta>—</li>';
  var ro=archived?' <span class="bstage" style="background:#6b7280">read-only</span>':'';
  var rpt=t.report?('<label>Report / DoD</label><div class="brow"><a class="btn n" href="/view/doc/'+encodeURIComponent(t.report)+'" target="_blank">📄 '+_esc(t.report)+'</a></div>'):'';
  var html=''
    +'<div class="brow" style="margin-bottom:10px">'
    +'<span class="bstage s-tasks">'+_esc(t.id)+'</span>'
    +'<span class="bchip">'+_esc(t.status)+'</span>'
    +'<span class="bchip">'+_esc(t.assignee||'-')+'</span>'+ro+'</div>'
    +'<p style="font-size:15px;font-weight:600;margin:0 0 8px">'+_esc(t.title)+'</p>'
    +'<label>Deskripsi</label><div class="tdesc">'+_esc(t.description||'—')+'</div>'
    +'<label>'+(isDone?'Definition of Done (acceptance)':'Acceptance')+'</label><ul style="margin:4px 0 0;padding-left:18px">'+acc+'</ul>'
    +rpt
    +'<div class="modal-actions">'
    +(archived?'':'<button class="btn n" onclick="_editTask('+"'"+_esc(t.id)+"'"+')">✏️ Edit</button>'
      +'<button class="btn" style="background:#dc2626;color:#fff" onclick="_delTask('+"'"+_esc(t.id)+"'"+')">🗑 Hapus</button>')
    +'<button class="btn n" onclick="closeBModal()">Tutup</button></div>';
  _bmodal('Task '+t.id+(archived?' (view-only)':''), html);
  window._curTask=t;
}}
function _editTask(id){{
  var t=window._curTask;if(!t)return;
  var opts=_tStatus.map(function(s){{return '<option value="'+s+'"'+(s===t.status?' selected':'')+'>'+s+'</option>';}}).join('');
  var acc=(t.acceptance||[]).join('\\n');
  var html=''
    +'<label>Judul</label><input id="et_title" value="'+_esc(t.title)+'">'
    +'<label>Status</label><select id="et_status">'+opts+'</select>'
    +'<label>Assignee</label><select id="et_assignee"><option value="agy"'+(t.assignee==='agy'?' selected':'')+'>agy</option><option value="kirocrew"'+(t.assignee==='kirocrew'?' selected':'')+'>kirocrew</option></select>'
    +'<label>Deskripsi</label><textarea id="et_desc" style="min-height:140px">'+_esc(t.description||'')+'</textarea>'
    +'<label>Acceptance (satu per baris)</label><textarea id="et_acc" style="min-height:90px">'+_esc(acc)+'</textarea>'
    +'<div class="modal-actions"><button class="btn n" onclick="_showTaskView(window._curTask,false)">Batal</button>'
    +'<button class="btn g" onclick="_saveTask('+"'"+_esc(id)+"'"+')">💾 Simpan</button></div>';
  _bmodal('Edit '+id, html);
}}
function _saveTask(id){{
  var fd=new FormData();fd.append('id',id);
  fd.append('title',document.getElementById('et_title').value);
  fd.append('status',document.getElementById('et_status').value);
  fd.append('assignee',document.getElementById('et_assignee').value);
  fd.append('description',document.getElementById('et_desc').value);
  fd.append('acceptance',document.getElementById('et_acc').value);
  fetch('/api/board/edit',{{method:'POST',body:fd}}).then(function(r){{return r.json();}}).then(function(r){{
    toast(r.ok?('✓ '+r.msg):('✗ '+r.msg));if(r.ok){{closeBModal();setTimeout(function(){{location.reload();}},500);}}}})
    .catch(function(){{toast('Error jaringan');}});
}}
function _delTask(id){{
  if(!confirm('Hapus task '+id+'? (dipindah ke .trash, bisa dipulihkan manual)'))return;
  var fd=new FormData();fd.append('id',id);
  fetch('/api/board/delete',{{method:'POST',body:fd}}).then(function(r){{return r.json();}}).then(function(r){{
    toast(r.ok?('✓ '+r.msg):('✗ '+r.msg));if(r.ok){{closeBModal();setTimeout(function(){{location.reload();}},500);}}}})
    .catch(function(){{toast('Error jaringan');}});
}}
</script>"""
    return shell("Board — Scrum Tracking", body, extra_head=f"<style>{BOARD_EXTRA_CSS}</style>")

# ----- board write op: change a task's status (drag-to-move) -----
TASK_STATUS = ["todo", "in-progress", "blocked", "review", "done"]
_BOARD_ID_RE = re.compile(r"^TASK-\d{3,}$")

def op_board_task_status(task_id, new_status):
    """Set status of a board task (JSON) by its id. Only tasks are drag-movable
    (status Kanban). Writes the same JSON schema agents read -> auto-synced."""
    if not task_id or not _BOARD_ID_RE.match(task_id):
        return False, "id task tidak valid"
    if new_status not in TASK_STATUS:
        return False, f"status tidak sah: {new_status}"
    if not BOARD_DIR.is_dir():
        return False, "board tidak ditemukan"
    # find the task file (only inside a tasks/ folder, by id), board-wide but not archive
    target = None
    for f in BOARD_DIR.rglob(f"{task_id}*.json"):
        if f.parent.name == "tasks" and "archive" not in f.relative_to(BOARD_DIR).parts:
            target = f; break
    if target is None:
        return False, "task tidak ditemukan (aktif)"
    try:
        t = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"JSON rusak: {e}"
    t["status"] = new_status
    t["updated"] = now_iso()
    target.write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, f"{task_id} -> {new_status}"


def _find_task_file(task_id, include_archive=True):
    """Resolve a task id -> its JSON file path (board-wide, incl. archive).
    Exact match on filename stem, not glob prefix (TASK-001 must not match TASK-0010.json)."""
    if not BOARD_DIR.is_dir():
        return None
    for f in BOARD_DIR.rglob(f"{task_id}*.json"):
        if f.stem != task_id or f.parent.name != "tasks":
            continue
        in_archive = "archive" in f.relative_to(BOARD_DIR).parts
        if in_archive and not include_archive:
            continue
        return f
    return None

def op_board_task_get(task_id):
    if not _BOARD_ID_RE.match(task_id or ""):
        return False, "id task tidak valid"
    f = _find_task_file(task_id)
    if f is None:
        return False, "task tidak ditemukan"
    try:
        t = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"JSON rusak: {e}"
    t["_archived"] = "archive" in f.relative_to(BOARD_DIR).parts
    t["_sprint"] = next((p for p in f.relative_to(BOARD_DIR).parts if p.startswith("sprint-")), "")
    return True, t

def op_board_task_edit(task_id, fields):
    """Edit editable fields of a task JSON (title/description/status/assignee/acceptance)."""
    if not _BOARD_ID_RE.match(task_id or ""):
        return False, "id task tidak valid"
    f = _find_task_file(task_id, include_archive=False)
    if f is None:
        return False, "task aktif tidak ditemukan"
    try:
        t = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"JSON rusak: {e}"
    if fields.get("status") and fields["status"] not in TASK_STATUS:
        return False, f"status tidak sah: {fields['status']}"
    if fields.get("assignee") and fields["assignee"] not in ("kirocrew", "agy"):
        return False, "assignee harus kirocrew/agy"
    for k in ("title", "description", "status", "assignee"):
        if k in fields and fields[k] != "":
            t[k] = fields[k]
    if fields.get("acceptance"):
        t["acceptance"] = [l.strip() for l in fields["acceptance"].splitlines() if l.strip()]
    t["updated"] = now_iso()
    f.write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, f"{task_id} tersimpan"

def op_board_task_delete(task_id):
    if not _BOARD_ID_RE.match(task_id or ""):
        return False, "id task tidak valid"
    f = _find_task_file(task_id, include_archive=False)
    if f is None:
        return False, "task aktif tidak ditemukan"
    TRASH.mkdir(exist_ok=True)
    dest = TRASH / f"{task_id}.{int(datetime.datetime.now().timestamp())}.json"
    shutil.move(str(f), str(dest))
    return True, f"{task_id} dihapus (ke .trash)"

# ---------- board notes (research / product-research / governance / root docs / addenda) -----
# NOT Scrum artifacts: no id/status lifecycle, not covered by validate.py.
# Grouped read+CRUD view for documents that exist in board/ but never appear
# on the Kanban (its BOARD_STAGES is deliberately Scrum-only).
NOTE_GROUPS = {
    "research": ("📚 Research", BOARD_DIR / "research"),
    "product-research": ("📈 Product Research", BOARD_DIR / "product-research"),
    "governance": ("🏛 Governance", BOARD_DIR / "governance"),
}
# root-level docs worth surfacing (explicit allowlist -- not every stray file,
# e.g. .gitignore/validate.py/sprint-manager.py are code/config, not notes)
NOTE_ROOT_FILES = ["RULES.md", "AGENTS.md", "TEMPLATES.md", "MIGRATION-PLAN-tahap2.md",
                    "BOARD_BUG_AUDIT_REPORT.md", "AUDIT_SUMMARY_FOR_LEAD.txt"]
_NOTE_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_ADDENDUM_RE = re.compile(r"^#{1,6}\s*(ADDENDUM.*)$", re.I | re.M)

def _note_id(group, fname):
    return f"{group}::{fname}"

def _note_path(note_id):
    """note_id -> Path, exact match only, confined to BOARD_DIR (no traversal)."""
    if "::" not in (note_id or ""):
        return None
    group, fname = note_id.split("::", 1)
    if not _NOTE_ID_RE.match(fname):
        return None
    if group == "root":
        if fname not in NOTE_ROOT_FILES:
            return None
        return BOARD_DIR / fname
    if group not in NOTE_GROUPS:
        return None
    return NOTE_GROUPS[group][1] / fname

def load_notes():
    """Scan note groups -> list of {id, group, group_label, fname, title, created,
    mtime, size, addenda:[...]}. Reads disk every call, same no-cache contract as
    load_board(). Sorted DESC by created within each group (grouping happens at
    render time; this just guarantees stable newest-first order per group)."""
    items = []
    groups = dict(NOTE_GROUPS)
    for fname in NOTE_ROOT_FILES:
        p = BOARD_DIR / fname
        if p.is_file():
            items.append(_note_item("root", "🗂 Root Docs", p))
    for gkey, (glabel, gdir) in groups.items():
        if not gdir.is_dir():
            continue
        for p in sorted(gdir.glob("*.md")) + sorted(gdir.glob("*.txt")):
            if p.name == "README.md":
                continue  # folder index, not a note -- same filter as load_board()'s README skip
            items.append(_note_item(gkey, glabel, p))
    items.sort(key=lambda it: it["created"], reverse=True)
    return items

def _note_item(gkey, glabel, p):
    text = p.read_text(encoding="utf-8", errors="replace")
    fm = _board_fm(text)
    title = fm.get("title") or p.stem.replace("-", " ")
    addenda = [m.group(1).strip() for m in _ADDENDUM_RE.finditer(text)]
    mtime = p.stat().st_mtime
    # created_at: prefer explicit front-matter `created:` (set by op_note_create/edit),
    # fall back to file mtime for the 29+ pre-existing research docs that have none.
    created = fm.get("created") or datetime.datetime.fromtimestamp(mtime).isoformat()
    return {"id": _note_item_id(gkey, p.name), "group": gkey, "group_label": glabel,
            "fname": p.name, "title": title, "created": created, "mtime": mtime,
            "size": p.stat().st_size, "addenda": addenda}

def _note_item_id(gkey, fname):
    return _note_id(gkey, fname)

def op_note_get(note_id):
    p = _note_path(note_id)
    if p is None or not p.is_file():
        return False, "dokumen tidak ditemukan"
    text = p.read_text(encoding="utf-8", errors="replace")
    fm = _board_fm(text)
    body = re.sub(r"^---\s*\n.*?\n---\s*\n?", "", text, count=1, flags=re.S)
    return True, {"id": note_id, "title": fm.get("title") or p.stem.replace("-", " "),
                  "raw": body, "html": md_to_html(body),
                  "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}

def op_note_create(group, title, content):
    if group not in NOTE_GROUPS:
        return False, "grup tidak dikenal (pilih research/product-research/governance)"
    title = (title or "Untitled note").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "note"
    gdir = NOTE_GROUPS[group][1]
    gdir.mkdir(parents=True, exist_ok=True)
    fname = f"{slug}-{datetime.date.today().isoformat()}.md"
    p = gdir / fname
    if p.exists():
        return False, "file dengan nama ini sudah ada, ganti judul"
    fm = f"---\ntitle: \"{title}\"\ncreated: {now_iso()}\n---\n\n"
    p.write_text(fm + (content or ""), encoding="utf-8")
    return True, _note_id(group, fname)

def op_note_edit(note_id, title, content):
    p = _note_path(note_id)
    if p is None or not p.is_file():
        return False, "dokumen tidak ditemukan"
    if note_id.split("::", 1)[0] == "root":
        return False, "root doc bersifat view-only dari tab ini (edit manual di filesystem)"
    existing = _board_fm(p.read_text(encoding="utf-8", errors="replace"))
    title = (title or existing.get("title") or p.stem).strip()
    fm = f"---\ntitle: \"{title}\"\ncreated: {existing.get('created', now_iso())}\nupdated: {now_iso()}\n---\n\n"
    p.write_text(fm + (content or ""), encoding="utf-8")
    return True, f"{p.name} tersimpan"

def op_note_delete(note_id):
    p = _note_path(note_id)
    if p is None or not p.is_file():
        return False, "dokumen tidak ditemukan"
    if note_id.split("::", 1)[0] == "root":
        return False, "root doc tidak bisa dihapus dari tab ini"
    TRASH.mkdir(exist_ok=True)
    dest = TRASH / f"{p.name}.{int(datetime.datetime.now().timestamp())}"
    shutil.move(str(p), str(dest))
    return True, f"{p.name} dihapus (ke .trash)"

def render_notes_section():
    """Research & Notes as a collapsible sub-section of the front-page index
    (not a separate page) -- same <details>/<summary> dropdown pattern as the
    artifact folder tree, same .card look as the index, sorted DESC by created
    within each group. Search integrates with the index's existing search box
    via the same data-s attribute _card_html() cards use."""
    items = load_notes()
    order = ["root"] + list(NOTE_GROUPS.keys())
    labels = {"root": "🗂 Root Docs", **{k: v[0] for k, v in NOTE_GROUPS.items()}}
    by_group = {k: [] for k in order}
    for it in items:
        by_group.setdefault(it["group"], []).append(it)

    def _card(it):
        addbadge = f' <span class="badge" style="background:#fef3c7;color:#92400e">+{len(it["addenda"])} addendum</span>' if it["addenda"] else ""
        is_root = it["group"] == "root"
        robadge = ' <span class="badge pin" title="view-only">🔒</span>' if is_root else ""
        when = (it["created"] or "")[:10]
        meta = f'{when} · {it["size"]//1024 or 1} KB'
        sk = _html.escape((it["title"] + " " + it["fname"]).lower())
        root_attr = ' data-root="1"' if is_root else ""
        return (f'<div class="card" data-s="{sk}" data-id="{_html.escape(it["id"])}"{root_attr}>'
                f'<div class="body"><a class="title" href="#" onclick="openNote(this.closest(\'.card\'));return false">'
                f'{_html.escape(it["title"])}</a>{addbadge}{robadge}'
                f'<div class="meta"><span class="badge md">{_html.escape(it["fname"])}</span> {_html.escape(meta)}</div></div></div>')

    groups_html = []
    for gkey in order:
        its = by_group.get(gkey, [])
        groups_html.append(
            f'<details class="folder" data-folder="notes-{gkey}" open>'
            f'<summary class="grp-h">​<span class="arrow"></span>{labels[gkey]}<span class="cnt">{len(its)}</span></summary>'
            f'<div class="grp-body">{"".join(_card(it) for it in its) or "<div class=empty>Belum ada dokumen.</div>"}</div></details>')

    html = (f'<details class="folder" id="notes-root" data-folder="📚 Research &amp; Notes" open>'
            f'<summary class="grp-h"><span class="arrow"></span>📚 Research &amp; Notes<span class="cnt">{len(items)}</span>'
            f'<span class="facts noprint"><button class="btn-reset" style="padding:2px 8px;font-size:12px" '
            f'onclick="event.preventDefault();event.stopPropagation();newNote()">＋ Catatan baru</button></span></summary>'
            f'<div class="grp-body" style="margin-left:12px">'
            f'<p class="meta" style="margin:2px 0 10px">📌 Dokumen riset/addendum/governance -- bukan artefak Scrum, tidak divalidasi validate.py. 🔒 = Root Docs, view-only.</p>'
            f'{"".join(groups_html)}</div></details>')
    return html

NOTES_MODAL_JS = """
function _esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function _nmodal(title,html){
  var m=document.getElementById('nmodal');
  if(!m){m=document.createElement('div');m.id='nmodal';m.className='modal';m.innerHTML='<div class="modal-box"><h3 id="nmt"></h3><div id="nmb"></div></div>';document.body.appendChild(m);
    m.addEventListener('click',function(e){if(e.target===m)closeNModal();});}
  document.getElementById('nmt').textContent=title;document.getElementById('nmb').innerHTML=html;m.classList.add('open');
}
function closeNModal(){var m=document.getElementById('nmodal');if(m)m.classList.remove('open');}
function openNote(card){
  var id=card.dataset.id, isRoot=card.dataset.root==='1';
  fetch('/api/note?id='+encodeURIComponent(id)).then(function(r){return r.json();}).then(function(r){
    if(!r.ok){toast('✗ '+r.msg);return;}
    var n=r.note;
    var html='<div style="max-height:55vh;overflow:auto;font-size:14px;line-height:1.55;margin-bottom:10px">'+n.html+'</div>'
      +'<div class="modal-actions">'
      +(isRoot?'':'<button class="btn-reset" onclick="_editNote('+"'"+_esc(id)+"'"+')">✏️ Edit</button>'
        +'<button class="btn-reset" style="color:#dc2626" onclick="_delNote('+"'"+_esc(id)+"'"+')">🗑 Hapus</button>')
      +'<button class="btn-reset" onclick="closeNModal()">Tutup</button></div>';
    _nmodal(n.title+(isRoot?' (view-only)':'')+' · '+n.mtime, html);
    window._curNote={id:id, title:n.title, raw:n.raw};
  }).catch(function(){toast('Error jaringan');});
}
function _editNote(id){
  var n=window._curNote;if(!n)return;
  var html='<label>Judul</label><input id="en_title" value="'+_esc(n.title)+'">'
    +'<label>Isi (markdown)</label><textarea id="en_body" style="min-height:50vh;font-family:ui-monospace,monospace;font-size:13px">'+_esc(n.raw)+'</textarea>'
    +'<div class="modal-actions"><button class="btn-reset" onclick="closeNModal()">Batal</button>'
    +'<button class="btn-reset" onclick="_saveNote('+"'"+_esc(id)+"'"+')">💾 Simpan</button></div>';
  _nmodal('Edit '+n.title, html);
}
function _saveNote(id){
  var fd=new FormData();fd.append('id',id);
  fd.append('title',document.getElementById('en_title').value);
  fd.append('content',document.getElementById('en_body').value);
  fetch('/api/note/edit',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(r){
    toast(r.ok?('✓ '+r.msg):('✗ '+r.msg));if(r.ok){closeNModal();setTimeout(function(){location.reload();},500);}})
    .catch(function(){toast('Error jaringan');});
}
function _delNote(id){
  if(!confirm('Hapus dokumen ini? (dipindah ke .trash, bisa dipulihkan manual)'))return;
  var fd=new FormData();fd.append('id',id);
  fetch('/api/note/delete',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(r){
    toast(r.ok?('✓ '+r.msg):('✗ '+r.msg));if(r.ok){closeNModal();setTimeout(function(){location.reload();},500);}})
    .catch(function(){toast('Error jaringan');});
}
function newNote(){
  var html='<label>Grup</label><select id="nn_group">'
    +'<option value="research">Research</option><option value="product-research">Product Research</option>'
    +'<option value="governance">Governance</option></select>'
    +'<label>Judul</label><input id="nn_title" placeholder="Judul dokumen">'
    +'<label>Isi (markdown, opsional)</label><textarea id="nn_body" style="min-height:40vh;font-family:ui-monospace,monospace;font-size:13px"></textarea>'
    +'<div class="modal-actions"><button class="btn-reset" onclick="closeNModal()">Batal</button>'
    +'<button class="btn-reset" onclick="_createNote()">💾 Buat</button></div>';
  _nmodal('Catatan baru', html);
}
function _createNote(){
  var fd=new FormData();
  fd.append('group',document.getElementById('nn_group').value);
  fd.append('title',document.getElementById('nn_title').value);
  fd.append('content',document.getElementById('nn_body').value);
  fetch('/api/note/create',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(r){
    toast(r.ok?('✓ dibuat'):('✗ '+r.msg));if(r.ok){closeNModal();setTimeout(function(){location.reload();},500);}})
    .catch(function(){toast('Error jaringan');});
}
"""



class H(BaseHTTPRequestHandler):
    def _send(self, body, ct="text/html; charset=utf-8", code=200):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), ct="application/json", code=code)

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if path in ("/", ""):
            return self._send(render_index())
        if path == "/board":
            qs = parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
            view = (qs.get("view") or ["status"])[0]
            sprint = (qs.get("sprint") or [""])[0]
            archived = (qs.get("archived") or [""])[0] in ("1", "true", "yes")
            return self._send(render_board("stage" if view=="stage" else "status", sprint, archived))
        if path == "/api/note":
            qs = parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
            ok, r = op_note_get((qs.get("id") or [""])[0])
            return self._json({"ok": ok, "note": r if ok else None, "msg": "" if ok else r})
        if path == "/api/board/task":
            qs = parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
            tid = (qs.get("id") or [""])[0]
            ok, r = op_board_task_get(tid)
            return self._json({"ok": ok, "task": r if ok else None, "msg": "" if ok else r})
        if path == "/api/bridge/health":
            return self._json({"ok": True, **bridge_health()})
        if path == "/api/bridge/events":
            return self._json({"ok": True, "events": bridge_events()})
        if path == "/api/bridge/interval":
            qs = parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
            return self._json({"ok": True, "minutes": bridge_interval_get((qs.get("unit") or ["agy-bridge"])[0])})
        if path == "/api/board/report":
            qs = parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
            rid = (qs.get("id") or [""])[0]
            h = board_report_html(rid)
            return self._json({"ok": h is not None, "html": h or "", "msg": "" if h else "report tak ditemukan"})
        m = re.match(r"^/edit/artifact/(.+)$", path)
        if m:
            return self._send(render_editor(m.group(1)))
        m = re.match(r"^/view/(artifact|doc)/(.+)$", path)
        if m:
            src, aid = m.group(1), m.group(2); a = read_artifact(src, aid)
            if not a: return self._send(shell("Not found", '<div class="empty">Tidak ditemukan.</div>'), code=404)
            if a["kind"] == "md":
                inner = f'<div class="content">{md_to_html(a["raw"])}</div>'
                edit = f'<a class="btn n noprint" href="/edit/{src}/{quote(aid)}" style="margin-left:8px">✎ Edit</a>' if src=="artifact" else ""
                head = f'<header style="display:none"></header>'
                page = shell(a["name"], f'<div class="treebar noprint"><a class="btn" href="/pdf/{src}/{quote(aid)}">⬇ PDF</a>{edit}</div>'+inner)
                return self._send(page)
            else:
                return self._send(wrap_widget(a["name"], a["raw"], aid if src=="artifact" else ""))
        m = re.match(r"^/pdf/(artifact|doc)/(.+)$", path)
        if m:
            src, aid = m.group(1), m.group(2); a = read_artifact(src, aid)
            if not a: return self._send(shell("Not found", '<div class="empty">Tidak ditemukan.</div>'), code=404)
            inner = f'<div class="content">{md_to_html(a["raw"]) if a["kind"]=="md" else a["raw"]}</div>'
            page = shell(a["name"], inner).replace("</body>", "<script>window.addEventListener('load',function(){setTimeout(function(){window.print()},400)})</script></body>")
            return self._send(page)
        return self._send(shell("Not found", '<div class="empty">404</div>'), code=404)

    def _read_body(self):
        ln = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(ln) if ln else b""

    def _parse_form(self, body):
        """Parse multipart/form-data OR x-www-form-urlencoded -> dict.
        File fields become (filename, bytes); text fields become str."""
        ctype = self.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" in ctype:
            parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            return {k: v[0] for k, v in parsed.items()}
        m = re.search(r"boundary=(.+)", ctype)
        if not m: return {}
        boundary = ("--" + m.group(1).strip()).encode()
        out = {}
        for part in body.split(boundary):
            if not part or part in (b"--\r\n", b"--", b"\r\n"): continue
            if b"\r\n\r\n" not in part: continue
            head, data = part.split(b"\r\n\r\n", 1)
            data = data.rstrip(b"\r\n")
            hs = head.decode("utf-8", "replace")
            nm = re.search(r'name="([^"]+)"', hs)
            fn = re.search(r'filename="([^"]*)"', hs)
            if not nm: continue
            if fn:
                out[nm.group(1)] = (fn.group(1), data)
            else:
                out[nm.group(1)] = data.decode("utf-8", "replace")
        return out

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_body()
        form = self._parse_form(body)
        try:
            if path == "/api/board/retry":
                ok, r = board_task_retry(form.get("id",""))
                return self._json({"ok": ok, "msg": r})
            if path == "/api/bridge/interval":
                ok, r = bridge_interval_set(form.get("minutes","5"), form.get("unit","agy-bridge"))
                return self._json({"ok": ok, "msg": r})
            if path == "/api/create":
                ok, r = op_create_artifact(form.get("name","Untitled"), form.get("kind","md"),
                                           form.get("folder",""), form.get("content",""))
                return self._json({"ok": ok, "msg": "File dibuat" if ok else r, "id": r if ok else None})
            if path == "/api/mkdir":
                ok, r = op_create_folder(form.get("name","folder"), form.get("parent",""))
                return self._json({"ok": ok, "msg": "Folder dibuat"})
            if path == "/api/move":
                ok, r = op_move(form.get("id",""), form.get("folder",""))
                return self._json({"ok": ok, "msg": "Dipindahkan" if ok else r})
            if path == "/api/edit":
                ok, r = op_edit(form.get("id",""), form.get("content",""))
                return self._json({"ok": ok, "msg": r})
            if path == "/api/delete":
                ok, r = op_delete(form.get("id",""))
                return self._json({"ok": ok, "msg": "Dihapus (ke .trash)" if ok else r})
            if path == "/api/import":
                fn, data = form.get("file", ("", b""))
                if not fn: return self._json({"ok": False, "msg": "tidak ada file"}, code=400)
                ok, r = op_import(fn, data if isinstance(data, bytes) else data.encode(), form.get("folder",""))
                return self._json({"ok": ok, "msg": "Diimport" if ok else r})
            if path == "/api/board/status":
                ok, r = op_board_task_status(form.get("id",""), form.get("status",""))
                return self._json({"ok": ok, "msg": r})
            if path == "/api/board/edit":
                ok, r = op_board_task_edit(form.get("id",""), {
                    "title": form.get("title",""), "description": form.get("description",""),
                    "status": form.get("status",""), "assignee": form.get("assignee",""),
                    "acceptance": form.get("acceptance","")})
                return self._json({"ok": ok, "msg": r})
            if path == "/api/board/delete":
                ok, r = op_board_task_delete(form.get("id",""))
                return self._json({"ok": ok, "msg": r})
            if path == "/api/note/create":
                ok, r = op_note_create(form.get("group",""), form.get("title",""), form.get("content",""))
                return self._json({"ok": ok, "msg": "dibuat" if ok else r, "id": r if ok else None})
            if path == "/api/note/edit":
                ok, r = op_note_edit(form.get("id",""), form.get("title",""), form.get("content",""))
                return self._json({"ok": ok, "msg": r})
            if path == "/api/note/delete":
                ok, r = op_note_delete(form.get("id",""))
                return self._json({"ok": ok, "msg": r})
        except Exception as e:
            return self._json({"ok": False, "msg": f"error: {e}"}, code=500)
        return self._json({"ok": False, "msg": "unknown endpoint"}, code=404)

    def log_message(self, *a): pass

class S(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True; daemon_threads = True

# idle-exit so systemd socket-activation can stop us when nobody is reading
import os as _os, time as _time, threading as _threading
IDLE_TIMEOUT = int(_os.environ.get("ARTIFACT_IDLE_TIMEOUT", "300"))  # 0 = never
_last_hit = _time.monotonic()
_orig_handle = H.handle
def _handle(self, *a, **k):
    globals().__setitem__("_last_hit", _time.monotonic())
    return _orig_handle(self, *a, **k)
H.handle = _handle

def _idle_watchdog(srv):
    while True:
        _time.sleep(5)
        if IDLE_TIMEOUT and (_time.monotonic() - _last_hit) > IDLE_TIMEOUT:
            srv.shutdown(); return

if __name__ == "__main__":
    import socket as _socket
    listen_fds = int(_os.environ.get("LISTEN_FDS", "0"))
    if listen_fds >= 1:
        # systemd socket-activation: adopt fd 3 (SD_LISTEN_FDS_START)
        srv = S.__new__(S)
        S.__init__(srv, (HOST, PORT), H, bind_and_activate=False)
        srv.socket = _socket.socket(fileno=3)
    else:
        srv = S((HOST, PORT), H)
        print(f"Artifact manager on http://{HOST}:{PORT}")
    _threading.Thread(target=_idle_watchdog, args=(srv,), daemon=True).start()
    srv.serve_forever(poll_interval=1)
