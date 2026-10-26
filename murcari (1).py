#!/usr/bin/env python3
"""
Murcari — live alerts for new Mercari listings, found through Google search.

    python murcari.py "quiksilver shirt" "patagonia fleece"

Then open http://localhost:8765 (it opens automatically). You can also add and
remove searches from that page while it runs.

Search backends (set ONE in a .env file next to this script, or as env vars):
    SERPER_API_KEY=...                         -> Serper.dev  (Google results)
    SERPAPI_API_KEY=...                        -> SerpApi     (Google results)
    GOOGLE_API_KEY=...  +  GOOGLE_CSE_ID=...   -> Google Custom Search JSON API
                                                  (existing customers only; shuts down 2027-01-01)
Optional:
    NTFY_TOPIC=some-secret-topic-name          -> phone push alerts via the free ntfy app
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

HERE = Path(__file__).resolve().parent

# Mercari US item URLs look like https://www.mercari.com/us/item/m12345678901/
ITEM_RE = re.compile(r"https?://(?:www\.)?mercari\.com/(?:us/)?item/(m[0-9a-z]+)", re.I)
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=value per line) so no extra package is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Search providers — each returns a list of {title, link, snippet}
# --------------------------------------------------------------------------- #
class SearchError(Exception):
    pass


def _json_or_raise(resp: requests.Response) -> dict:
    if resp.status_code >= 400:
        raise SearchError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


class SerperProvider:
    label = "Serper.dev (Google results)"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, recency: str) -> list[dict]:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 10, "tbs": f"qdr:{recency}"},
            timeout=20,
        )
        data = _json_or_raise(resp)
        return [
            {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
            for r in data.get("organic", [])
        ]


class SerpApiProvider:
    label = "SerpApi (Google results)"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, recency: str) -> list[dict]:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "api_key": self.api_key,
                    "num": 10, "tbs": f"qdr:{recency}"},
            timeout=30,
        )
        data = _json_or_raise(resp)
        if "error" in data:
            if "hasn't returned any results" in data["error"]:
                return []
            raise SearchError(data["error"])
        return [
            {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
            for r in data.get("organic_results", [])
        ]


class GoogleCSEProvider:
    label = "Google Custom Search JSON API"

    def __init__(self, api_key: str, cx: str):
        self.api_key, self.cx = api_key, cx

    def search(self, query: str, recency: str) -> list[dict]:
        # CSE has no hour filter; the smallest window is one day.
        restrict = {"h": "d1", "d": "d1", "w": "w1", "m": "m1"}[recency]
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": self.api_key, "cx": self.cx, "q": query,
                    "num": 10, "dateRestrict": restrict},
            timeout=20,
        )
        data = _json_or_raise(resp)
        return [
            {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
            for r in data.get("items", [])
        ]


def pick_provider():
    choice = os.environ.get("SEARCH_PROVIDER", "").lower()
    serper = os.environ.get("SERPER_API_KEY")
    serpapi = os.environ.get("SERPAPI_API_KEY")
    gkey, gcx = os.environ.get("GOOGLE_API_KEY"), os.environ.get("GOOGLE_CSE_ID")

    if choice == "serper" or (not choice and serper):
        if serper:
            return SerperProvider(serper)
    if choice == "serpapi" or (not choice and serpapi):
        if serpapi:
            return SerpApiProvider(serpapi)
    if choice in ("google", "cse") or (not choice and gkey and gcx):
        if gkey and gcx:
            return GoogleCSEProvider(gkey, gcx)
    sys.exit(
        "No search API key found.\n"
        "Create a file named .env next to murcari.py containing ONE of:\n"
        "  SERPER_API_KEY=your_key            (sign up at serper.dev)\n"
        "  SERPAPI_API_KEY=your_key           (sign up at serpapi.com)\n"
        "  GOOGLE_API_KEY=...  and  GOOGLE_CSE_ID=...   (existing Google customers only)\n"
    )


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def build_query(term: str) -> str:
    return f"site:mercari.com/us/item {term}"


def clean_title(title: str) -> str:
    title = re.sub(r"\s*[|\-–]\s*Mercari.*$", "", title, flags=re.I)
    return title.strip() or "(untitled listing)"


def parse_price(text: str) -> float | None:
    m = PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Storage (SQLite)
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS searches (
                term TEXT PRIMARY KEY,
                max_price REAL,
                added_at TEXT,
                last_checked TEXT,
                last_error TEXT,
                baselined INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS listings (
                term TEXT,
                item_id TEXT,
                title TEXT,
                url TEXT,
                price REAL,
                snippet TEXT,
                first_seen TEXT,
                is_new INTEGER,
                PRIMARY KEY (term, item_id)
            );
            """
        )
        self.db.commit()

    def add_search(self, term: str, max_price: float | None) -> bool:
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO searches (term, max_price, added_at) VALUES (?, ?, ?)",
                (term, max_price, now_iso()),
            )
            if cur.rowcount == 0 and max_price is not None:
                self.db.execute("UPDATE searches SET max_price=? WHERE term=?", (max_price, term))
            self.db.commit()
            return cur.rowcount > 0

    def remove_search(self, term: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM searches WHERE term=?", (term,))
            self.db.execute("DELETE FROM listings WHERE term=?", (term,))
            self.db.commit()

    def get_search(self, term: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM searches WHERE term=?", (term,)).fetchone()
            return dict(row) if row else None

    def searches(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM searches ORDER BY added_at")]

    def record_check(self, term: str, error: str | None) -> None:
        with self.lock:
            if error:
                self.db.execute(
                    "UPDATE searches SET last_checked=?, last_error=? WHERE term=?",
                    (now_iso(), error, term),
                )
            else:
                self.db.execute(
                    "UPDATE searches SET last_checked=?, last_error=NULL, baselined=1 WHERE term=?",
                    (now_iso(), term),
                )
            self.db.commit()

    def insert_listing(self, term, item_id, title, url, price, snippet, is_new) -> bool:
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO listings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (term, item_id, title, url, price, snippet, now_iso(), 1 if is_new else 0),
            )
            self.db.commit()
            return cur.rowcount > 0

    def listings(self, limit: int = 300) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT rowid AS seq, * FROM listings ORDER BY rowid DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def _ascii(s: str) -> str:
    return s.encode("ascii", "ignore").decode() or "item"


class Notifier:
    def __init__(self):
        self.ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            from plyer import notification  # optional desktop popups
            self.desktop = notification
        except Exception:
            self.desktop = None

    def send(self, term: str, item: dict) -> None:
        price = f"${item['price']:,.2f}" if item.get("price") is not None else "price n/a"
        log(f"NEW  [{term}]  {item['title']}  —  {price}\n           {item['url']}")
        if self.ntfy_topic:
            try:
                requests.post(
                    f"{self.ntfy_server}/{self.ntfy_topic}",
                    data=f"{item['title']} — {price}".encode("utf-8"),
                    headers={
                        "Title": _ascii(f"Murcari: new listing for {term}"),
                        "Click": item["url"],
                        "Tags": "shopping_cart",
                    },
                    timeout=10,
                )
            except Exception as e:
                log(f"ntfy push failed: {e}")
        if self.desktop:
            try:
                self.desktop.notify(title=f"Murcari: {term}", message=f"{item['title'][:200]} — {price}",
                                    app_name="Murcari", timeout=10)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Watcher thread
# --------------------------------------------------------------------------- #
class Watcher(threading.Thread):
    def __init__(self, store: Store, provider, interval_min: float, recency: str, notifier: Notifier):
        super().__init__(daemon=True)
        self.store, self.provider = store, provider
        self.interval = max(1.0, interval_min) * 60
        self.recency, self.notifier = recency, notifier
        self.wake = threading.Event()
        self.next_run = time.time()

    def run(self) -> None:
        while True:
            for s in self.store.searches():
                self.check(s["term"])
            self.next_run = time.time() + self.interval
            self.wake.wait(self.interval)
            self.wake.clear()

    def check_all_now(self) -> None:
        self.wake.set()

    def check(self, term: str) -> None:
        s = self.store.get_search(term)
        if not s:
            return
        try:
            results = self.provider.search(build_query(term), self.recency)
        except Exception as e:
            log(f"Search failed for '{term}': {e}")
            self.store.record_check(term, str(e)[:300])
            return

        first_run = not s["baselined"]
        new_items = []
        for r in results:
            m = ITEM_RE.search(r.get("link", ""))
            if not m:
                continue
            item_id = m.group(1).lower()
            item = {
                "title": clean_title(r.get("title", "")),
                "url": f"https://www.mercari.com/us/item/{item_id}/",
                "price": parse_price(f"{r.get('title', '')} {r.get('snippet', '')}"),
            }
            inserted = self.store.insert_listing(
                term, item_id, item["title"], item["url"], item["price"],
                r.get("snippet", ""), is_new=not first_run,
            )
            if inserted and not first_run:
                new_items.append(item)

        self.store.record_check(term, None)
        if first_run:
            log(f"'{term}': saved {len(results)} existing results as a baseline. Watching for new ones.")
            return

        max_price = s.get("max_price")
        for item in new_items:
            if max_price and item["price"] is not None and item["price"] > max_price:
                continue
            self.notifier.send(term, item)
        if not new_items:
            log(f"'{term}': nothing new.")


# --------------------------------------------------------------------------- #
# Local dashboard
# --------------------------------------------------------------------------- #
DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Murcari</title>
<style>
:root{--bg:#ffffff;--card:#fff;--ink:#222222;--muted:#6b6b6b;--line:#e6e6ea;--accent:#5356ee;--new:#7400b9;--warn:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#15161c;--card:#1e1f27;--ink:#ececf1;--muted:#9a9aa6;--line:#2e2f3a;--accent:#8587ff;--new:#b05cf0;--warn:#f59e0b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 2px}.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
input{flex:1 1 220px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font-size:15px}
input.price{flex:0 1 130px}
button{padding:10px 14px;border:0;border-radius:10px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer;font-size:14px}
button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:18px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 8px 6px 12px;display:flex;align-items:center;gap:8px;font-size:13px}
.chip .meta{color:var(--muted)}.chip .err{color:var(--warn)}
.chip button{padding:2px 8px;background:transparent;color:var(--muted);border:0;font-size:15px}
.item{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px;display:flex;gap:12px;align-items:flex-start}
.item.dim{opacity:.5}.item .main{flex:1;min-width:0}
.item a{color:var(--ink);font-weight:600;text-decoration:none;word-break:break-word}.item a:hover{text-decoration:underline}
.item .snip{color:var(--muted);font-size:13px;margin-top:3px}
.item .row{font-size:12px;color:var(--muted);margin-top:6px;display:flex;gap:10px;flex-wrap:wrap}
.price{font-weight:700;white-space:nowrap}
.badge{font-size:11px;font-weight:700;color:#fff;background:var(--new);border-radius:6px;padding:1px 6px}
.empty{color:var(--muted);text-align:center;padding:40px 0}
</style></head><body><div class="wrap">
<h1>Murcari</h1>
<div class="sub" id="sub">Loading…</div>
<form id="add"><input id="term" placeholder='What do you want to buy? e.g. quiksilver shirt' required>
<input id="max" class="price" type="number" min="0" step="1" placeholder="Max $ (optional)">
<button>Watch</button></form>
<div class="bar"><button class="ghost" id="checknow">Check now</button>
<button class="ghost" id="alerts">Enable browser alerts</button>
<label style="font-size:13px;color:var(--muted)"><input type="checkbox" id="onlynew" style="flex:none"> Only show new</label></div>
<div class="chips" id="chips"></div>
<div id="list"></div>
</div>
<script>
let lastSeq = null;
const $ = s => document.querySelector(s);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const ago = iso => { if (!iso) return "never"; const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "just now"; if (s < 3600) return Math.floor(s/60) + "m ago";
  if (s < 86400) return Math.floor(s/3600) + "h ago"; return Math.floor(s/86400) + "d ago"; };
const money = p => p == null ? "" : "$" + Number(p).toLocaleString(undefined,{minimumFractionDigits:0,maximumFractionDigits:2});

async function api(path, body) {
  const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)} : {});
  return r.json();
}

function render(st) {
  $("#sub").textContent = `${st.provider} · checks every ${st.interval_min} min · ~${st.queries_per_day} searches/day · next check ${st.next_check_in}`;
  const chips = $("#chips"); chips.replaceChildren();
  const budgets = {};
  for (const s of st.searches) {
    budgets[s.term] = s.max_price;
    const c = el("div", "chip");
    c.append(el("strong", null, s.term));
    if (s.max_price) c.append(el("span", "meta", "≤ " + money(s.max_price)));
    c.append(s.last_error ? el("span", "err", "error: " + s.last_error.slice(0, 80)) : el("span", "meta", "checked " + ago(s.last_checked)));
    const x = el("button", null, "×"); x.title = "Stop watching";
    x.onclick = async () => { await api("/api/remove", {term: s.term}); refresh(); };
    c.append(x); chips.append(c);
  }
  const list = $("#list"); list.replaceChildren();
  const onlyNew = $("#onlynew").checked;
  const items = st.listings.filter(l => !onlyNew || l.is_new);
  if (!items.length) list.append(el("div", "empty", st.searches.length ? "No listings yet. New matches will appear here." : "Add a search above to start watching."));
  for (const l of items) {
    const over = budgets[l.term] && l.price != null && l.price > budgets[l.term];
    const card = el("div", "item" + (over ? " dim" : ""));
    const main = el("div", "main");
    const a = el("a", null, l.title); a.href = l.url; a.target = "_blank"; a.rel = "noopener";
    main.append(a);
    if (l.snippet) main.append(el("div", "snip", l.snippet));
    const row = el("div", "row");
    if (l.is_new) row.append(el("span", "badge", "NEW"));
    row.append(el("span", null, l.term), el("span", null, "found " + ago(l.first_seen)));
    if (over) row.append(el("span", null, "over budget"));
    main.append(row);
    card.append(main, el("div", "price", money(l.price)));
    list.append(card);
  }
  // Browser notifications for new matches since the last poll
  const maxSeq = st.listings.reduce((m, l) => Math.max(m, l.seq), 0);
  if (lastSeq !== null && "Notification" in window && Notification.permission === "granted") {
    for (const l of st.listings) {
      if (l.seq > lastSeq && l.is_new) {
        const n = new Notification("Murcari: new listing for " + l.term, {body: `${l.title} ${money(l.price)}`});
        n.onclick = () => window.open(l.url, "_blank");
      }
    }
  }
  lastSeq = maxSeq;
}

async function refresh() { try { render(await api("/api/state")); } catch (e) { $("#sub").textContent = "Lost connection to the watcher — is the script still running?"; } }

$("#add").onsubmit = async e => {
  e.preventDefault();
  const term = $("#term").value.trim(); if (!term) return;
  const max = $("#max").value ? Number($("#max").value) : null;
  await api("/api/searches", {term, max_price: max});
  $("#term").value = ""; $("#max").value = "";
  refresh(); setTimeout(refresh, 4000);
};
$("#checknow").onclick = async () => { await api("/api/check", {}); setTimeout(refresh, 4000); };
$("#alerts").onclick = () => { if ("Notification" in window) Notification.requestPermission().then(p => { $("#alerts").textContent = p === "granted" ? "Browser alerts on" : "Alerts blocked"; }); };
$("#onlynew").onchange = refresh;
if ("Notification" in window && Notification.permission === "granted") $("#alerts").textContent = "Browser alerts on";
refresh(); setInterval(refresh, 15000);
</script></body></html>"""


def make_handler(store: Store, watcher: Watcher, provider_label: str, interval_min: float):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console clean
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, DASHBOARD.encode(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                searches = store.searches()
                secs = max(0, int(watcher.next_run - time.time()))
                self._json({
                    "provider": provider_label,
                    "interval_min": interval_min,
                    "queries_per_day": int(len(searches) * 1440 / interval_min),
                    "next_check_in": f"in {secs // 60}m {secs % 60}s" if secs else "now",
                    "searches": searches,
                    "listings": store.listings(),
                })
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            data = self._body()
            if self.path == "/api/searches":
                term = " ".join(str(data.get("term", "")).split())[:120]
                if not term:
                    return self._json({"error": "empty search"}, 400)
                max_price = data.get("max_price")
                max_price = float(max_price) if max_price not in (None, "") else None
                store.add_search(term, max_price)
                log(f"Now watching: '{term}'" + (f" (max ${max_price:g})" if max_price else ""))
                threading.Thread(target=watcher.check, args=(term,), daemon=True).start()
                self._json({"ok": True})
            elif self.path == "/api/remove":
                store.remove_search(str(data.get("term", "")))
                self._json({"ok": True})
            elif self.path == "/api/check":
                watcher.check_all_now()
                self._json({"ok": True})
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    load_dotenv(HERE / ".env")

    ap = argparse.ArgumentParser(description="Murcari: live alerts for new Mercari listings via Google search.")
    ap.add_argument("searches", nargs="*", help='What to watch for, e.g. "quiksilver shirt"')
    ap.add_argument("--interval", type=float, default=15, help="Minutes between checks (default 15)")
    ap.add_argument("--recency", choices=["h", "d", "w", "m"], default="d",
                    help="Only Google results from the past hour/day/week/month (default d)")
    ap.add_argument("--max-price", type=float, default=None, help="Only alert at or below this price")
    ap.add_argument("--port", type=int, default=8765, help="Dashboard port (default 8765)")
    ap.add_argument("--db", default=str(HERE / "murcari.db"), help="Where to save seen listings")
    ap.add_argument("--no-browser", action="store_true", help="Don't open the dashboard automatically")
    args = ap.parse_args()

    provider = pick_provider()
    store = Store(Path(args.db))
    for term in args.searches:
        term = " ".join(term.split())
        if term:
            store.add_search(term, args.max_price)

    notifier = Notifier()
    watcher = Watcher(store, provider, args.interval, args.recency, notifier)

    n = len(store.searches())
    log(f"Using {provider.label}. Watching {n} search(es), checking every {args.interval:g} min "
        f"(~{int(n * 1440 / args.interval)} API searches/day).")
    if notifier.ntfy_topic:
        log(f"Phone alerts: ntfy topic '{notifier.ntfy_topic}'")
    if notifier.desktop:
        log("Desktop pop-ups: on")

    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(store, watcher, provider.label, args.interval))
    url = f"http://localhost:{args.port}"
    log(f"Dashboard: {url}   (Ctrl+C to stop)")
    watcher.start()
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopped.")


def _running_in_streamlit() -> bool:
    try:
        from streamlit.runtime import exists
        return exists()
    except Exception:
        return False


if __name__ == "__main__":
    if _running_in_streamlit():
        # Streamlit was pointed at this file: show the Murcari app instead of
        # starting the standalone server (which would leave the page blank).
        import runpy
        runpy.run_path(str(HERE / "streamlit_app.py"), run_name="__main__")
    else:
        main()
