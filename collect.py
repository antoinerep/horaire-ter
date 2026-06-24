"""Collect realtime departures + arrivals on the Lyon ↔ Saint-Étienne ↔ Le Puy
axes from the SNCF / Navitia API.

The SNCF realtime API only retains realtime data for a few hours, so this script
runs frequently (every 2h via GitHub Actions) and accumulates observations into
a daily file. Each row records `base_*` (scheduled) and `*_date_time` (realtime)
for both arrival and departure at one stop_area for one train.

Output: `data/YYYY-MM-DD.jsonl.gz`, one row per (train, stop, direction) event.
    {date, run_ts, vj_id, train_name, line_name, direction,
     stop_id, stop_name, kind (dep|arr), base_dt, realtime_dt,
     delay_sec, freshness, status}

Re-runs append + deduplicate on (vj_id, stop_id, kind, base_dt).

Usage:
    SNCF_API_KEY=xxx python collect.py             # default: capture past 4h
    SNCF_API_KEY=xxx python collect.py --backfill  # capture full current day
    SNCF_API_KEY=xxx python collect.py --refresh-stops  # regenerate STOPS list
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import requests
from requests.auth import HTTPBasicAuth

API_BASE = "https://api.sncf.com/v1/coverage/sncf"

# Curated train stops on the Lyon ↔ Saint-Étienne ↔ Le Puy axes (C18 + P28).
# Bus stops, gares routières and TCL stops are excluded.
# Regenerate with `python collect.py --refresh-stops`.
STOPS: dict[str, str] = {
    "stop_area:SNCF:87723197": "Lyon Part Dieu",
    "stop_area:SNCF:87722025": "Lyon Perrache",
    "stop_area:SNCF:87722207": "Oullins",
    "stop_area:SNCF:87723502": "Crépieux la Pape",
    "stop_area:SNCF:87723528": "Miribel",
    "stop_area:SNCF:87723536": "Saint-Maurice-de-Beynost",
    "stop_area:SNCF:87723544": "Beynost",
    "stop_area:SNCF:87723569": "Montluel",
    "stop_area:SNCF:87723577": "La Valbonne",
    "stop_area:SNCF:87723585": "Meximieux - Pérouges",
    "stop_area:SNCF:87743716": "Ambérieu-en-Bugey",
    "stop_area:SNCF:87722405": "Givors Ville",
    "stop_area:SNCF:87726331": "Rive-de-Gier",
    "stop_area:SNCF:87726307": "Saint-Chamond",
    "stop_area:SNCF:87726000": "Saint-Étienne Châteaucreux",
    "stop_area:SNCF:87698662": "Saint-Étienne Châteaucreux Gare Routière",
    "stop_area:SNCF:87726190": "Saint-Étienne Bellevue",
    "stop_area:SNCF:87726174": "Saint-Étienne Le Clapier",
    "stop_area:SNCF:87726901": "Saint-Étienne Carnot",
    "stop_area:SNCF:87726703": "La Ricamarie",
    "stop_area:SNCF:87726711": "Le Chambon-Feugerolles",
    "stop_area:SNCF:87726729": "Firminy",
    "stop_area:SNCF:87726737": "Fraisses - Unieux",
    "stop_area:SNCF:87726760": "Aurec",
    "stop_area:SNCF:87726778": "Bas - Monistrol",
    "stop_area:SNCF:87726794": "Beauzac",
    "stop_area:SNCF:87726786": "Pont de Lignon",
    "stop_area:SNCF:87734723": "Vorey",
    "stop_area:SNCF:87734731": "Chamalières-sur-Loire",
    "stop_area:SNCF:87734707": "Lavoûte-sur-Loire",
    "stop_area:SNCF:87734749": "Retournac",
    "stop_area:SNCF:87734715": "Saint-Vincent le Château",
    "stop_area:SNCF:87734699": "Le Puy-en-Velay",
    # Le Puy connecting bus stops (P37 to Clermont-Ferrand, P85 to Brioude)
    "stop_area:SNCF:87024380": "Le Puy-en-Velay Lafayette",
    "stop_area:SNCF:87589598": "Le Puy En Velay - Hôpital E.Roux",
}

# Lines whose stops we re-discover with `--refresh-stops`.
# C18 = Lyon ↔ Saint-Étienne, P28 = Saint-Étienne ↔ Le Puy,
# P37 / P85 = Le Puy ↔ Clermont-Ferrand / Brioude (connection buses).
REFRESH_LINE_CODES = ("C18", "P28", "P37", "P85")
AURA_NETWORK_ID = "network:SNCF:FR:Branding::f4fa116c-2d6a-4696-b6c9-47195206d6f4:"

# How far back each run looks. 4h covers the 2h cron interval with overlap.
LOOKBACK_HOURS = 4

# Page size for departures/arrivals (Navitia caps around 1000).
PAGE_COUNT = 500

SLEEP_BETWEEN_CALLS_S = 0.25
MAX_RETRIES = 5
RETRY_BASE_DELAY_S = 2.0


def api_key() -> str:
    key = os.environ.get("SNCF_API_KEY")
    if not key:
        sys.exit("Missing SNCF_API_KEY env var.")
    return key


def get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    auth = HTTPBasicAuth(api_key(), "")
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, params=params, auth=auth, timeout=30)
        if resp.status_code == 200:
            time.sleep(SLEEP_BETWEEN_CALLS_S)
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            delay = RETRY_BASE_DELAY_S * (2**attempt)
            print(f"  HTTP {resp.status_code}, retry in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {url}")


def parse_dt(s: str | None) -> datetime | None:
    """Navitia local datetimes look like '20260623T184500'."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def delay_seconds(scheduled: datetime | None, realtime: datetime | None) -> int | None:
    if scheduled is None or realtime is None:
        return None
    return int((realtime - scheduled).total_seconds())


def fetch_stops(path: str, key: str, stop_id: str, stop_name: str, from_dt: str, kind: str, run_ts: str) -> Iterator[dict[str, Any]]:
    """Page through a departures or arrivals endpoint."""
    params = {
        "from_datetime": from_dt,
        "data_freshness": "realtime",
        "count": PAGE_COUNT,
        "depth": 0,
    }
    url: str | None = f"{API_BASE}/stop_areas/{stop_id}/{path}"
    while url:
        data = get_json(url, params=params)
        for item in data.get(key, []):
            sdt = item.get("stop_date_time") or {}
            base_dep = parse_dt(sdt.get("base_departure_date_time"))
            real_dep = parse_dt(sdt.get("departure_date_time"))
            base_arr = parse_dt(sdt.get("base_arrival_date_time"))
            real_arr = parse_dt(sdt.get("arrival_date_time"))
            di = item.get("display_informations") or {}
            links = item.get("links") or []
            vj_id = next((l.get("id", "") for l in links if l.get("type") == "vehicle_journey"), "")
            # Yield one row for the arrival side and one for the departure side
            # when both are available (intermediate stops have both).
            common = {
                "run_ts": run_ts,
                "vj_id": vj_id,
                "train_name": di.get("headsign") or di.get("name") or "",
                "line_name": di.get("commercial_mode") or di.get("name") or "",
                "direction": di.get("direction") or "",
                "stop_id": stop_id,
                "stop_name": stop_name,
                "freshness": sdt.get("data_freshness"),
                "kind_origin": kind,  # "departures" or "arrivals" call origin
            }
            if base_dep is not None:
                yield {
                    **common,
                    "kind": "dep",
                    "base_dt": base_dep.isoformat(),
                    "realtime_dt": real_dep.isoformat() if real_dep else None,
                    "delay_sec": delay_seconds(base_dep, real_dep),
                }
            if base_arr is not None and base_arr != base_dep:
                yield {
                    **common,
                    "kind": "arr",
                    "base_dt": base_arr.isoformat(),
                    "realtime_dt": real_arr.isoformat() if real_arr else None,
                    "delay_sec": delay_seconds(base_arr, real_arr),
                }
        next_link = next(
            (l["href"] for l in data.get("links", []) if l.get("type") == "next"),
            None,
        )
        url = next_link
        params = None


def collect_window(from_dt: datetime, target_date: date) -> pathlib.Path:
    run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    from_str = from_dt.strftime("%Y%m%dT%H%M%S")
    out_dir = pathlib.Path("data")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{target_date.isoformat()}.jsonl.gz"

    # Load existing observations (dedupe key: vj_id|stop_id|kind|base_dt).
    existing: dict[tuple, dict[str, Any]] = {}
    if out_file.exists():
        with gzip.open(out_file, "rt", encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                key = (row["vj_id"], row["stop_id"], row["kind"], row["base_dt"])
                existing[key] = row

    new_rows = 0
    updated_rows = 0
    api_calls = 0
    events_seen = 0
    for stop_id, stop_name in STOPS.items():
        for path, key, kind in (("departures", "departures", "dep_origin"), ("arrivals", "arrivals", "arr_origin")):
            for row in fetch_stops(path, key, stop_id, stop_name, from_str, kind, run_ts):
                events_seen += 1
                row_date = row["base_dt"][:10]
                if row_date != target_date.isoformat():
                    continue  # ignore data slipping into adjacent days
                k = (row["vj_id"], row["stop_id"], row["kind"], row["base_dt"])
                prev = existing.get(k)
                # Prefer rows with realtime freshness; ignore base_schedule
                # updates that would overwrite a realtime observation.
                if prev is None:
                    existing[k] = row
                    new_rows += 1
                elif (
                    prev.get("freshness") == "base_schedule"
                    and row.get("freshness") == "realtime"
                ):
                    existing[k] = row
                    updated_rows += 1
                elif (
                    prev.get("freshness") == row.get("freshness")
                    and row.get("delay_sec") != prev.get("delay_sec")
                ):
                    existing[k] = row
                    updated_rows += 1
            api_calls += 1

    with gzip.open(out_file, "wt", encoding="utf-8") as f:
        for row in existing.values():
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        f"{run_ts}: {api_calls} API calls, {events_seen} events seen, "
        f"{len(existing)} rows total ({new_rows} new, {updated_rows} updated) → {out_file}"
    )

    # Silent-failure detection: during operational hours we should always see
    # some events. Zero events with non-zero calls means the API returned
    # empty everywhere — likely an outage or a quota issue.
    paris_hour = (datetime.now(timezone.utc) + timedelta(hours=2)).hour
    if api_calls > 0 and events_seen == 0 and 5 <= paris_hour <= 23:
        sys.exit(
            f"FAIL: 0 events from {api_calls} API calls at {paris_hour}h Paris "
            "(operational hours) — suspected outage or quota issue."
        )

    return out_file


def refresh_stops() -> None:
    """Re-query line stops and print an updated STOPS dict."""
    lines = get_json(f"{API_BASE}/networks/{AURA_NETWORK_ID}/lines", params={"count": 100, "depth": 2})
    targets = [l for l in lines.get("lines", []) if l.get("code") in REFRESH_LINE_CODES]
    seen: dict[str, str] = {}
    for line in targets:
        data = get_json(f"{API_BASE}/lines/{line['id']}/stop_areas", params={"count": 200})
        for sa in data.get("stop_areas", []):
            name = sa.get("name", "")
            # Drop bus and roadside stops by naming convention.
            if any(t in name.lower() for t in ("gare routière", "r.d.", "rd1084", "tcl", "parking")):
                continue
            seen[sa["id"]] = name
    print("STOPS: dict[str, str] = {")
    for sid, name in sorted(seen.items()):
        print(f'    "{sid}": "{name}",')
    print("}")


def main() -> None:
    if "--refresh-stops" in sys.argv:
        refresh_stops()
        return

    now_paris = datetime.now(timezone.utc) + timedelta(hours=2)  # Approx Europe/Paris in summer
    if "--backfill" in sys.argv:
        from_dt = now_paris.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        from_dt = now_paris - timedelta(hours=LOOKBACK_HOURS)

    target_date = now_paris.date()
    collect_window(from_dt.replace(tzinfo=None), target_date)


if __name__ == "__main__":
    main()
