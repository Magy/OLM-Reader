#!/usr/bin/env python3
"""OLM Reader — fast version with SQLite/FTS5 cache and paginated API."""

import sys, os, zipfile, sqlite3, json, threading, webbrowser, hashlib, time, mimetypes
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote
from datetime import datetime

# ── SQLite schema ─────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS emails (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    folder     TEXT    NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    subject    TEXT    NOT NULL DEFAULT '',
    from_addr  TEXT    NOT NULL DEFAULT '',
    to_addr    TEXT    NOT NULL DEFAULT '',
    cc         TEXT    NOT NULL DEFAULT '',
    date_fmt   TEXT    NOT NULL DEFAULT '',
    date_raw   TEXT    NOT NULL DEFAULT '',
    has_html       INTEGER NOT NULL DEFAULT 0,
    has_attachment INTEGER NOT NULL DEFAULT 0,
    source     TEXT    NOT NULL DEFAULT '',
    zip_path   TEXT    NOT NULL DEFAULT '',
    olm_file   TEXT    NOT NULL DEFAULT '',
    body_html  TEXT    NOT NULL DEFAULT '',
    body_plain TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_folder   ON emails(folder);
CREATE INDEX IF NOT EXISTS idx_sent     ON emails(sent);
CREATE INDEX IF NOT EXISTS idx_date_raw ON emails(date_raw DESC);
CREATE INDEX IF NOT EXISTS idx_att      ON emails(has_attachment);

CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject, from_addr, to_addr, body_plain,
    content='emails', content_rowid='id',
    tokenize='unicode61'
);
-- FTS index is rebuilt manually after bulk insert (no trigger needed)
"""

SENT_KEYWORDS = {"sent", "inviati", "posta inviata", "sent items", "sent mail", "inviata"}

# ── parsing helpers ───────────────────────────────────────────────────────────

def parse_date(raw):
    if not raw:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            pass
    return raw.strip()

def _local_tag(el):
    """Strip XML namespace from tag name: {ns}Tag → Tag"""
    t = el.tag
    return t.split("}")[-1] if "}" in t else t

def find_text(root, *tags):
    """Find first element matching any local tag name (namespace-agnostic)."""
    tag_set = set(tags)
    for el in root.iter():
        if _local_tag(el) in tag_set and el.text and el.text.strip():
            return el.text.strip()
    return ""

def find_node(root, local_tag):
    """Find first element by local tag name (namespace-agnostic)."""
    for el in root.iter():
        if _local_tag(el) == local_tag:
            return el
    return None

def addr_text(node):
    if node is None:
        return ""
    parts = []
    for el in node.iter():
        if _local_tag(el) == "emailAddress":
            name = el.get("OPFContactEmailAddressName", "")
            mail = el.get("OPFContactEmailAddressAddress", "")
            if mail:
                parts.append(f"{name} <{mail}>" if name else mail)
    return ", ".join(parts)

def parse_email_xml(data, folder, my_email=""):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    body_html  = find_text(root, "OPFMessageCopyHTMLBody", "htmlBody")
    body_plain = find_text(root, "OPFMessageCopyBody", "plainBody", "textBody")

    # Namespace-agnostic fallback: scan all elements for body-like content
    if not body_html and not body_plain:
        for el in root.iter():
            if not el.text or len(el.text.strip()) < 20:
                continue
            tag = _local_tag(el).lower()
            if "htmlbody" in tag or tag == "htmlbody":
                body_html = el.text.strip(); break
            elif "body" in tag or "plainbody" in tag:
                body_plain = el.text.strip()

    date_raw  = find_text(root, "OPFMessageCopySentTime", "OPFMessageCopyReceivedTime")
    from_addr = addr_text(find_node(root, "OPFMessageCopyFromAddresses"))
    to_addr   = addr_text(find_node(root, "OPFMessageCopyToAddresses"))
    cc        = addr_text(find_node(root, "OPFMessageCopyCCAddresses"))

    sent = (
        any(k in folder.lower() for k in SENT_KEYWORDS)
        or bool(my_email and my_email.lower() in from_addr.lower())
    )

    has_attachment_xml = (
        find_text(root, "OPFMessageCopyHasAttachments") in ("1", "true", "True")
        or find_node(root, "OPFMessageCopyAttachmentList") is not None
        or find_node(root, "OPFMessageCopyAttachmentNames") is not None
    )

    return dict(
        folder         = folder,
        sent           = int(sent),
        subject        = find_text(root, "OPFMessageCopySubject") or "(nessun oggetto)",
        from_addr      = from_addr,
        to_addr        = to_addr,
        cc             = cc,
        date_fmt       = parse_date(date_raw),
        date_raw       = date_raw,
        has_html       = int(bool(body_html)),
        has_attachment = int(has_attachment_xml),
        source         = "",
        zip_path       = "",
        olm_file       = "",
        body_html      = body_html,
        body_plain     = body_plain,
    )

# ── thread-local ZipFile pool ─────────────────────────────────────────────────

_local = threading.local()

def _worker(olm_path, name, folder, my_email, has_att_hint, source=""):
    if not hasattr(_local, "zf") or _local.zf.filename != olm_path:
        _local.zf = zipfile.ZipFile(olm_path, "r")
    try:
        data = _local.zf.read(name)
        result = parse_email_xml(data, folder, my_email)
        if result:
            if has_att_hint:
                result["has_attachment"] = 1
            result["source"]   = source
            result["zip_path"] = name
            result["olm_file"] = olm_path
        return result
    except Exception:
        return None

# ── indexing ──────────────────────────────────────────────────────────────────

def _combined_fingerprint(paths):
    h = hashlib.md5()
    for p in sorted(paths):
        stat = os.stat(p)
        h.update(f"{p}:{stat.st_size}:{stat.st_mtime}".encode())
    return h.hexdigest()

def cache_path(olm_paths):
    cache_dir = os.path.expanduser("~/.cache/olm_reader")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, _combined_fingerprint(olm_paths) + ".db")

def _index_one_olm(olm_path, con, my_email, n_workers, t0_global, file_label):
    """Index a single OLM file into an open DB connection. Returns (n_html, n_plain, n_empty)."""
    source = os.path.basename(olm_path)

    with zipfile.ZipFile(olm_path, "r") as zf:
        all_names = zf.namelist()

    att_xml_paths = set()
    for n in all_names:
        if n.endswith(".xml") or "/com.microsoft.__Messages/" not in n:
            continue
        idx = n.index("/com.microsoft.__Messages/")
        prefix = n[:idx + len("/com.microsoft.__Messages/")]
        rest   = n[len(prefix):]
        parts  = rest.split("/")
        if len(parts) >= 2 and parts[0]:
            att_xml_paths.add(prefix + parts[0] + ".xml")

    entries = []
    for name in all_names:
        if not name.endswith(".xml") or "/com.microsoft.__Messages/" not in name:
            continue
        parts = name.split("/com.microsoft.__Messages/")[0].split("/")
        folder = " / ".join(
            p for p in parts if p and not p.startswith("com.microsoft")
        ) or "Posta in arrivo"
        entries.append((name, folder))

    total = len(entries)
    att_count = sum(1 for name, _ in entries if name in att_xml_paths)
    print(f"\n  {file_label} {source}: {total} email, {att_count} con allegati")

    INSERT_SQL = """INSERT INTO emails
        (folder,sent,subject,from_addr,to_addr,cc,date_fmt,date_raw,has_html,has_attachment,source,zip_path,olm_file,body_html,body_plain)
        VALUES (:folder,:sent,:subject,:from_addr,:to_addr,:cc,:date_fmt,:date_raw,:has_html,:has_attachment,:source,:zip_path,:olm_file,:body_html,:body_plain)"""

    batch, done = [], 0
    n_html, n_plain, n_empty = 0, 0, 0

    def flush(b):
        if b:
            with con:
                con.executemany(INSERT_SQL, b)

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = {
            ex.submit(_worker, olm_path, name, folder, my_email, name in att_xml_paths, source): (name, folder)
            for name, folder in entries
        }
        for future in as_completed(futures):
            result = future.result()
            done += 1
            if result:
                batch.append(result)
                if result["body_html"]:    n_html  += 1
                elif result["body_plain"]: n_plain += 1
                else:                      n_empty += 1
            if len(batch) >= 500:
                flush(batch); batch = []
            if done % 500 == 0 or done == total:
                pct = done * 100 // total
                elapsed = time.time() - t0_global
                print(f"\r    [{pct:3d}%] {done}/{total}  ({elapsed:.0f}s totali)", end="", flush=True)

    flush(batch)
    print(f"\r    Corpi: {n_html} HTML, {n_plain} testo, {n_empty} vuoti.          ")
    return n_html, n_plain, n_empty

def build_index(olm_paths, db_path, my_email=""):
    print(f"Primo avvio: indicizzazione di {len(olm_paths)} archivi…")
    t0 = time.time()

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('olm_paths', ?)", ("|".join(olm_paths),))
    con.commit()

    n_workers = min(8, (os.cpu_count() or 2) + 2)

    for i, olm_path in enumerate(olm_paths, 1):
        _index_one_olm(olm_path, con, my_email, n_workers, t0, f"[{i}/{len(olm_paths)}]")

    print("  Costruzione indice di ricerca…", flush=True)
    with con:
        con.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")

    con.execute("INSERT OR REPLACE INTO meta VALUES ('indexed_at', ?)", (str(time.time()),))
    if my_email:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('my_email', ?)", (my_email,))
    con.commit()
    con.close()

    elapsed = time.time() - t0
    print(f"  Indicizzazione completata in {elapsed:.1f}s")

def open_db(db_path):
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA query_only=ON")
    return con

# ── global state ──────────────────────────────────────────────────────────────

DB: sqlite3.Connection = None
DB_LOCK = threading.Lock()

def query(sql, params=()):
    with DB_LOCK:
        return DB.execute(sql, params).fetchall()

def query_one(sql, params=()):
    with DB_LOCK:
        return DB.execute(sql, params).fetchone()

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<title>OLM Reader</title>
<style>
* { box-sizing:border-box; margin:0; padding:0; }
body {
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  font-size:13px; background:#f0f0f0;
  height:100vh; display:flex; flex-direction:column;
}
#topbar {
  background:#2c2c2e; color:#fff; padding:8px 14px;
  display:flex; align-items:center; gap:10px; flex-shrink:0;
}
#topbar h1 { font-size:15px; font-weight:600; white-space:nowrap; }
#search {
  flex:1; padding:5px 10px; border-radius:6px; border:none;
  font-size:13px; background:#3a3a3c; color:#fff; outline:none;
}
#search::placeholder { color:#888; }
#count { color:#aaa; font-size:12px; white-space:nowrap; }
#main { display:flex; flex:1; overflow:hidden; }

/* list */
#list-pane {
  width:270px; background:#fff; border-right:1px solid #ddd;
  overflow-y:auto; flex-shrink:0; display:flex; flex-direction:column;
}
.filter-tabs { display:flex; border-bottom:1px solid #e0e0e0; flex-shrink:0; }
.tab {
  flex:1; padding:7px 4px; text-align:center; cursor:pointer;
  font-size:12px; color:#555; border-bottom:2px solid transparent;
}
.tab:hover { background:#f5f5f7; }
.tab.active { color:#007aff; border-bottom-color:#007aff; font-weight:600; }
#adv-filters {
  padding:7px 10px; border-bottom:1px solid #e0e0e0; background:#fafafa;
  display:flex; flex-direction:column; gap:5px; flex-shrink:0;
}
.adv-row { display:flex; align-items:center; gap:5px; }
.adv-label { font-size:11px; color:#888; width:22px; flex-shrink:0; }
.adv-input {
  flex:1; padding:3px 7px; border:1px solid #d0d0d0; border-radius:5px;
  font-size:12px; outline:none;
}
.adv-input:focus { border-color:#007aff; }
.adv-check { display:flex; align-items:center; gap:5px; font-size:12px; color:#444; cursor:pointer; }
.adv-check input { width:14px; height:14px; cursor:pointer; }
#email-list { overflow-y:auto; flex:1; }
.email-item { padding:9px 12px; border-bottom:1px solid #f0f0f0; cursor:pointer; }
.email-item:hover { background:#f5f5f7; }
.email-item.active { background:#ddeeff; }
.ei-top { display:flex; justify-content:space-between; align-items:baseline; gap:6px; }
.ei-from {
  font-weight:600; color:#111;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; flex:1;
}
.ei-date { color:#999; font-size:11px; white-space:nowrap; }
.ei-subject {
  color:#444; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  margin-top:2px; font-size:12px;
}
.badge {
  display:inline-block; font-size:10px; padding:1px 5px;
  border-radius:4px; margin-top:3px; font-weight:600;
}
.badge-sent { background:#fff3cd; color:#856404; }
.badge-recv { background:#d1e7dd; color:#0f5132; }
#load-more {
  padding:10px; text-align:center; cursor:pointer;
  color:#007aff; font-size:12px; border-top:1px solid #eee;
  flex-shrink:0;
}
#load-more:hover { background:#f5f5f7; }

/* detail */
#detail-pane {
  flex:1; overflow-y:auto; background:#fafafa; padding:20px 24px;
  display:flex; flex-direction:column;
}
#detail-pane h2 { font-size:16px; margin-bottom:10px; color:#111; }
.meta { color:#555; margin-bottom:5px; line-height:1.6; font-size:13px; }
.meta b { color:#111; }
#body-frame { margin-top:14px; border-top:1px solid #e5e5e5; padding-top:14px; flex:1; }
#body-frame iframe { width:100%; height:600px; border:none; background:#fff; }
#body-frame pre { white-space:pre-wrap; font-family:inherit; color:#222; line-height:1.6; }

/* right folder sidebar */
#sidebar {
  width:195px; background:#e8e8ed; border-left:1px solid #ccc;
  overflow-y:auto; padding:8px 0; flex-shrink:0;
}
.sidebar-title {
  font-size:11px; font-weight:700; color:#888; text-transform:uppercase;
  padding:6px 12px 4px; letter-spacing:.05em;
}
.folder-item {
  padding:5px 10px; cursor:pointer; border-radius:5px; margin:1px 4px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:12px;
  display:flex; align-items:center; gap:5px;
}
.folder-item:hover { background:#d5d5db; }
.folder-item.active { background:#007aff; color:#fff; }
.folder-count { margin-left:auto; font-size:11px; opacity:.6; }
.empty { color:#aaa; padding:20px; text-align:center; font-size:13px; }
</style>
</head>
<body>
<div id="topbar">
  <h1>📬 OLM Reader</h1>
  <input id="search" type="text" placeholder="Cerca oggetto, mittente, corpo…" oninput="debSearch(this.value)">
  <span id="count"></span>
</div>
<div id="main">
  <div id="list-pane">
    <div class="filter-tabs">
      <div class="tab active" id="tab-all"  onclick="setTab('all')">Tutte</div>
      <div class="tab"       id="tab-recv" onclick="setTab('recv')">Ricevute</div>
      <div class="tab"       id="tab-sent" onclick="setTab('sent')">Inviate</div>
    </div>
    <div id="adv-filters">
      <div class="adv-row">
        <span class="adv-label">Da</span>
        <input class="adv-input" id="filter-from" type="text" placeholder="mittente…" oninput="debAdv()">
      </div>
      <div class="adv-row">
        <span class="adv-label">A</span>
        <input class="adv-input" id="filter-to" type="text" placeholder="destinatario…" oninput="debAdv()">
      </div>
      <label class="adv-check">
        <input type="checkbox" id="filter-att" onchange="debAdv()"> Solo con allegati 📎
      </label>
    </div>
    <div id="email-list"></div>
    <div id="load-more" style="display:none" onclick="loadMore()">Carica altre…</div>
  </div>
  <div id="detail-pane"><p class="empty">Seleziona una email</p></div>
  <div id="sidebar">
    <div class="sidebar-title">Cartelle</div>
    <div id="folder-list"></div>
  </div>
</div>

<script>
const PER_PAGE = 50;
let currentFolder="__all__", currentTab="all", currentQuery="";
let currentFrom="", currentTo="", currentAtt=false;
let currentPage=0, total=0, emails=[];
let searchTimer=null, advTimer=null;

async function init() {
  const res = await fetch("/api/folders");
  const folders = await res.json();
  buildFolderSidebar(folders);
  await loadPage(0, true);
}

// ── folders ──────────────────────────────────────────────────────────────────
function buildFolderSidebar(folders) {
  const totalCount = folders.reduce((s,f) => s+f.count, 0);
  const fl = document.getElementById("folder-list");
  fl.innerHTML = folderItem("__all__", "📁 Tutte le email", totalCount) +
    folders.map(f => folderItem(f.name, "📂 " + f.name, f.count)).join("");
}

function folderItem(key, label, n) {
  const safe = key.replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\'");
  return `<div class="folder-item" data-key="${esc(key)}" onclick="setFolder('${safe}')">
    <span style="overflow:hidden;text-overflow:ellipsis">${esc(label)}</span>
    <span class="folder-count">${n}</span>
  </div>`;
}

function setFolder(folder) {
  currentFolder = folder;
  document.querySelectorAll(".folder-item").forEach(el =>
    el.classList.toggle("active", el.dataset.key === folder));
  loadPage(0, true);
}

// ── tabs ─────────────────────────────────────────────────────────────────────
function setTab(tab) {
  currentTab = tab;
  ["all","recv","sent"].forEach(t =>
    document.getElementById("tab-"+t).classList.toggle("active", t===tab));
  loadPage(0, true);
}

// ── search ───────────────────────────────────────────────────────────────────
function debSearch(q) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { currentQuery = q; loadPage(0, true); }, 250);
}

function debAdv() {
  clearTimeout(advTimer);
  advTimer = setTimeout(() => {
    currentFrom = document.getElementById("filter-from").value.trim();
    currentTo   = document.getElementById("filter-to").value.trim();
    currentAtt  = document.getElementById("filter-att").checked;
    loadPage(0, true);
  }, 300);
}

// ── load page ────────────────────────────────────────────────────────────────
async function loadPage(page, reset) {
  const params = new URLSearchParams({
    folder: currentFolder, tab: currentTab,
    q: currentQuery, from_q: currentFrom, to_q: currentTo,
    att: currentAtt ? "1" : "0",
    page, per_page: PER_PAGE
  });
  const res  = await fetch("/api/emails?" + params);
  const data = await res.json();
  total = data.total;
  currentPage = page;

  if (reset) emails = data.emails;
  else emails = emails.concat(data.emails);

  document.getElementById("count").textContent = total + " email";
  renderList();
}

function loadMore() { loadPage(currentPage + 1, false); }

// ── list ─────────────────────────────────────────────────────────────────────
function renderList() {
  const el = document.getElementById("email-list");
  if (!emails.length) { el.innerHTML='<p class="empty">Nessun risultato</p>'; }
  else {
    el.innerHTML = emails.map((e,i) => {
      const badge = e.sent
        ? '<span class="badge badge-sent">Inviata</span>'
        : '<span class="badge badge-recv">Ricevuta</span>';
      const attBadge = e.has_attachment ? ' <span style="font-size:12px">📎</span>' : '';
      const from = e.sent ? ("A: "+(e.to_addr||"?")) : (e.from_addr||"(sconosciuto)");
      return `<div class="email-item" onclick="showEmail(${i})" id="ei-${i}">
        <div class="ei-top">
          <div class="ei-from">${esc(from)}</div>
          <div class="ei-date">${esc(e.date_fmt)}</div>
        </div>
        <div class="ei-subject">${esc(e.subject)}</div>
        ${badge}${attBadge}
      </div>`;
    }).join("");
  }
  const lm = document.getElementById("load-more");
  lm.style.display = (emails.length < total) ? "block" : "none";
  if (emails.length < total)
    lm.textContent = `Carica altre… (${emails.length}/${total})`;
}

// ── detail ────────────────────────────────────────────────────────────────────
async function showEmail(idx) {
  document.querySelectorAll(".email-item").forEach(el => el.classList.remove("active"));
  const item = document.getElementById("ei-"+idx);
  if (item) item.classList.add("active");

  const e = emails[idx];
  const badge = e.sent
    ? '<span class="badge badge-sent" style="font-size:12px;padding:2px 8px">Inviata</span>'
    : '<span class="badge badge-recv" style="font-size:12px;padding:2px 8px">Ricevuta</span>';

  document.getElementById("detail-pane").innerHTML = `
    <h2>${esc(e.subject)} ${badge} <span style="font-size:11px;color:#aaa;font-weight:normal">id:${e.id}</span></h2>
    <div class="meta"><b>Da:</b> ${esc(e.from_addr)}</div>
    <div class="meta"><b>A:</b> ${esc(e.to_addr)}</div>
    ${e.cc ? `<div class="meta"><b>CC:</b> ${esc(e.cc)}</div>` : ""}
    <div class="meta"><b>Data:</b> ${esc(e.date_fmt)}</div>
    <div class="meta"><b>Cartella:</b> ${esc(e.folder)}</div>
    ${e.source ? `<div class="meta"><b>Archivio:</b> ${esc(e.source)}</div>` : ""}
    ${e.has_attachment ? '<div id="att-list" class="meta">📎 Caricamento allegati…</div>' : ""}
    <div id="body-frame"><p class="empty">Caricamento…</p></div>`;

  if (e.has_attachment) {
    fetch("/api/attachments/" + e.id).then(r => r.json()).then(atts => {
      const el = document.getElementById("att-list");
      if (!el) return;
      if (!atts.length) { el.textContent = "📎 (allegati non trovati nel ZIP)"; return; }
      el.innerHTML = "📎 <b>Allegati:</b> " + atts.map(a => a.available
        ? `<a href="/api/attachment/${e.id}/${a.path}?name=${a.dl||''}" download="${esc(a.name)}"
              style="margin-left:8px;color:#007aff;text-decoration:none;border:1px solid #cce;
                     border-radius:4px;padding:1px 7px;font-size:12px">
             ${esc(a.name)} <span style="color:#aaa;font-size:10px">${fmtSize(a.size)}</span>
           </a>`
        : `<span style="margin-left:8px;color:#aaa;border:1px solid #ddd;
                        border-radius:4px;padding:1px 7px;font-size:12px;cursor:default"
                title="non disponibile nell'archivio">
             ${esc(a.name)} <span style="font-size:10px">(non disponibile)</span>
           </span>`
      ).join("");
    });
  }

  if (e.has_html) {
    const iframe = document.createElement("iframe");
    iframe.sandbox = "allow-same-origin";
    iframe.style.cssText = "width:100%;height:600px;border:none;background:#fff";
    document.getElementById("body-frame").innerHTML = "";
    document.getElementById("body-frame").appendChild(iframe);
    iframe.src = "/api/body/" + e.id;
  } else {
    const res = await fetch("/api/body/" + e.id);
    const text = await res.text();
    document.getElementById("body-frame").innerHTML = text
      ? `<pre>${esc(text)}</pre>`
      : '<p class="empty">(corpo non disponibile)</p>';
  }
}

function esc(s) {
  return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
                      .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function fmtSize(b) {
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b/1024).toFixed(0) + " KB";
  return (b/1048576).toFixed(1) + " MB";
}

init();
</script>
</body>
</html>"""

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, ct):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        def q1(k, default=""):
            return qs.get(k, [default])[0]

        path = parsed.path

        if path == "/":
            self.send_bytes(HTML.encode(), "text/html; charset=utf-8")

        elif path == "/api/folders":
            rows = query(
                "SELECT folder, COUNT(*) AS count FROM emails GROUP BY folder ORDER BY folder"
            )
            self.send_json([{"name": r["folder"], "count": r["count"]} for r in rows])

        elif path == "/api/emails":
            folder  = q1("folder", "__all__")
            tab     = q1("tab",    "all")
            search  = q1("q",      "").strip()
            to_q    = q1("to_q",   "").strip()
            from_q  = q1("from_q", "").strip()
            att     = q1("att",    "0").strip()
            page    = max(0, int(q1("page",     "0")))
            per     = min(200, max(1, int(q1("per_page", "50"))))
            offset  = page * per

            conditions = []
            params     = []

            if folder != "__all__":
                conditions.append("e.folder = ?"); params.append(folder)
            if tab == "sent":
                conditions.append("e.sent = 1")
            elif tab == "recv":
                conditions.append("e.sent = 0")
            if to_q:
                conditions.append("e.to_addr LIKE ?"); params.append(f"%{to_q}%")
            if from_q:
                conditions.append("e.from_addr LIKE ?"); params.append(f"%{from_q}%")
            if att == "1":
                conditions.append("e.has_attachment = 1")

            COLS = "e.id,e.folder,e.sent,e.subject,e.from_addr,e.to_addr,e.cc,e.date_fmt,e.date_raw,e.has_html,e.has_attachment,e.source"

            if search:
                and_cond = ("AND " + " AND ".join(conditions)) if conditions else ""
                fts_sql = f"""
                    SELECT {COLS}
                    FROM emails_fts f JOIN emails e ON e.id = f.rowid
                    WHERE emails_fts MATCH ? {and_cond}
                    ORDER BY e.date_raw DESC LIMIT ? OFFSET ?
                """
                count_sql = f"""
                    SELECT COUNT(*) FROM emails_fts f JOIN emails e ON e.id=f.rowid
                    WHERE emails_fts MATCH ? {and_cond}
                """
                fts_params = [search] + params
                rows  = query(fts_sql,  fts_params + [per, offset])
                total = query_one(count_sql, fts_params)[0]
            else:
                where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
                rows  = query(
                    f"SELECT e.id,e.folder,e.sent,e.subject,e.from_addr,e.to_addr,e.cc,e.date_fmt,e.date_raw,e.has_html,e.has_attachment,e.source "
                    f"FROM emails e {where} ORDER BY e.date_raw DESC LIMIT ? OFFSET ?",
                    params + [per, offset]
                )
                total = query_one(f"SELECT COUNT(*) FROM emails e {where}", params)[0]

            self.send_json({
                "total": total, "page": page, "per_page": per,
                "emails": [dict(r) for r in rows],
            })

        elif path.startswith("/api/body/"):
            try:
                eid = int(path.split("/api/body/")[1])
            except ValueError:
                self.send_response(404); self.end_headers(); return
            row = query_one(
                "SELECT has_html, body_html, body_plain FROM emails WHERE id=?", (eid,)
            )
            if not row:
                self.send_response(404); self.end_headers(); return
            if row["has_html"]:
                self.send_bytes(row["body_html"].encode(), "text/html; charset=utf-8")
            else:
                self.send_bytes(row["body_plain"].encode(), "text/plain; charset=utf-8")

        elif path.startswith("/api/rawxml/"):
            try:
                eid = int(path.split("/api/rawxml/")[1])
            except ValueError:
                self.send_response(400); self.end_headers(); return
            row = query_one("SELECT zip_path, olm_file FROM emails WHERE id=?", (eid,))
            if not row:
                self.send_response(404); self.end_headers(); return
            try:
                with zipfile.ZipFile(row["olm_file"], "r") as zf:
                    data = zf.read(row["zip_path"])
                self.send_bytes(data, "text/xml; charset=utf-8")
            except Exception as ex:
                self.send_bytes(str(ex).encode(), "text/plain")

        elif path.startswith("/api/debug-att/"):
            try:
                eid = int(path.split("/api/debug-att/")[1])
            except ValueError:
                self.send_response(400); self.end_headers(); return
            row = query_one("SELECT zip_path, olm_file FROM emails WHERE id=?", (eid,))
            if not row:
                self.send_json({"error": "not found"}); return
            zip_path = row["zip_path"]
            parent   = zip_path.rsplit("/", 1)[0] + "/"
            att_dir  = parent + "com.microsoft.__Attachments/"
            try:
                with zipfile.ZipFile(row["olm_file"], "r") as zf:
                    xml_data = zf.read(zip_path)
                    root = ET.fromstring(xml_data)
                    msg_id = find_text(root, "OPFMessageCopyMessageID")
                    att_els = [
                        {"name": el.get("OPFAttachmentName",""),
                         "url":  el.get("OPFAttachmentURL",""),
                         "ext":  el.get("OPFAttachmentContentExtension",""),
                         "size": el.get("OPFAttachmentContentFileSize","")}
                        for el in root.iter() if _local_tag(el) == "messageAttachment"
                    ]
                    att_files = sorted([
                        {"path": zi.filename, "size": zi.file_size}
                        for zi in zf.infolist()
                        if zi.filename.startswith(att_dir) and not zi.filename.endswith("/")
                    ], key=lambda x: x["path"])
                    candidates = [att_dir + msg_id + f"_{i:04d}" for i in range(len(att_els))] if msg_id else []
                self.send_json({"zip_path": zip_path, "att_dir": att_dir,
                                "msg_id": msg_id, "att_elements": att_els,
                                "candidates": candidates, "att_files": att_files[:30]})
            except Exception as ex:
                self.send_json({"error": str(ex)})

        elif path.startswith("/api/debug/"):
            try:
                eid = int(path.split("/api/debug/")[1])
            except ValueError:
                self.send_response(400); self.end_headers(); return
            row = query_one("SELECT zip_path, olm_file, has_attachment FROM emails WHERE id=?", (eid,))
            if not row:
                self.send_json({"error": "not found"}); return
            zip_path = row["zip_path"]
            att_prefix = zip_path[:-4] + "/" if zip_path else ""
            parent = zip_path.rsplit("/", 1)[0] + "/" if "/" in zip_path else ""
            result = {"zip_path": zip_path, "olm_file": row["olm_file"],
                      "has_attachment": row["has_attachment"],
                      "att_prefix_searched": att_prefix}
            try:
                with zipfile.ZipFile(row["olm_file"], "r") as zf:
                    nearby = [zi.filename for zi in zf.infolist()
                              if zi.filename.startswith(parent)][:50]
                result["nearby_entries"] = nearby
            except Exception as ex:
                result["error"] = str(ex)
            self.send_json(result)

        elif path.startswith("/api/attachments/"):
            try:
                eid = int(path.split("/api/attachments/")[1])
            except ValueError:
                self.send_response(404); self.end_headers(); return
            row = query_one("SELECT zip_path, olm_file FROM emails WHERE id=?", (eid,))
            if not row or not row["zip_path"]:
                self.send_json([]); return
            try:
                zip_path = row["zip_path"]
                olm_file = row["olm_file"]
                parent   = zip_path.rsplit("/", 1)[0] + "/"
                att_dir  = parent + "com.microsoft.__Attachments/"

                with zipfile.ZipFile(olm_file, "r") as zf:
                    xml_data = zf.read(zip_path)
                    root = ET.fromstring(xml_data)

                    att_elements = [el for el in root.iter()
                                    if _local_tag(el) == "messageAttachment"]
                    msg_id = find_text(root, "OPFMessageCopyMessageID")

                    # index of physical files in __Attachments/ for this folder
                    att_files = {zi.filename: zi.file_size
                                 for zi in zf.infolist()
                                 if zi.filename.startswith(att_dir)
                                    and not zi.filename.endswith("/")
                                    and zi.file_size > 0}

                    infos, used = [], set()
                    for i, el in enumerate(att_elements):
                        name = el.get("OPFAttachmentName", "") or f"allegato_{i}"
                        ext  = el.get("OPFAttachmentContentExtension", "")
                        url  = el.get("OPFAttachmentURL", "")

                        found_path = None
                        # msg_id may have angle brackets; filenames strip them
                        raw_id = (msg_id or "").strip("<>")

                        if url:
                            # try as-is, then relative to parent folder
                            for u in [url, parent + url]:
                                if u in att_files:
                                    found_path = u
                                    break

                        if not found_path and raw_id:
                            # try all known naming conventions in order
                            for candidate in [
                                att_dir + raw_id + f"_{i:04d}",          # indexed no-ext
                                att_dir + raw_id + f".{ext}" if ext else None,  # with ext
                                att_dir + raw_id,                         # bare MessageID
                                att_dir + msg_id + f"_{i:04d}" if msg_id != raw_id else None,
                                att_dir + msg_id if msg_id != raw_id else None,
                            ]:
                                if candidate and candidate in att_files and candidate not in used:
                                    found_path = candidate
                                    break

                        if found_path:
                            infos.append({"name": name, "size": att_files[found_path],
                                          "path": quote(found_path, safe=""),
                                          "dl":   quote(name, safe=""),
                                          "available": True})
                            used.add(found_path)
                        else:
                            # file not in archive — show name but mark unavailable
                            infos.append({"name": name, "size": 0, "path": "",
                                          "dl": "", "available": False})

                self.send_json(infos)
            except Exception as ex:
                self.send_json({"error": str(ex)})

        elif path.startswith("/api/attachment/"):
            # serve one attachment: /api/attachment/<email_id>/<url-encoded zip path>
            rest = path[len("/api/attachment/"):]
            slash = rest.find("/")
            if slash < 0:
                self.send_response(400); self.end_headers(); return
            try:
                eid = int(rest[:slash])
            except ValueError:
                self.send_response(400); self.end_headers(); return
            # strip query string from zip_entry
            entry_and_qs = rest[slash+1:]
            if "?" in entry_and_qs:
                entry_part, qs_part = entry_and_qs.split("?", 1)
                name_param = parse_qs(qs_part).get("name", [None])[0]
            else:
                entry_part, name_param = entry_and_qs, None
            zip_entry = unquote(entry_part)
            row = query_one("SELECT olm_file FROM emails WHERE id=?", (eid,))
            if not row:
                self.send_response(404); self.end_headers(); return
            try:
                with zipfile.ZipFile(row["olm_file"], "r") as zf:
                    data = zf.read(zip_entry)
                fname = unquote(name_param) if name_param else zip_entry.split("/")[-1]
                ct, _ = mimetypes.guess_type(fname)
                ct = ct or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", len(data))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{fname}"')
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(404); self.end_headers()

        else:
            self.send_response(404); self.end_headers()

# ── main ──────────────────────────────────────────────────────────────────────

def _apply_my_email(db_path, my_email):
    """Update sent=1 for emails from my_email in an already-cached DB."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    stored = con.execute("SELECT value FROM meta WHERE key='my_email'").fetchone()
    if stored and stored[0] == my_email:
        con.close(); return  # already applied
    n = con.execute(
        "UPDATE emails SET sent=1 WHERE from_addr LIKE ? AND sent=0",
        (f"%{my_email}%",)
    ).rowcount
    con.execute("INSERT OR REPLACE INTO meta VALUES ('my_email', ?)", (my_email,))
    con.commit()
    con.close()
    if n:
        print(f"  Aggiornate {n} email come 'inviate' (from: {my_email})")

REQUIRED_COLS = {"has_attachment", "source", "zip_path", "olm_file"}

def main():
    args = sys.argv[1:]
    my_email = ""
    if "--me" in args:
        i = args.index("--me")
        if i + 1 < len(args):
            my_email = args[i + 1]
            args = args[:i] + args[i+2:]

    if not args:
        print("Uso: python3 olm_reader.py arch1.olm [arch2.olm …] [--me tua@email.com]")
        sys.exit(1)

    olm_paths = []
    for a in args:
        p = os.path.abspath(a)
        if not os.path.exists(p):
            print(f"File non trovato: {p}"); sys.exit(1)
        olm_paths.append(p)

    print(f"Archivi: {len(olm_paths)}")
    for p in olm_paths:
        print(f"  {os.path.basename(p)}")

    db_path = cache_path(olm_paths)

    if not os.path.exists(db_path):
        build_index(olm_paths, db_path, my_email)
    else:
        _con = sqlite3.connect(db_path)
        cols = {r[1] for r in _con.execute("PRAGMA table_info(emails)")}
        _con.close()
        if not REQUIRED_COLS.issubset(cols):
            missing = REQUIRED_COLS - cols
            print(f"Schema aggiornato ({', '.join(missing)}) — re-indicizzazione…")
            os.remove(db_path)
            build_index(olm_paths, db_path, my_email)
        else:
            print("Cache trovata — caricamento istantaneo.")
            if my_email:
                _apply_my_email(db_path, my_email)

    global DB
    DB = open_db(db_path)

    count = query_one("SELECT COUNT(*) FROM emails")[0]
    sent  = query_one("SELECT COUNT(*) FROM emails WHERE sent=1")[0]
    if my_email:
        print(f"  Mittente personale: {my_email}")
    print(f"  {count} email totali ({sent} inviate).")

    port = 8765
    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Apri il browser su {url}  (Ctrl+C per uscire)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        DB.close()
        print("\nChiuso.")

if __name__ == "__main__":
    main()
