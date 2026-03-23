"""
app.py — Air India Fare Scraper (all-in-one Streamlit app)
Scrapes Air India round-trip fares and exports to Excel.
Runs entirely on Streamlit — no separate backend needed.
"""

import io
import os
import sys
import time
import threading
import subprocess
import pandas as pd
import streamlit as st
from datetime import date, timedelta


# ── Install Playwright browsers at startup (needed on Streamlit Cloud / Render) ──
@st.cache_resource
def install_playwright():
    """Runs once per server start. Installs Chromium if not already present."""
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, capture_output=True
        )
        return True, "Chromium ready"
    except subprocess.CalledProcessError as e:
        return False, str(e.stderr)

chromium_ok, chromium_msg = install_playwright()


# ── Scraper (runs in background thread) ───────────────────────────────────────

def _safe_text(card, *selectors) -> str:
    for sel in selectors:
        el = card.query_selector(sel)
        if el:
            t = el.inner_text().strip()
            if t:
                return t
    return ""

def _parse_price(raw: str):
    if not raw:
        return None
    cleaned = raw.replace("₹", "").replace(",", "").strip().split()[0]
    try:
        return float(cleaned)
    except ValueError:
        return None

def _parse_stops(raw: str):
    if not raw:
        return None
    if "non" in raw.lower():
        return 0
    digits = "".join(c for c in raw if c.isdigit())
    return int(digits) if digits else None

def _date_range(start: date, end: date):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]

def _build_url(origin, destination, depart, ret, adults):
    base = "https://www.airindia.com/in/en/book/flight-search.html"
    return (
        f"{base}?tripType=R&origin={origin}&destination={destination}"
        f"&departureDate={depart}&returnDate={ret}"
        f"&adult={adults}&child=0&infant=0&cabinClass=ECONOMY"
    )

def run_scraper(job: dict):
    """
    Runs in a background thread.
    Updates job dict in place — Streamlit polls it via session_state.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    def log(msg):
        job["log"].append(msg)

    job["status"] = "running"
    results = []

    out_dates = _date_range(job["outbound_start"], job["outbound_end"])
    ret_dates = _date_range(job["return_start"],   job["return_end"])

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="en-IN",
            )
            page = ctx.new_page()

            log("Opening Air India homepage…")
            page.goto("https://www.airindia.com/in/en.html", timeout=30000)
            time.sleep(3)

            for dep in out_dates:
                for ret in ret_dates:
                    if ret < dep:
                        continue

                    dep_str = dep.strftime("%Y-%m-%d")
                    ret_str = ret.strftime("%Y-%m-%d")
                    url = _build_url(
                        job["origin"], job["destination"],
                        dep_str, ret_str, job["adults"]
                    )
                    log(f"Searching {dep_str} → return {ret_str} …")

                    try:
                        page.goto(url, timeout=60000)
                        page.wait_for_selector(
                            "div.flight-card, div[class*='flightCard'], div[class*='flight-result']",
                            timeout=35000,
                        )
                        time.sleep(random_delay())

                        cards = page.query_selector_all(
                            "div.flight-card, div[class*='flightCard'], div[class*='flight-result']"
                        )

                        if not cards:
                            log(f"  ⚠ No results for {dep_str} / {ret_str}")
                            continue

                        for card in cards:
                            try:
                                results.append({
                                    "Outbound Date":  dep_str,
                                    "Return Date":    ret_str,
                                    "Flight Number":  _safe_text(card, "[class*='flightNumber']", "[class*='flight-number']"),
                                    "Departure Time": _safe_text(card, "[class*='departureTime']", "[class*='depart-time']"),
                                    "Arrival Time":   _safe_text(card, "[class*='arrivalTime']",   "[class*='arrive-time']"),
                                    "Duration":       _safe_text(card, "[class*='duration']",      "[class*='flight-duration']"),
                                    "Stops":          _parse_stops(_safe_text(card, "[class*='stop']", "[class*='stops']")),
                                    "Price (INR)":    _parse_price(_safe_text(card, "[class*='price']", "[class*='fare']", "[class*='amount']")),
                                    "Airline":        "Air India",
                                })
                            except Exception as e:
                                log(f"  ⚠ Skipped one card: {e}")

                        log(f"  ✓ {len(cards)} flight(s) found")

                    except PWTimeout:
                        log(f"  ✗ Timed out for {dep_str} / {ret_str} — skipping")
                    except Exception as e:
                        log(f"  ✗ Error: {e}")

                    time.sleep(random_delay())

            browser.close()

    except Exception as e:
        log(f"❌ Fatal error: {e}")
        job["status"] = "error"
        return

    job["results"]  = results
    job["status"]   = "done"
    log(f"✅ Done — {len(results)} flights found.")


def random_delay():
    import random
    return random.uniform(3, 6)


def build_excel(results: list) -> bytes:
    df = pd.DataFrame(results)
    cheapest = (
        df.dropna(subset=["Price (INR)"])
          .sort_values("Price (INR)"))
    cheapest = (
        cheapest.groupby(["Outbound Date", "Return Date"], as_index=False)
                .first()
                .sort_values(["Outbound Date", "Return Date"])
    )
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="All Flights", index=False)
        cheapest.to_excel(writer, sheet_name="Cheapest Per Date Pair", index=False)
        for sheet in writer.sheets.values():
            for col in sheet.columns:
                w = max(len(str(c.value or "")) for c in col) + 4
                sheet.column_dimensions[col[0].column_letter].width = min(w, 30)
    return buf.getvalue()


# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Air India Fare Finder", page_icon="✈️", layout="centered")
st.title("✈️ Air India Fare Finder")
st.caption("Round-trip fare search across a date range — exports to Excel.")

if not chromium_ok:
    st.error(f"Chromium install failed: {chromium_msg}")
    st.stop()

# ── Session state init ─────────────────────────────────────────────────────────
for key, default in {
    "job": None,
    "excel_bytes": None,
    "last_log_len": 0,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

COMMON_ROUTES = {
    "Kolkata (CCU) → Delhi (DEL)":      ("CCU", "DEL"),
    "Kolkata (CCU) → Mumbai (BOM)":     ("CCU", "BOM"),
    "Kolkata (CCU) → Bangalore (BLR)":  ("CCU", "BLR"),
    "Delhi (DEL) → Mumbai (BOM)":       ("DEL", "BOM"),
    "Mumbai (BOM) → Goa (GOI)":         ("BOM", "GOI"),
    "Custom…": None,
}

# ── Input form ─────────────────────────────────────────────────────────────────
with st.form("search_form"):
    st.subheader("Route")
    route_choice = st.selectbox("Select a route", list(COMMON_ROUTES.keys()))
    if COMMON_ROUTES[route_choice] is None:
        c1, c2 = st.columns(2)
        origin      = c1.text_input("Origin IATA",      value="CCU").upper().strip()
        destination = c2.text_input("Destination IATA", value="DEL").upper().strip()
    else:
        origin, destination = COMMON_ROUTES[route_choice]
        st.info(f"**{origin}** → **{destination}**")

    st.subheader("Outbound dates")
    c3, c4 = st.columns(2)
    out_start = c3.date_input("From", value=date.today() + timedelta(days=7),  key="os")
    out_end   = c4.date_input("To",   value=date.today() + timedelta(days=10), key="oe")

    st.subheader("Return dates")
    c5, c6 = st.columns(2)
    ret_start = c5.date_input("From", value=date.today() + timedelta(days=14), key="rs")
    ret_end   = c6.date_input("To",   value=date.today() + timedelta(days=17), key="re")

    adults    = st.number_input("Adults", min_value=1, max_value=9, value=1)
    submitted = st.form_submit_button("🔍  Search Fares", use_container_width=True)

# ── On submit ──────────────────────────────────────────────────────────────────
if submitted:
    if out_end < out_start:
        st.error("Outbound end must be after outbound start.")
        st.stop()
    if ret_end < ret_start:
        st.error("Return end must be after return start.")
        st.stop()
    if ret_start < out_start:
        st.error("Return dates must start on or after outbound start.")
        st.stop()

    job = {
        "status":         "pending",
        "log":            ["Job started…"],
        "results":        [],
        "origin":         origin,
        "destination":    destination,
        "outbound_start": out_start,
        "outbound_end":   out_end,
        "return_start":   ret_start,
        "return_end":     ret_end,
        "adults":         adults,
    }
    st.session_state.job          = job
    st.session_state.excel_bytes  = None
    st.session_state.last_log_len = 0

    thread = threading.Thread(target=run_scraper, args=(job,), daemon=True)
    thread.start()
    st.rerun()

# ── Live progress polling ──────────────────────────────────────────────────────
job = st.session_state.job
if job and job["status"] in ("pending", "running"):
    st.subheader("Live progress")
    log_box = st.code("\n".join(job["log"]), language=None)
    st.spinner("Scraping in progress…")
    time.sleep(3)
    st.rerun()                          # re-runs every 3 s to pick up new log lines

# ── Results ────────────────────────────────────────────────────────────────────
if job and job["status"] == "done" and st.session_state.excel_bytes is None:
    st.session_state.excel_bytes = build_excel(job["results"])

if job and job["status"] in ("done", "error"):
    st.subheader("Progress log")
    st.code("\n".join(job["log"]), language=None)

if st.session_state.excel_bytes:
    st.success(f"✅ Found {len(job['results'])} flights across all date combinations.")
    st.download_button(
        label="📥  Download Excel Report",
        data=st.session_state.excel_bytes,
        file_name="air_india_fares.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    st.subheader("📊 Cheapest fares by outbound date")
    df = pd.DataFrame(job["results"]).dropna(subset=["Price (INR)"])
    if not df.empty:
        chart = (
            df.groupby("Outbound Date")["Price (INR)"]
              .min()
              .reset_index()
              .rename(columns={"Price (INR)": "Cheapest (INR)"})
        )
        st.bar_chart(chart.set_index("Outbound Date"))
        st.dataframe(df.sort_values("Price (INR)"), use_container_width=True)

elif job and job["status"] == "error":
    st.error("Scrape failed — check the log above for details.")