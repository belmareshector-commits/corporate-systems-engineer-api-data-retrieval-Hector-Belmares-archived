# src/main.py
from dotenv import load_dotenv
import os, sys, csv, json, re, requests, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------- logging ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(message)s")
log = logging.getLogger("parks_trip")

# ---------- helpers ----------
def isoparse(s: str):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def nps_headers(user_agent: str):
    return {"User-Agent": user_agent, "Accept": "application/json"}

def nws_headers(user_agent: str):
    return {"User-Agent": user_agent, "Accept": "application/geo+json"}

def avg_wind_mph(wind_str: str):
    if not wind_str:
        return None
    nums = [int(s) for s in wind_str.replace("mph", "").replace("to", " ").split() if s.isdigit()]
    return (sum(nums) / len(nums)) if nums else None

# ---------- weather scoring ----------
COMFORT_TEMP = 75

def condition_bonus(text: str) -> int:
    if not text: return 0
    t = text.lower()
    if "sunny" in t or "clear" in t: return +10
    if "partly" in t: return +5
    if "mostly cloudy" in t: return -2
    if "showers" in t or "rain" in t: return -10
    if "thunder" in t or "storm" in t: return -14
    return 0

def weather_score(day: dict | None) -> float:
    if not day: return 40.0
    high = day.get("high"); hum = day.get("humidity")
    wind = day.get("wind_mph"); cond = day.get("conditions")
    score = 100.0
    if high is not None: score -= min(40, abs(high - COMFORT_TEMP) * 1.2)
    if hum  is not None: score -= min(30, hum * 0.3)
    if wind is not None: score -= min(20, wind * 0.6)
    score += condition_bonus(cond)
    return max(0.0, min(100.0, score))

# ---------- NPS ----------
def nps_activities_parks(api_key: str, user_agent: str) -> dict:
    url = "https://developer.nps.gov/api/v1/activities/parks"
    r = requests.get(url, headers=nps_headers(user_agent), params={"api_key": api_key}, timeout=20)
    r.raise_for_status()
    return r.json()

def pick_tx_parks_by_activity(payload: dict, activity: str, max_candidates: int = 20) -> list[dict]:
    target = next((e for e in payload.get("data", [])
                   if activity.lower() in e.get("name", "").lower()), None)
    if not target: return []
    out = []
    for p in target.get("parks", []):
        states = [s.strip().upper() for s in (p.get("states") or "").split(",") if s.strip()]
        if "TX" in states:
            out.append({"fullName": p["fullName"], "parkCode": p["parkCode"]})
            if len(out) >= max_candidates: break
    return out

def parks_with_coords(api_key: str, user_agent: str, codes: list[str], need: int = 5) -> list[dict]:
    base = "https://developer.nps.gov/api/v1/parks"
    headers = nps_headers(user_agent)

    def parse(items):
        rows = []
        latlong_re = re.compile(r"lat\s*:\s*([-+]?\d+(?:\.\d+)?)[,\s]*long\s*:\s*([-+]?\d+(?:\.\d+)?)", re.I)
        for p in items:
            lat = lon = None
            if p.get("latitude") and p.get("longitude"):
                try:
                    lat = float(p["latitude"]); lon = float(p["longitude"])
                except Exception:
                    lat = lon = None
            if (lat is None or lon is None) and p.get("latLong"):
                m = latlong_re.search(p["latLong"])
                if m:
                    try:
                        lat = float(m.group(1)); lon = float(m.group(2))
                    except Exception:
                        lat = lon = None
            if lat and lon:
                rows.append({
                    "fullName": p.get("fullName"),
                    "parkCode": p.get("parkCode"),
                    "lat": lat, "lon": lon,
                    "url": p.get("url"),
                })
        return rows

    r = requests.get(base, headers=headers, params={
        "api_key": api_key, "parkCode": ",".join(codes),
        "limit": 500, "fields": "latLong,latitude,longitude,url"
    }, timeout=20)
    r.raise_for_status()
    got = parse(r.json().get("data", []))

    if len(got) < need:
        rr = requests.get(base, headers=headers, params={
            "api_key": api_key, "stateCode": "TX", "limit": 500,
            "fields": "latLong,latitude,longitude,url"
        }, timeout=20)
        rr.raise_for_status()
        all_tx = parse(rr.json().get("data", []))
        existing = {p["parkCode"] for p in got}
        for p in all_tx:
            if p["parkCode"] not in existing:
                got.append(p); existing.add(p["parkCode"])
            if len(got) >= need: break

    log.info("Filled up to %d parks with coordinates successfully.", len(got))
    return got[:need]

# ---------- NWS ----------
def week_forecast(user_agent: str, lat: float, lon: float) -> dict:
    props = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=nws_headers(user_agent), timeout=20).json()["properties"]
    daily = requests.get(props["forecast"], headers=nws_headers(user_agent), timeout=20).json()["properties"]["periods"]
    grid  = requests.get(props["forecastGridData"], headers=nws_headers(user_agent), timeout=20).json()["properties"]

    def pick_day(date_):
        for p in daily:
            if p.get("isDaytime", True) and isoparse(p["startTime"]).date() == date_:
                return p
        return None

    def humidity_for(date_):
        series = (grid.get("relativeHumidity") or {}).get("values", [])
        if not series: return None
        target = datetime.combine(date_, datetime.min.time(), tzinfo=timezone.utc).replace(hour=12)
        series.sort(key=lambda v: abs((isoparse(v["validTime"].split("/")[0]) - target).total_seconds()))
        val = series[0].get("value")
        return int(round(val)) if val is not None else None

    start = datetime.now(timezone.utc).date()
    out = {}
    for i in range(7):
        d = start + timedelta(days=i)
        day = pick_day(d)
        if not day:
            out[d.isoformat()] = None
            continue
        low = None
        out[d.isoformat()] = {
            "high": day.get("temperature"),
            "low": low,
            "conditions": day.get("shortForecast"),
            "wind_mph": avg_wind_mph(day.get("windSpeed")),
            "humidity": humidity_for(d),
        }
    return out

# ---------- Google Sheets Upload ----------
import csv as _csv

def upload_to_google_sheets(csv_path: str):
    """Update existing Google Sheet instead of creating new ones."""
    if os.getenv("GOOGLE_SHEETS_UPLOAD", "false").lower() != "true":
        log.info("Google Sheets upload: skipped (GOOGLE_SHEETS_UPLOAD != true)")
        return

    creds_path = Path(os.getenv("GOOGLE_SHEETS_CREDENTIALS", "service_account.json")).expanduser().resolve()
    sheet_id   = (os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "") or "").strip()
    tab_name   = (os.getenv("GOOGLE_SHEETS_WORKSHEET_NAME", "Itinerary") or "").strip()

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        log.warning("Google Sheets upload: missing gspread/google-auth. Skipping.")
        return

    if not creds_path.exists():
        log.warning("Google Sheets upload: credentials file not found: %s. Skipping.", creds_path)
        return

    if not sheet_id:
        log.error("Google Sheets upload: missing GOOGLE_SHEETS_SPREADSHEET_ID in .env — cannot update.")
        return

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"
    ]

    creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
    client = gspread.authorize(creds)

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        rows = list(_csv.reader(f))

    ts_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        sh = client.open_by_key(sheet_id)
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows=max(50, len(rows)+10), cols=max(10, len(rows[0])+5))

        ws.clear()
        ws.update("A1", [[f"Last Updated", ts_local]])
        ws.update("A2", rows, value_input_option="RAW")
        ws.freeze(rows=2)

        print("\n✅ Google Sheets updated successfully!")
        print("🔗", sh.url, "\n")
       
    except Exception as e:
        log.warning("Google Sheets upload: error: %s", e)

# ---------- main ----------
def main():
    log.debug("main() started")
    load_dotenv()
    api_key = os.getenv("NPS_API_KEY")
    user_agent = os.getenv("USER_AGENT", "Hector Belmares <email>")
    activity = (os.getenv("ACTIVITY") or "Hiking").strip()

    if not api_key:
        log.error("Missing NPS_API_KEY in .env")
        sys.exit(1)

    payload = nps_activities_parks(api_key, user_agent)
    candidates = pick_tx_parks_by_activity(payload, activity)
    parks = parks_with_coords(api_key, user_agent, [p["parkCode"] for p in candidates], need=5)

    start = datetime.now(timezone.utc).date()
    visit_days = [start + timedelta(days=i) for i in range(5)]
    forecasts = {p["parkCode"]: week_forecast(user_agent, p["lat"], p["lon"]) for p in parks}

    scored = []
    for i, p in enumerate(parks):
        d = visit_days[i]
        fx = forecasts[p["parkCode"]].get(d.isoformat())
        scored.append({"park": p, "date": d, "fx": fx, "score": weather_score(fx)})
    scored.sort(key=lambda x: x["score"], reverse=True)

    rows = []
    print("\nTexas 7-Day Itinerary (Sunniest Trip)\n")
    for i, item in enumerate(scored, start=1):
        p, d, fx = item["park"], item["date"], item["fx"]
        rows.append({
            "Order": i,
            "Park Name": p["fullName"],
            "State": "TX",
            "Visit Date": d.isoformat(),
            "Forecast High (°F)": fx.get("high") if fx else None,
            "Forecast Low (°F)": fx.get("low") if fx else None,
            "Weather": fx.get("conditions") if fx else None,
            "Wind Speed (mph)": fx.get("wind_mph") if fx else None,
            "Humidity (%)": fx.get("humidity") if fx else None,
            "NPS Link": p["url"],
            "Directions": f"https://www.google.com/maps/search/?api=1&query={p['lat']},{p['lon']}",
        })
        if fx:
            print(f"{i}) {p['fullName']} — {d.isoformat()}")
            print(f"    High {fx.get('high')}°F  Low {fx.get('low')}°F  {fx.get('conditions')}  "
                  f"Wind {fx.get('wind_mph')} mph  Humidity {fx.get('humidity')}%")

    base = Path("parks_tx_itinerary_simple")
    with base.with_suffix(".json").open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    with base.with_suffix(".csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    log.info("Saved: %s", base.with_suffix(".csv").name)
    upload_to_google_sheets(base.with_suffix(".csv").name)

if __name__ == "__main__":
    main()
