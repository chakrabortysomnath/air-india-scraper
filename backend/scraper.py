"""
scraper.py
Playwright-based scraper for Air India round-trip fares.
Called by main.py for each outbound + return date combination.
"""

import time
import random
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ── Helpers ────────────────────────────────────────────────────────────────

def _pause(lo=3, hi=6):
    """Random polite delay to avoid triggering bot detection."""
    time.sleep(random.uniform(lo, hi))


def _date_range(start: str, end: str) -> list[str]:
    """Return list of dates from start to end inclusive (YYYY-MM-DD)."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end,   "%Y-%m-%d")
    return [(s + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((e - s).days + 1)]


def _build_url(origin, destination, depart_date, return_date, adults):
    """
    Construct Air India search URL for a round-trip.
    tripType=R means round-trip.
    """
    base = "https://www.airindia.com/in/en/book/flight-search.html"
    return (
        f"{base}?tripType=R"
        f"&origin={origin}"
        f"&destination={destination}"
        f"&departureDate={depart_date}"
        f"&returnDate={return_date}"
        f"&adult={adults}&child=0&infant=0&cabinClass=ECONOMY"
    )


def _safe_text(card, *selectors) -> str:
    """Try multiple CSS selectors; return first non-empty text found."""
    for sel in selectors:
        el = card.query_selector(sel)
        if el:
            t = el.inner_text().strip()
            if t:
                return t
    return ""


def _parse_price(raw: str):
    """Convert '₹4,500' or '4500.0' to a float, or return None."""
    cleaned = raw.replace("₹", "").replace(",", "").strip().split()[0]
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_stops(raw: str):
    """Convert 'Non-stop' → 0, '1 Stop' → 1, etc."""
    if not raw:
        return None
    if "non" in raw.lower():
        return 0
    digits = "".join(c for c in raw if c.isdigit())
    return int(digits) if digits else None


# ── Core scrape function ───────────────────────────────────────────────────

def scrape_round_trips(
    origin: str,
    destination: str,
    outbound_start: str,
    outbound_end: str,
    return_start: str,
    return_end: str,
    adults: int = 1,
    progress_callback=None,       # optional fn(message: str) for live updates
) -> list[dict]:
    """
    Scrape Air India round-trip fares for all combinations of:
      - outbound dates: outbound_start .. outbound_end
      - return  dates: return_start  .. return_end

    Returns a list of flight-pair dicts ready for DataFrame/Excel export.
    """

    outbound_dates = _date_range(outbound_start, outbound_end)
    return_dates   = _date_range(return_start,   return_end)
    all_results    = []

    def log(msg):
        if progress_callback:
            progress_callback(msg)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,          # must be True on Render servers
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

        # Visit homepage first — mimics real user behaviour
        log("Opening Air India homepage…")
        page.goto("https://www.airindia.com/in/en.html", timeout=30000)
        _pause(3, 5)

        for dep in outbound_dates:
            for ret in return_dates:
                # Return must be on or after outbound date
                if ret < dep:
                    continue

                url = _build_url(origin, destination, dep, ret, adults)
                log(f"Searching  {dep}  →  {ret}  …")

                try:
                    page.goto(url, timeout=60000)
                    page.wait_for_selector(
                        "div.flight-card, div[class*='flightCard'], div[class*='flight-result']",
                        timeout=35000,
                    )
                    _pause(3, 6)

                    cards = page.query_selector_all(
                        "div.flight-card, div[class*='flightCard'], div[class*='flight-result']"
                    )

                    if not cards:
                        log(f"  ⚠ No results found for {dep} / {ret}")
                        continue

                    for card in cards:
                        try:
                            flight_num = _safe_text(
                                card,
                                "[class*='flightNumber']",
                                "[class*='flight-number']",
                            )
                            departure = _safe_text(
                                card,
                                "[class*='departureTime']",
                                "[class*='depart-time']",
                            )
                            arrival = _safe_text(
                                card,
                                "[class*='arrivalTime']",
                                "[class*='arrive-time']",
                            )
                            duration = _safe_text(
                                card,
                                "[class*='duration']",
                                "[class*='flight-duration']",
                            )
                            stops_raw = _safe_text(
                                card,
                                "[class*='stop']",
                                "[class*='stops']",
                            )
                            price_raw = _safe_text(
                                card,
                                "[class*='price']",
                                "[class*='fare']",
                                "[class*='amount']",
                            )

                            all_results.append({
                                "Outbound Date":   dep,
                                "Return Date":     ret,
                                "Flight Number":   flight_num,
                                "Departure Time":  departure,
                                "Arrival Time":    arrival,
                                "Duration":        duration,
                                "Stops":           _parse_stops(stops_raw),
                                "Price (INR)":     _parse_price(price_raw),
                                "Airline":         "Air India",
                            })

                        except Exception as e:
                            log(f"  ⚠ Skipped one card: {e}")

                    log(f"  ✓ {len(cards)} flights found")

                except PWTimeout:
                    log(f"  ✗ Timed out for {dep} / {ret} — skipping")
                except Exception as e:
                    log(f"  ✗ Error for {dep} / {ret}: {e}")

                _pause(4, 8)          # polite gap between requests

        browser.close()

    return all_results