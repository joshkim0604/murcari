"""
Murcari — Streamlit app.

Run locally:   streamlit run streamlit_app.py
Deploy:        push this folder to GitHub and deploy it on Streamlit Community Cloud.
               Put your API key in the app's Secrets (see secrets.toml.example).

Search, storage and alert logic live in murcari.py; this file is the interface.
"""
from __future__ import annotations

import html
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Murcari", page_icon="🛍️", layout="wide",
                   initial_sidebar_state="collapsed")

# Streamlit secrets -> environment variables, so murcari.py reads them the same way as .env
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, (str, int, float)):
            os.environ.setdefault(_k, str(_v))
except Exception:
    pass  # no secrets configured; a local .env may still work

import murcari as mw  # noqa: E402

mw.load_dotenv(mw.HERE / ".env")

INTERVAL_MIN = float(os.environ.get("CHECK_INTERVAL_MIN", 15))
RECENCY = os.environ.get("RECENCY", "d")
PASSWORD = os.environ.get("APP_PASSWORD", "")

# Mercari US listing photos follow this pattern. If a photo doesn't load,
# the card shows a plain tile instead.
PHOTO_URL = "https://u-mercari-images.mercdn.net/photos/{item_id}_1.jpg"


# --------------------------------------------------------------------------- #
# Look and feel
# --------------------------------------------------------------------------- #
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700;800&display=swap');

:root {
  --canvas: #FFFFFF;
  --ink: #222222;
  --muted: #6B6B6B;
  --brand: #5356EE;
  --brand-deep: #252B60;
  --violet: #7400B9;
  --line: #E6E6EA;
  --tile: #F2F2FB;
  --warn: #C2410C;
}

.stApp, .stApp *:not([data-testid="stIconMaterial"]):not(.material-symbols-rounded),
[data-testid="stToast"], [data-testid="stToast"] *:not([data-testid="stIconMaterial"]) {
  font-family: 'Figtree', 'Averta', system-ui, -apple-system, 'Segoe UI', Arial, sans-serif !important;
}
.stApp { background: var(--canvas); color: var(--ink); }
[data-testid="stHeader"] { background: transparent; height: 0; }
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 1120px; padding-top: 0; padding-bottom: 4rem;
}

/* Top bar */
.mu-bar {
  display: flex; align-items: center; gap: .75rem;
  padding: 1.1rem 0 1rem; margin-bottom: 1.5rem; border-bottom: 1px solid var(--line);
}
.mu-bar .mu-logo {
  font-weight: 800 !important; font-size: 2rem !important; letter-spacing: -0.035em; line-height: 1 !important;
  color: var(--brand-deep); margin: 0;
}
.mu-logo span { color: var(--brand); }
.mu-bar .mu-tagline { color: var(--muted); font-size: .95rem !important; margin: 0; padding-top: .45rem; }

/* Search form */
.st-key-hunt [data-testid="stWidgetLabel"] p { font-size: .9rem; font-weight: 600; color: var(--ink); }
.st-key-term input { font-size: 1.05rem; padding: .8rem 1rem; }
.st-key-maxp input { font-size: 1rem; padding: .8rem .9rem; }
.st-key-hunt [data-testid="stTextInputRootElement"],
.st-key-hunt [data-testid="stNumberInputContainer"] { border-radius: 8px; border-color: var(--line); background: #F5F5F7; }
.stButton button, [data-testid="stFormSubmitButton"] button { border-radius: 4px; font-weight: 600; }
/* Brand colors even if .streamlit/config.toml is missing */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
  background: var(--brand) !important; border-color: var(--brand) !important; color: #fff !important;
}
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover {
  background: #3F42D9 !important; border-color: #3F42D9 !important;
}
.st-key-hunt [data-testid="stFormSubmitButton"] button { min-height: 3.2rem; font-size: 1rem; }

/* Headings */
.mu-h { font-weight: 700; font-size: 1.2rem; color: var(--ink); margin: 2rem 0 .2rem; }
.mu-h .n { color: var(--muted); font-weight: 500; margin-left: .35rem; }
.mu-sub { color: var(--muted); font-size: .9rem; margin: 0 0 .6rem; }

/* Saved searches */
.mu-saved .t { font-weight: 600; font-size: 1rem; color: var(--ink); }
.mu-saved .s { color: var(--muted); font-size: .85rem; }
.mu-saved .s.err { color: var(--warn); }
[class*="st-key-rm_"] { width: 100% !important; }
[class*="st-key-rm_"], [class*="st-key-rm_"] .stButton { display: flex; justify-content: flex-end; }
[class*="st-key-rm_"] button { color: var(--muted); width: auto !important; }
[class*="st-key-rm_"] button:hover { color: var(--warn); }
.st-key-filter { margin-top: 1.25rem; }

/* Listing grid */
.mu-grid {
  display: grid; gap: 1.5rem 1rem; margin-top: .75rem;
  grid-template-columns: repeat(auto-fill, minmax(176px, 1fr));
}
.mu-card { display: block; color: var(--ink) !important; text-decoration: none !important; min-width: 0; }
.mu-card:focus-visible { outline: 3px solid var(--brand); outline-offset: 4px; border-radius: 8px; }
.mu-tile {
  position: relative; aspect-ratio: 1 / 1; border-radius: 8px; overflow: hidden;
  background: var(--tile); display: grid; place-items: center;
}
.mu-tile img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; color: transparent; }
/* Shown only when the photo fails to load */
.mu-tile img::before {
  content: ""; position: absolute; inset: 0; background: var(--tile) url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2048%2048%22%20fill%3D%22none%22%3E%3Cpath%20d%3D%22M6%208v14.3c0%201%20.4%202%201.2%202.8l17.7%2017.7a4%204%200%200%200%205.6%200l12.3-12.3a4%204%200%200%200%200-5.6L25.1%207.2A4%204%200%200%200%2022.3%206H8a2%202%200%200%200-2%202Z%22%20stroke%3D%22%23A9AAF6%22%20stroke-width%3D%223%22%20stroke-linejoin%3D%22round%22%2F%3E%3Ccircle%20cx%3D%2215%22%20cy%3D%2215%22%20r%3D%223.5%22%20stroke%3D%22%23A9AAF6%22%20stroke-width%3D%223%22%2F%3E%3C%2Fsvg%3E") center / 34% no-repeat;
}
.mu-card:hover .mu-tile img { transform: scale(1.03); transition: transform .25s ease; }
@media (prefers-reduced-motion: reduce) { .mu-card:hover .mu-tile img { transform: none; } }
.mu-price {
  position: absolute; left: 8px; bottom: 8px; padding: 4px 10px; border-radius: 999px;
  background: rgba(34, 34, 34, .78); color: #fff; font-weight: 700; font-size: .95rem;
  font-variant-numeric: tabular-nums;
}
.mu-new {
  position: absolute; left: 8px; top: 8px; padding: 3px 9px; border-radius: 4px;
  background: var(--violet); color: #fff; font-weight: 700; font-size: .75rem;
}
.mu-card.over .mu-tile { opacity: .55; }
.mu-title {
  font-size: .9rem; line-height: 1.35; margin: .55rem 0 .2rem; font-weight: 500;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.mu-card:hover .mu-title { color: var(--brand); }
.mu-meta { color: var(--muted); font-size: .8rem; margin: 0; }
.mu-meta.over { color: var(--warn); }

.mu-empty {
  color: var(--muted); font-size: 1rem; line-height: 1.55; margin: .5rem 0 0;
  padding: 2rem 1.25rem; background: #F7F7FA; border-radius: 8px; text-align: center;
}
.mu-foot { color: var(--muted); font-size: .8rem; margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line); }

[data-testid="stExpander"] details { border-color: var(--line); border-radius: 8px; }

@media (max-width: 640px) {
  .mu-bar { flex-direction: column; align-items: flex-start; gap: .2rem; }
  .mu-logo { font-size: 1.6rem; }
  .mu-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem .75rem; }
  [class*="st-key-rm_"], [class*="st-key-rm_"] .stButton { justify-content: flex-start; }
  [class*="st-key-rm_"] { margin-top: -.6rem; }
  [class*="st-key-rm_"] button { padding-left: 0; }
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)



def top_bar() -> None:
    st.markdown(
        '<div class="mu-bar"><div class="mu-logo" role="heading" aria-level="1">Mur<span>cari</span></div>'
        '<div class="mu-tagline">Get alerted when new Mercari listings match what you want.</div></div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Gatekeeping: optional password, and an API key
# --------------------------------------------------------------------------- #
if PASSWORD and not st.session_state.get("authed"):
    top_bar()
    _, mid, _ = st.columns([1, 2, 1])
    with mid, st.form("unlock", border=False):
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Unlock", type="primary", use_container_width=True):
            if pw == PASSWORD:
                st.session_state.authed = True
                st.rerun()
            st.error("That password isn't right.")
    st.stop()

env = os.environ
if not (env.get("SERPER_API_KEY") or env.get("SERPAPI_API_KEY")
        or (env.get("GOOGLE_API_KEY") and env.get("GOOGLE_CSE_ID"))):
    top_bar()
    st.error(
        "No search API key is set. Add one in your app's **Settings → Secrets** "
        "(or in `.streamlit/secrets.toml` when running on your computer), then reload:\n\n"
        '```toml\nSERPER_API_KEY = "your_key_here"\n```'
    )
    st.stop()


# --------------------------------------------------------------------------- #
# One shared watcher for the whole server (keeps running between page views)
# --------------------------------------------------------------------------- #
@st.cache_resource
def start_watcher():
    provider = mw.pick_provider()
    try:
        store = mw.Store(mw.HERE / "murcari.db")
    except sqlite3.OperationalError:  # read-only app folder
        store = mw.Store(Path(tempfile.gettempdir()) / "murcari.db")
    watcher = mw.Watcher(store, provider, INTERVAL_MIN, RECENCY, mw.Notifier())
    watcher.start()
    return store, watcher, provider.label


store, watcher, provider_label = start_watcher()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def money(p) -> str:
    if p is None:
        return ""
    return f"${p:,.0f}" if float(p).is_integer() else f"${p:,.2f}"


def ago(iso: str | None) -> str:
    if not iso:
        return "not yet"
    secs = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = int(secs // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400:
        h = int(secs // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(secs // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


def friendly_error(err: str) -> str:
    low = err.lower()
    if "401" in low or "403" in low or "unauthorized" in low or "invalid api key" in low:
        return "Last check failed: the search service rejected your API key. Check it in Secrets."
    if "429" in low or "quota" in low or "credits" in low or "limit" in low:
        return "Last check failed: you're out of API searches for now. Raise the check interval or top up your plan."
    return f"Last check failed: {err[:120]}"


def grid(items: list[dict], budgets: dict, show_term: bool) -> str:
    cards = []
    for l in items:
        budget = budgets.get(l["term"])
        over = bool(budget) and l["price"] is not None and l["price"] > budget
        photo = html.escape(PHOTO_URL.format(item_id=l["item_id"]))
        price = f'<span class="mu-price">{html.escape(money(l["price"]))}</span>' if l["price"] is not None else ""
        new = '<span class="mu-new">New</span>' if l["is_new"] else ""
        if over:
            meta = f'<p class="mu-meta over">Over your {html.escape(money(budget))} max</p>'
        else:
            when = f"Found {ago(l['first_seen'])}"
            if show_term:
                when += f" for “{html.escape(l['term'])}”"
            meta = f'<p class="mu-meta">{when}</p>'
        cards.append(
            f'<a class="mu-card{" over" if over else ""}" href="{html.escape(l["url"])}" '
            f'target="_blank" rel="noopener" title="{html.escape(l["title"])}">'
            f'<div class="mu-tile">'
            f'<img src="{photo}" alt="" loading="lazy" referrerpolicy="no-referrer">'
            f'{new}{price}</div>'
            f'<p class="mu-title">{html.escape(l["title"])}</p>{meta}</a>'
        )
    return '<div class="mu-grid">' + "".join(cards) + "</div>"


def add_search(term: str, max_price) -> None:
    store.add_search(term, max_price)
    threading.Thread(target=watcher.check, args=(term,), daemon=True).start()


def remove_search(term: str) -> None:
    store.remove_search(term)
    st.toast(f"Removed “{term}” from your saved searches.")


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
top_bar()

with st.form("hunt", clear_on_submit=True, border=False):
    c1, c2, c3 = st.columns([6, 2, 1.5], vertical_alignment="bottom")
    term = c1.text_input("What are you looking for?", placeholder="Search for anything, like quiksilver shirt",
                         key="term")
    max_price = c2.number_input("Max price", min_value=0.0, value=None, step=1.0,
                                format="%.0f", placeholder="Any price", key="maxp")
    submitted = c3.form_submit_button("Save search", type="primary", use_container_width=True)

if submitted:
    clean = " ".join(term.split())[:120]
    if clean:
        add_search(clean, max_price)
        st.toast(f"Saved “{clean}”. You'll be alerted about listings posted from now on.")
    else:
        st.toast("Type what you're looking for first.")


@st.fragment(run_every=15)
def live() -> None:
    searches = store.searches()
    budgets = {s["term"]: s["max_price"] for s in searches}
    listings = store.listings()

    # Pop-up for matches that arrived since this browser last refreshed
    max_seq = max((l["seq"] for l in listings), default=0)
    last_seq = st.session_state.get("last_seq")
    if last_seq is not None:
        for l in listings:
            if l["seq"] > last_seq and l["is_new"]:
                price = f" for {money(l['price'])}" if l["price"] is not None else ""
                st.toast(f"New listing: {l['title'][:70]}{price}", icon="🛍️")
    st.session_state.last_seq = max_seq

    if not searches:
        st.markdown(
            '<p class="mu-empty">Save a search above, like “quiksilver shirt”. '
            "Murcari checks Mercari for you and shows new listings here.</p>",
            unsafe_allow_html=True,
        )
        return

    # --- Saved searches ---
    mins = max(0, int((watcher.next_run - time.time()) // 60))
    next_check = "in under a minute" if mins < 1 else f"in {mins} minute{'s' if mins != 1 else ''}"
    per_day = int(len(searches) * 1440 / INTERVAL_MIN)
    h1, h2 = st.columns([5, 1.5], vertical_alignment="bottom")
    h1.markdown(
        f'<div class="mu-h">Saved searches<span class="n">{len(searches)}</span></div>'
        f'<p class="mu-sub">Checked every {INTERVAL_MIN:g} minutes. Next check {next_check}. '
        f'About {per_day} API searches a day.</p>',
        unsafe_allow_html=True,
    )
    h2.button("Check now", on_click=watcher.check_all_now, use_container_width=True, key="checknow")

    for s in searches:
        c1, c2 = st.columns([5, 1.5], vertical_alignment="center")
        bits = []
        if s["max_price"]:
            bits.append(f"Up to {money(s['max_price'])}.")
        if s["last_error"]:
            status = f'<span class="s err">{html.escape(friendly_error(s["last_error"]))}</span>'
        elif not s["baselined"]:
            status = '<span class="s">Checking for the first time…</span>'
        else:
            bits.append(f"Checked {ago(s['last_checked'])}.")
            status = ""
        lead = f'<span class="s">{html.escape(" ".join(bits))} </span>' if bits else ""
        c1.markdown(
            f'<div class="mu-saved"><div class="t">{html.escape(s["term"])}</div>{lead}{status}</div>',
            unsafe_allow_html=True,
        )
        c2.button("Remove", key=f"rm_{abs(hash(s['term']))}", type="tertiary",
                  on_click=remove_search, args=(s["term"],))

    # --- Listings ---
    show_term = len(searches) > 1
    if show_term:
        pick = st.pills("Show listings from", ["All saved searches"] + [s["term"] for s in searches],
                        default="All saved searches", key="filter")
        if pick and pick != "All saved searches":
            listings = [l for l in listings if l["term"] == pick]
            show_term = False

    fresh = [l for l in listings if l["is_new"]]
    existing = [l for l in listings if not l["is_new"]]

    st.markdown(
        f'<div class="mu-h">New listings<span class="n">{len(fresh) if fresh else ""}</span></div>',
        unsafe_allow_html=True,
    )
    if fresh:
        st.markdown(grid(fresh, budgets, show_term), unsafe_allow_html=True)
    else:
        st.markdown(
            '<p class="mu-empty">No new listings yet. They show up here once Google picks them up, '
            "usually a few hours after they're posted on Mercari.</p>",
            unsafe_allow_html=True,
        )

    if existing:
        with st.expander(f"Already listed when you saved the search ({len(existing)})"):
            st.markdown(grid(existing, budgets, show_term), unsafe_allow_html=True)


live()

st.markdown(
    f'<p class="mu-foot">Murcari is a personal tool and isn\'t affiliated with Mercari. '
    f"Results come from {html.escape(provider_label)}.</p>",
    unsafe_allow_html=True,
)
