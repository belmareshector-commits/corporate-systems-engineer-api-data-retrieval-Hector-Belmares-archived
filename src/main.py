# src/main.py
from dotenv import load_dotenv
import os, sys, csv, json, time, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

# ----------------- Simple helpers -----------------

def isoparse(s: str):
    """Minimal ISO8601 parser (no extra package)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def nps_headers(user_agent: str):
    return {"User-Agent": user_agent, "Accept": "application/json"}

def nws_headers(user_agent: str):
    return {"User-Agent": user_agent, "Accept": "application/geo+json"}

def avg_wind_mph(wind_str: str):
    """Handle '10 mph' or '10 to 15 mph' -> average mph."""
    if not wind_str:
        return None
    nums = [int(s) for s in wind_str.replace("mph", "").replace("to", " ").split() if s.isdigit()]
    return (sum(nums) / len(nums)) if nums else None

# ----------------- Weather scoring -----------------
COMFORT_TEMP = 75  # aim for mild highs

def condition_bonus(text: str) -> int:
    """Sunny/clear good, rain/storm bad."""
    if not text: return 0
    t = text.lower()
    if "sunny" in t or "clear" in t: return +10
    if "partly" in t: return +5
    if "mostly cloudy" in t: return -2
    if "showers" in t or "rain" in t: return -10
    if "thunder" in t or "storm" in t: return -14
    return 0

def weather_score(day: dict | None) -> float:
    """0..100 (higher = better). Penalize heat, humidity, wind; reward sunny."""
    if not day: return 40.0
    high = day.get("high")
    hum  = day.get("humidity")
    wind = day.get("wind_mph")
    cond = day.get("conditions")

    score = 100.0
    if high is not None:
        score -= min(40, abs(high - COMFORT_TEMP) * 1.2)
    if hum is not None:
        score -= min(30, hum * 0.3)
    if wind is not None:
        score -= min(20, wind * 0.6)
    score += condition_bonus(cond)
    return max(0.0, min(100.0, score))

# ----------------- NPS: parks lookup -----------------

def nps_activities_parks(api_key: str, user_agent: str) -> dict:
    url = "https://developer.nps.gov/api/v1/activities/parks"
    r = requests.get(url, headers=nps_headers(user_agent), params={"api_key": api_key}, timeout=20)
    r.raise_for_status()
    return r.json()

def pick_tx_parks_by_activity(payload: dict, activity: str, max_candidates: int = 20) -> list[dict]:
    """First up to max_candidates TX parks that list the activity."""
    target = next((e for e in payload.get("data", [])
                   if activity.lower() in e.get("name", "").lower()), None)
    if not target:
        return []
    out = []
    for p in target.get("parks", []):
        states = [s.strip().upper() for s in (p.get("states") or "").split(",") if s.strip()]
        if "TX" in states:
            out.append({"fullName": p["fullName"], "parkCode": p["parkCode"]})
            if len(out) >= max_candidates:
                break
    return out

def parks_with_coords(api_key: str, user_agent: str, codes: list[str], need: int = 5) -> list[dict]:
    """Resolve lat/lon for parks and keep pulling until we have 5 valid ones."""
    base = "https://developer.nps.gov/api/v1/parks"
    headers = nps_headers(user_agent)

    def parse(items):
        rows = []
        latlong_re = re.compile(r"lat\s*:\s*([-+]?\d+(?:\.\d+)?)[,\s]*long\s*:\s*([-+]?\d+(?:\.\d+)?)", re.I)
        for p in items:
            lat = lon = None
            if p.get("latitude") and p.get("longitude"):
                try:
                    lat = float(p["latitude"])
                    lon = float(p["longitude"])
                except Exception:
                    lat = lon = None
            if (lat is None or lon is None) and p.get("latLong"):
                m = latlong_re.search(p["latLong"])
                if m:
                    try:
                        lat = float(m.group(1))
                        lon = float(m.group(2))
                    except Exception:
                        lat = lon = None
            if lat and lon:
                rows.append({
                    "fullName": p.get("fullName"),
                    "parkCode": p.get("parkCode"),
                    "lat": lat,
                    "lon": lon,
                    "url": p.get("url"),
                })
        return rows

    # 1️⃣ Try bulk fetch first
    r = requests.get(
        base,
        headers=headers,
        params={
            "api_key": api_key,
            "parkCode": ",".join(codes),
            "limit": 500,
            "fields": "latLong,latitude,longitude,url",
        },
        timeout=20,
    )
    r.raise_for_status()
    got = parse(r.json().get("data", []))

    # 2️⃣ Fallback: fetch *all TX parks* and keep filling until we have 5
    if len(got) < need:
        rr = requests.get(
            base,
            headers=headers,
            params={
                "api_key": api_key,
                "stateCode": "TX",
                "limit": 500,
                "fields": "latLong,latitude,longitude,url",
            },
            timeout=20,
        )
        rr.raise_for_status()
        all_tx = parse(rr.json().get("data", []))

        # Fill from TX parks until we reach 5
        existing = {p["parkCode"] for p in got}
        for p in all_tx:
            if p["parkCode"] not in existing:
                got.append(p)
                existing.add(p["parkCode"])
            if len(got) >= need:
                break

    # 3️⃣ Final check
    if len(got) < need:
        print(f"⚠️ Only resolved {len(got)} park(s) with coordinates; used every TX park available.")
    else:
        print(f"✅ Filled up to {len(got)} parks with coordinates successfully.")

    return got[:need]


# ----------------- NWS: 7-day forecast per park -----------------

def week_forecast(user_agent: str, lat: float, lon: float) -> dict:
    """Return {YYYY-MM-DD: {high,low,conditions,wind_mph,humidity}} for next 7 days."""
    props = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=nws_headers(user_agent), timeout=20).json()["properties"]
    daily = requests.get(props["forecast"], headers=nws_headers(user_agent), timeout=20).json()["properties"]["periods"]
    grid  = requests.get(props["forecastGridData"], headers=nws_headers(user_agent), timeout=20).json()["properties"]

    def pick_day(date_):
        for p in daily:
            if p.get("isDaytime", True) and isoparse(p["startTime"]).date() == date_:
                return p
        # fallback closest daytime
        cands = [p for p in daily if p.get("isDaytime", True)]
        if not cands: return None
        cands.sort(key=lambda p: abs((isoparse(p["startTime"]).date() - date_).days))
        return cands[0]

    def humidity_for(date_):
        series = (grid.get("relativeHumidity") or {}).get("values", [])
        if not series: return None
        target = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0).date()
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
        # try to read the following night for "low"
        idx = daily.index(day)
        low = None
        if idx+1 < len(daily) and not daily[idx+1].get("isDaytime", True):
            low = daily[idx+1].get("temperature")
        out[d.isoformat()] = {
            "high": day.get("temperature"),
            "low": low,
            "conditions": day.get("shortForecast"),
            "wind_mph": avg_wind_mph(day.get("windSpeed")),
            "humidity": humidity_for(d),
        }
    return out

# ----------------- Main orchestration -----------------

def main():
    load_dotenv()
    api_key = os.getenv("NPS_API_KEY")
    user_agent = os.getenv("USER_AGENT", "Hector Belmares <email>")
    activity = (os.getenv("ACTIVITY") or "Hiking").strip()

    if not api_key:
        print("Missing NPS_API_KEY in .env"); sys.exit(1)

    # 1) Pick TX parks (activity filter)
    payload = nps_activities_parks(api_key, user_agent)
    candidates = pick_tx_parks_by_activity(payload, activity, max_candidates=20)
    if len(candidates) < 5:
        print(f"Need at least 5 TX parks for '{activity}'. Found {len(candidates)}.")
        sys.exit(1)

    # 2) Get coordinates and keep the first 5 that resolve
    codes = [p["parkCode"] for p in candidates]
    parks = parks_with_coords(api_key, user_agent, codes, need=5)
    if len(parks) < 5:
        print("Could not resolve coordinates for enough parks."); sys.exit(1)

    # 3) Build 7-day window; visit parks on days 1–5, leave 6–7 as flex
    start = datetime.now(timezone.utc).date()
    visit_days = [start + timedelta(days=i) for i in range(5)]
    flex_days  = [start + timedelta(days=5), start + timedelta(days=6)]

    # 4) Get forecasts once per park, then score for its visit day
    forecasts = {p["parkCode"]: week_forecast(user_agent, p["lat"], p["lon"]) for p in parks}
    scored = []
    for i, p in enumerate(parks):
        d = visit_days[i]
        fx = forecasts[p["parkCode"]].get(d.isoformat())
        scored.append({
            "park": p,
            "date": d,
            "fx": fx,
            "score": weather_score(fx)
        })

    # 5) Sort by “Sunniest Trip” (best weather first)
    scored.sort(key=lambda x: x["score"], reverse=True)

    # 6) Build final rows (parks for days 1–5; flex for days 6–7)
    rows = []
    for i, item in enumerate(scored, start=1):
        p, d, fx = item["park"], item["date"], item["fx"]
        rows.append({
            "Order": i,
            "Park Name": p["fullName"],
            "State": "TX",
            "Visit Date": d.isoformat(),
            "Forecast High": fx.get("high") if fx else None,
            "Forecast Low": fx.get("low") if fx else None,
            "Weather": fx.get("conditions") if fx else None,
            "Wind Speed": fx.get("wind_mph") if fx else None,
            "Humidity": fx.get("humidity") if fx else None,
            "NPS Link": p["url"],
            "Directions": f"https://www.google.com/maps/search/?api=1&query={p['lat']},{p['lon']}",
        })

    # Add 2 flex days
    rows.append({"Order": 6, "Park Name": "Flex/Travel", "State": "TX",
                 "Visit Date": flex_days[0].isoformat(), "Forecast High": None,
                 "Forecast Low": None, "Weather": None, "Wind Speed": None,
                 "Humidity": None, "NPS Link": None,
                 "Directions": "Use prior day's location"})
    rows.append({"Order": 7, "Park Name": "Flex/Travel", "State": "TX",
                 "Visit Date": flex_days[1].isoformat(), "Forecast High": None,
                 "Forecast Low": None, "Weather": None, "Wind Speed": None,
                 "Humidity": None, "NPS Link": None,
                 "Directions": "Use next day's location"})

    # 7) Print + save
    print("\n=== Texas 7-Day Itinerary (Sunniest Trip) ===\n")
    for r in rows:
        if r["Park Name"].startswith("Flex"):
            print(f"{r['Order']}) {r['Park Name']} — {r['Visit Date']}")
        else:
            print(f"{r['Order']}) {r['Park Name']} — {r['Visit Date']}")
            print(f"    High {r['Forecast High']}  Low {r['Forecast Low']}  {r['Weather']}  "
                  f"Wind {r['Wind Speed']} mph  Humidity {r['Humidity']}%")

    base = Path("parks_tx_itinerary_simple")
    with base.with_suffix(".json").open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    with base.with_suffix(".csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    print(f"\nSaved: {base.with_suffix('.json').name}, {base.with_suffix('.csv').name}")

if __name__ == "__main__":
    main()
