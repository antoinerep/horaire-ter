# Lyon ↔ Le Puy realtime delays

Captures realtime train arrivals and departures on the Lyon Part-Dieu ↔
Saint-Étienne Châteaucreux ↔ Le Puy-en-Velay axes (TER lines C18 + P28) from
the SNCF / Navitia API. Runs every 2 hours via GitHub Actions and commits
observations back to this repo.

## Why every 2 hours?

The SNCF API only retains the `realtime` freshness flag for a few hours after
a train has run. Beyond that, the API falls back to `base_schedule`, hiding any
delay that actually occurred. Frequent polling catches every train while its
realtime status is still exposed.

## Setup

1. **Get a token** at https://numerique.sncf.com/startup/api/token-developpeur/
   (free, 5 000 req/day).
2. **Push this repo to GitHub** (public repo → unlimited Actions minutes).
3. **Add the secret** in repo Settings → Secrets and variables → Actions:
   - Name: `SNCF_API_KEY`
   - Value: your token
4. **Trigger a first run** manually via Actions → workflow_dispatch with
   `backfill=true` to seed the current day.

## Output

`data/YYYY-MM-DD.jsonl.gz` — one gzipped JSON-lines file per day. Each row:

```json
{
  "run_ts": "2026-06-24T10:00:00+00:00",
  "vj_id": "vehicle_journey:SNCF:2026-06-24:890300:1187:Train",
  "train_name": "890300",
  "line_name": "REGIONAURA",
  "direction": "Saint-Étienne Châteaucreux",
  "stop_id": "stop_area:SNCF:87723197",
  "stop_name": "Lyon Part Dieu",
  "kind": "dep",
  "base_dt": "2026-06-24T07:12:00",
  "realtime_dt": "2026-06-24T07:14:00",
  "delay_sec": 120,
  "freshness": "realtime"
}
```

Re-runs append new observations and refresh existing rows when a `base_schedule`
entry is later upgraded to `realtime`.

## Stops covered

35 stops covering the Lyon ↔ Saint-Étienne ↔ Le Puy train axes (C18 + P28)
plus connecting bus origins at Saint-Étienne Châteaucreux Gare Routière and
the two extra Le Puy stops on lines P37 (toward Clermont-Ferrand) and P85
(toward Brioude). Refresh with `python collect.py --refresh-stops`.

## Quota

35 stops × 2 endpoints (departures + arrivals) × 12 runs/day ≈ 840 calls/day.
Quota is 5 000/day → comfortable margin.

## Analysis (after a few weeks of data)

```python
import gzip, json, pathlib, statistics

delays = []
for f in sorted(pathlib.Path("data").glob("*.jsonl.gz")):
    with gzip.open(f, "rt") as fh:
        for line in fh:
            row = json.loads(line)
            if row["kind"] == "arr" and row["delay_sec"] is not None:
                delays.append(row["delay_sec"])

late = [d for d in delays if d >= 300]  # SNCF's "5 min" threshold
print(f"% late: {len(late) / len(delays):.1%}")
print(f"avg delay (late only): {statistics.mean(late) / 60:.1f} min")
```

For missed connections at Saint-Étienne Châteaucreux: filter rows with
`stop_id == "stop_area:SNCF:87726000"`, pair arriving trains from Lyon with
departing trains toward Le Puy, flag pairs where `realtime_dt` of arrival is
within 3 min of `base_dt` of departure.
