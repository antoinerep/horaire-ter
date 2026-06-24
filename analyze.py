"""Compute a daily summary of Lyon ↔ Le Puy delays and write STATS.md.

Reads gzipped JSONL files in data/, filters to the last 24h, restricted to
REGIONAURA TER trains (excludes TGV INOUI and Intercités passing through).

Output: STATS.md (overwritten on each run).
"""

from __future__ import annotations

import gzip
import json
import pathlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Callable

DATA_DIR = pathlib.Path("data")
OUT_FILE = pathlib.Path("STATS.md")

PARIS_TZ = timezone(timedelta(hours=2))  # CEST; OK for summer, off by 1h in winter

STE_HUB = "stop_area:SNCF:87726000"  # Saint-Étienne Châteaucreux (trains)
LE_PUY = "stop_area:SNCF:87734699"
LYON_STOPS = {
    "stop_area:SNCF:87723197",  # Lyon Part-Dieu
    "stop_area:SNCF:87722025",  # Lyon Perrache
}

# Hub groups for axis filtering: a train is "on axis" if its journey covers
# at least 2 of these hub groups.
AXIS_HUBS = (
    LYON_STOPS,             # any Lyon hub
    {STE_HUB},              # Saint-Étienne Châteaucreux
    {LE_PUY},               # Le Puy-en-Velay terminus
)

WINDOW_HOURS = 24
DELAY_THRESHOLD_SEC = 300  # 5 min = SNCF "en retard" threshold
MIN_CONNECTION_GAP_MIN = 3  # User's rule: less than 3 min = missed
CONNECTION_WINDOW_MIN = 60  # Look for next train within this window

RELEVANT_LINES = {"REGIONAURA"}


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def load_rows(window_hours: int) -> list[dict]:
    cutoff = datetime.now(PARIS_TZ).replace(tzinfo=None) - timedelta(hours=window_hours)
    rows: list[dict] = []
    for f in sorted(DATA_DIR.glob("*.jsonl.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                dt = parse_dt(row.get("base_dt"))
                if dt and dt >= cutoff and row.get("line_name") in RELEVANT_LINES:
                    rows.append(row)
    return rows


def is_on_axis(stops: list[dict]) -> bool:
    """A train is on the Lyon ↔ Le Puy axis if its journey crosses at least
    two of the hub groups (Lyon, Saint-Étienne, Le Puy)."""
    visited_groups = sum(
        1 for hub in AXIS_HUBS if any(s["stop_id"] in hub for s in stops)
    )
    return visited_groups >= 2


def build_journeys(rows: list[dict]) -> dict[str, list[dict]]:
    by_vj: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_vj[r["vj_id"]].append(r)
    for stops in by_vj.values():
        stops.sort(key=lambda x: x["base_dt"])
    return by_vj


def find_stop(stops: list[dict], stop_id: str, kind: str) -> dict | None:
    for s in stops:
        if s["stop_id"] == stop_id and s["kind"] == kind:
            return s
    return None


def from_lyon_to_ste(stops: list[dict]) -> bool:
    ste_arr = find_stop(stops, STE_HUB, "arr")
    if not ste_arr:
        return False
    lyon = next((s for s in stops if s["stop_id"] in LYON_STOPS), None)
    return bool(lyon and lyon["base_dt"] < ste_arr["base_dt"])


def from_ste_to_lepuy(stops: list[dict]) -> bool:
    ste_dep = find_stop(stops, STE_HUB, "dep")
    le_puy_arr = find_stop(stops, LE_PUY, "arr")
    return bool(ste_dep and le_puy_arr and ste_dep["base_dt"] < le_puy_arr["base_dt"])


def from_lepuy_to_ste(stops: list[dict]) -> bool:
    ste_arr = find_stop(stops, STE_HUB, "arr")
    le_puy_dep = find_stop(stops, LE_PUY, "dep")
    return bool(ste_arr and le_puy_dep and le_puy_dep["base_dt"] < ste_arr["base_dt"])


def from_ste_to_lyon(stops: list[dict]) -> bool:
    ste_dep = find_stop(stops, STE_HUB, "dep")
    if not ste_dep:
        return False
    lyon_arrs = [s for s in stops if s["stop_id"] in LYON_STOPS and s["kind"] == "arr"]
    return any(ste_dep["base_dt"] < la["base_dt"] for la in lyon_arrs)


def max_arrival_delay(stops: list[dict]) -> int | None:
    delays = [s["delay_sec"] for s in stops if s["kind"] == "arr" and s.get("delay_sec") is not None]
    return max(delays) if delays else None


def find_connections(
    journeys: dict[str, list[dict]],
    inbound_filter: Callable[[list[dict]], bool],
    outbound_filter: Callable[[list[dict]], bool],
) -> list[dict]:
    """For each inbound arrival at St-Étienne, identify the next outbound
    departure and compute whether the connection was missed."""
    outbound: list[dict] = []
    for vj, stops in journeys.items():
        if outbound_filter(stops):
            d = find_stop(stops, STE_HUB, "dep")
            if d:
                outbound.append({"vj_id": vj, "stop": d, "stops": stops})
    outbound.sort(key=lambda x: x["stop"]["base_dt"])

    results: list[dict] = []
    for vj, stops in journeys.items():
        if not inbound_filter(stops):
            continue
        a = find_stop(stops, STE_HUB, "arr")
        if not a or a.get("delay_sec") is None:
            continue
        a_base = parse_dt(a["base_dt"])
        a_real = parse_dt(a.get("realtime_dt")) or a_base

        candidates = [
            d for d in outbound
            if parse_dt(d["stop"]["base_dt"]) and parse_dt(d["stop"]["base_dt"]) > a_base
            and (parse_dt(d["stop"]["base_dt"]) - a_base) <= timedelta(minutes=CONNECTION_WINDOW_MIN)
        ]
        if not candidates:
            continue
        intended = candidates[0]
        i_base = parse_dt(intended["stop"]["base_dt"])
        i_real = parse_dt(intended["stop"].get("realtime_dt")) or i_base

        gap_min = (i_real - a_real).total_seconds() / 60
        missed = gap_min < MIN_CONNECTION_GAP_MIN

        result = {
            "inbound_vj": vj,
            "inbound_stops": stops,
            "arr_base": a_base,
            "arr_real": a_real,
            "arr_delay_sec": a["delay_sec"],
            "intended_vj": intended["vj_id"],
            "intended_stops": intended["stops"],
            "intended_dep_base": i_base,
            "intended_dep_real": i_real,
            "missed": missed,
            "gap_min": gap_min,
        }
        if missed:
            idx = outbound.index(intended)
            next_dep = outbound[idx + 1] if idx + 1 < len(outbound) else None
            if next_dep:
                n_real = parse_dt(next_dep["stop"].get("realtime_dt")) or parse_dt(next_dep["stop"]["base_dt"])
                result["next_vj"] = next_dep["vj_id"]
                result["next_stops"] = next_dep["stops"]
                result["next_dep_real"] = n_real
                result["added_delay_sec"] = int((n_real - i_base).total_seconds())
        results.append(result)
    return results


def fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def train_label(stops: list[dict]) -> str:
    return stops[-1].get("train_name") or stops[-1].get("vj_id", "?")


def write_no_data() -> None:
    now = datetime.now(timezone.utc)
    OUT_FILE.write_text(
        f"# Statistiques TER Lyon ↔ Le Puy\n\n"
        f"_Mis à jour le {now:%Y-%m-%d %H:%M UTC} — pas de donnée disponible sur "
        f"la fenêtre des dernières {WINDOW_HOURS} heures._\n"
    )


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    rows = load_rows(WINDOW_HOURS)
    if not rows:
        write_no_data()
        print(f"Wrote {OUT_FILE} (no data)")
        return

    journeys = build_journeys(rows)
    # Restrict to trains that actually run on the Lyon ↔ Le Puy axis.
    journeys = {vj: stops for vj, stops in journeys.items() if is_on_axis(stops)}

    delays: dict[str, int] = {}
    for vj, stops in journeys.items():
        d = max_arrival_delay(stops)
        if d is not None:
            delays[vj] = d
    total = len(delays)
    delayed = {vj: d for vj, d in delays.items() if d >= DELAY_THRESHOLD_SEC}
    pct_delayed = (len(delayed) / total * 100) if total else 0.0
    med_delay_min = (median(delays.values()) / 60) if delays else 0.0

    conn_to_lepuy = find_connections(journeys, from_lyon_to_ste, from_ste_to_lepuy)
    conn_to_lyon = find_connections(journeys, from_lepuy_to_ste, from_ste_to_lyon)
    all_conn = conn_to_lepuy + conn_to_lyon
    missed = [c for c in all_conn if c["missed"]]

    # User-experienced wait at St-Étienne for each transfer attempt.
    experienced_waits: list[int] = []
    for c in all_conn:
        if c["missed"] and "next_dep_real" in c:
            wait = (c["next_dep_real"] - c["intended_dep_base"]).total_seconds()
        else:
            wait = (c["intended_dep_real"] - c["intended_dep_base"]).total_seconds()
        experienced_waits.append(int(max(0, wait)))
    med_exp_min = (median(experienced_waits) / 60) if experienced_waits else 0.0

    now = datetime.now(timezone.utc)
    lines: list[str] = []
    lines.append("# Statistiques TER Lyon ↔ Le Puy")
    lines.append("")
    lines.append(
        f"_Mis à jour le {now:%Y-%m-%d %H:%M UTC} — fenêtre des dernières "
        f"{WINDOW_HOURS} heures. Trains REGIONAURA uniquement._"
    )
    lines.append("")
    lines.append("## Vue d'ensemble")
    lines.append("")
    lines.append(f"- **Trains observés (avec donnée realtime)** : {total}")
    lines.append(f"- **Trains en retard ≥ 5 min à l'arrivée** : {len(delayed)} ({pct_delayed:.1f} %)")
    lines.append(f"- **Médiane retard à l'arrivée (sans correspondance)** : {med_delay_min:.1f} min")
    if all_conn:
        pct_missed = len(missed) / len(all_conn) * 100
        lines.append(
            f"- **Correspondances loupées à St-Étienne** : {len(missed)} / {len(all_conn)} "
            f"({pct_missed:.1f} %)"
        )
        lines.append(
            f"- **Médiane retard ressenti à St-Étienne (avec correspondance)** : "
            f"{med_exp_min:.1f} min"
        )
    lines.append("")

    if delayed:
        lines.append("## Trains en retard")
        lines.append("")
        train_rows = []
        for vj in sorted(delayed, key=lambda v: -delayed[v])[:30]:
            stops = journeys[vj]
            train_rows.append([
                train_label(stops),
                (stops[-1].get("direction") or "?")[:40],
                f"+{delayed[vj] // 60} min",
            ])
        lines.append(fmt_table(["Train", "Destination", "Retard arr. max"], train_rows))
        lines.append("")

    if missed:
        lines.append("## Correspondances loupées à St-Étienne Châteaucreux")
        lines.append("")
        rows_out = []
        for c in sorted(missed, key=lambda x: x["arr_real"]):
            inbound = train_label(c["inbound_stops"])
            intended = train_label(c["intended_stops"])
            arr_str = c["arr_real"].strftime("%d/%m %H:%M") + f" (+{c['arr_delay_sec'] // 60}m)"
            if "next_dep_real" in c:
                nxt = train_label(c["next_stops"])
                nxt_str = c["next_dep_real"].strftime("%H:%M")
                added = f"+{c['added_delay_sec'] // 60} min"
            else:
                nxt = "—"
                nxt_str = "(hors fenêtre)"
                added = "—"
            rows_out.append([
                inbound,
                arr_str,
                f"{intended} ({c['intended_dep_real']:%H:%M})",
                f"{nxt} ({nxt_str})",
                added,
            ])
        lines.append(fmt_table(
            ["Train arrivée", "Arr. réelle", "Correspondance prévue", "Correspondance prise", "Retard ajouté"],
            rows_out,
        ))
        lines.append("")

    OUT_FILE.write_text("\n".join(lines))
    print(
        f"Wrote {OUT_FILE} ({len(rows)} rows, {total} trains, "
        f"{len(delayed)} delayed, {len(missed)} missed connections)."
    )


if __name__ == "__main__":
    main()
