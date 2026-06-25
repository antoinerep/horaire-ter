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


def normalize_vj_id(vj_id: str) -> str:
    """Navitia re-issues a train under a ":RealTime:UUID"-suffixed vj_id each
    time its realtime state changes. Strip the suffix so every variant of the
    same physical train shares one canonical id."""
    parts = vj_id.split(":")
    return ":".join(parts[:6]) if len(parts) > 6 else vj_id


def load_rows(window_hours: int) -> list[dict]:
    cutoff = datetime.now(PARIS_TZ).replace(tzinfo=None) - timedelta(hours=window_hours)
    rows: list[dict] = []
    for f in sorted(DATA_DIR.glob("*.jsonl.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                dt = parse_dt(row.get("base_dt"))
                if dt and dt >= cutoff and row.get("line_name") in RELEVANT_LINES:
                    row["vj_id"] = normalize_vj_id(row["vj_id"])
                    rows.append(row)
    return rows


def is_on_axis(stops: list[dict]) -> bool:
    """A train is on the Lyon ↔ Le Puy axis if its journey crosses at least
    two of the hub groups (Lyon, Saint-Étienne, Le Puy)."""
    visited_groups = sum(
        1 for hub in AXIS_HUBS if any(s["stop_id"] in hub for s in stops)
    )
    return visited_groups >= 2


def _row_quality(r: dict) -> tuple:
    """Realtime > base_schedule > cancelled. Within the same tier, the most
    recent observation (run_ts) wins so we keep the freshest delay value."""
    if r.get("cancelled"):
        return (0, r.get("run_ts", ""))
    if r.get("freshness") == "realtime":
        return (2, r.get("run_ts", ""))
    return (1, r.get("run_ts", ""))


def build_journeys(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by canonical vj_id, keeping only the best-quality row per
    (stop, kind, base_dt) so the same train doesn't appear multiple times when
    Navitia issued several realtime variants."""
    by_vj: dict[str, dict[tuple, dict]] = defaultdict(dict)
    for r in rows:
        ev_key = (r["stop_id"], r["kind"], r["base_dt"])
        prev = by_vj[r["vj_id"]].get(ev_key)
        if prev is None or _row_quality(r) > _row_quality(prev):
            by_vj[r["vj_id"]][ev_key] = r
    out: dict[str, list[dict]] = {}
    for vj, events in by_vj.items():
        stops = list(events.values())
        stops.sort(key=lambda x: x["base_dt"])
        out[vj] = stops
    return out


def find_stop(stops: list[dict], stop_id: str, kind: str) -> dict | None:
    for s in stops:
        if s["stop_id"] == stop_id and s["kind"] == kind:
            return s
    return None


def best_event(stops: list[dict], stop_id: str, preferred_kind: str) -> dict | None:
    """Return the event at stop_id matching preferred_kind, falling back to
    any kind at the same stop. Recovers terminus events that were emitted
    under the opposite kind label by older collect.py runs."""
    primary = find_stop(stops, stop_id, preferred_kind)
    if primary:
        return primary
    return next((s for s in stops if s["stop_id"] == stop_id), None)


def visits(stops: list[dict], stop_id: str) -> dict | None:
    """First event observed at a given stop, regardless of kind. Robust to
    the terminus quirk where arrival/departure share a single timestamp."""
    return next((s for s in stops if s["stop_id"] == stop_id), None)


def visits_group(stops: list[dict], group: set[str]) -> dict | None:
    return next((s for s in stops if s["stop_id"] in group), None)


def from_lyon_to_ste(stops: list[dict]) -> bool:
    ste = visits(stops, STE_HUB)
    lyon = visits_group(stops, LYON_STOPS)
    return bool(ste and lyon and lyon["base_dt"] < ste["base_dt"])


def from_ste_to_lepuy(stops: list[dict]) -> bool:
    ste = visits(stops, STE_HUB)
    le_puy = visits(stops, LE_PUY)
    return bool(ste and le_puy and ste["base_dt"] < le_puy["base_dt"])


def from_lepuy_to_ste(stops: list[dict]) -> bool:
    ste = visits(stops, STE_HUB)
    le_puy = visits(stops, LE_PUY)
    return bool(ste and le_puy and le_puy["base_dt"] < ste["base_dt"])


def from_ste_to_lyon(stops: list[dict]) -> bool:
    ste = visits(stops, STE_HUB)
    lyon = visits_group(stops, LYON_STOPS)
    return bool(ste and lyon and ste["base_dt"] < lyon["base_dt"])


def max_arrival_delay(stops: list[dict]) -> int | None:
    delays = [s["delay_sec"] for s in stops if s["kind"] == "arr" and s.get("delay_sec") is not None]
    return max(delays) if delays else None


def origin_stop(stops: list[dict]) -> dict | None:
    """First observed departure on the axis (may not be the train's true origin
    if the train started outside our axis stops)."""
    deps = [s for s in stops if s["kind"] == "dep"]
    if deps:
        return deps[0]
    return stops[0] if stops else None


def origin_scheduled_dt(stops: list[dict]) -> datetime | None:
    o = origin_stop(stops)
    return parse_dt(o["base_dt"]) if o else None


def origin_stop_name(stops: list[dict]) -> str:
    o = origin_stop(stops)
    return o.get("stop_name", "?") if o else "?"


def hub_delay_sec(stops: list[dict]) -> int | None:
    """Worst delay observed at Saint-Étienne Châteaucreux for this train."""
    hub = [
        s["delay_sec"]
        for s in stops
        if s["stop_id"] == STE_HUB and s.get("delay_sec") is not None
    ]
    return max(hub) if hub else None


def is_origin_cancelled(stops: list[dict]) -> bool:
    o = origin_stop(stops)
    return bool(o and o.get("cancelled"))


def direction_label(stops: list[dict]) -> str:
    return (stops[-1].get("direction") or "").strip()


def substitute_delay_sec(stops: list[dict], all_journeys: dict[str, list[dict]]) -> int | None:
    """For a cancelled train, find the next non-cancelled train departing the
    same stop in the same direction. Return wait time (realtime of next train
    minus scheduled time of cancelled train), in seconds."""
    o = origin_stop(stops)
    if not o:
        return None
    cancelled_base = parse_dt(o["base_dt"])
    direction = direction_label(stops)
    if not cancelled_base or not direction:
        return None
    candidates: list[tuple[datetime, datetime]] = []
    for vj, ss in all_journeys.items():
        if direction_label(ss) != direction:
            continue
        ev = next(
            (s for s in ss if s["stop_id"] == o["stop_id"] and s["kind"] == o["kind"]),
            None,
        )
        if not ev or ev.get("cancelled"):
            continue
        ev_base = parse_dt(ev["base_dt"])
        if not ev_base or ev_base <= cancelled_base:
            continue
        ev_real = parse_dt(ev.get("realtime_dt")) or ev_base
        candidates.append((ev_base, ev_real))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    _, next_real = candidates[0]
    return int((next_real - cancelled_base).total_seconds())


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
            d = best_event(stops, STE_HUB, "dep")
            if d and not d.get("cancelled"):
                outbound.append({"vj_id": vj, "stop": d, "stops": stops})
    outbound.sort(key=lambda x: x["stop"]["base_dt"])

    results: list[dict] = []
    for vj, stops in journeys.items():
        if not inbound_filter(stops):
            continue
        a = best_event(stops, STE_HUB, "arr")
        if not a or a.get("cancelled"):
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


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (matches numpy default)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    if f >= len(s) - 1:
        return float(s[-1])
    return s[f] + (s[f + 1] - s[f]) * (k - f)


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

    # Cancelled trains: substitute delay = wait for next same-direction train.
    cancelled: dict[str, int | None] = {}
    for vj, stops in journeys.items():
        if is_origin_cancelled(stops):
            cancelled[vj] = substitute_delay_sec(stops, journeys)

    # Delays from realtime observations (excludes cancelled trains).
    delays: dict[str, int] = {}
    for vj, stops in journeys.items():
        if vj in cancelled:
            continue
        d = max_arrival_delay(stops)
        if d is not None:
            delays[vj] = d

    total_observed = len(delays) + len(cancelled)
    delayed = {vj: d for vj, d in delays.items() if d >= DELAY_THRESHOLD_SEC}
    n_disrupted = len(delayed) + len(cancelled)
    pct_disrupted = (n_disrupted / total_observed * 100) if total_observed else 0.0
    # Median delay including cancellations: use substitute delay where known.
    all_delay_values = list(delays.values()) + [d for d in cancelled.values() if d is not None]
    med_delay_min = (median(all_delay_values) / 60) if all_delay_values else 0.0

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
    lines.append(f"- **Trains observés** : {total_observed}")
    lines.append(f"- **Trains annulés** : {len(cancelled)}")
    lines.append(
        f"- **Trains en retard ≥ 5 min ou annulés** : {n_disrupted} ({pct_disrupted:.1f} %)"
    )
    lines.append("")
    lines.append(
        "**Distribution des retards à l'arrivée** _(hors correspondance, "
        "annulations comptées comme attente du train suivant)_ :"
    )
    lines.append("")
    if all_delay_values:
        pct_rows = []
        for p in (50, 75, 90, 95, 99):
            v_min = percentile(all_delay_values, p) / 60
            label = "à l'heure" if v_min < 0.5 else f"retard ≤ {v_min:.1f} min"
            pct_rows.append([f"{p} %", label])
        lines.append(fmt_table(["Percentile", "Trains au-dessous"], pct_rows))
    else:
        lines.append("_(pas assez de données)_")
    lines.append("")
    if all_conn:
        pct_missed = len(missed) / len(all_conn) * 100
        lines.append(
            f"**Correspondances à St-Étienne Châteaucreux** : {len(all_conn)} analysées, "
            f"**{len(missed)} loupées** ({pct_missed:.1f} %). Médiane retard "
            f"ressenti à St-Étienne : {med_exp_min:.1f} min."
        )
        lines.append("")

    # Combined table: delayed + cancelled trains, sorted by effective delay.
    disrupted: list[tuple[str, int, str]] = []  # (vj, effective_delay_sec, status)
    for vj, d in delayed.items():
        disrupted.append((vj, d, "Retard"))
    for vj, d in cancelled.items():
        disrupted.append((vj, d if d is not None else -1, "ANNULÉ"))
    if disrupted:
        lines.append("## Trains en retard ou annulés")
        lines.append("")
        train_rows = []
        for vj, eff_delay, status in sorted(disrupted, key=lambda x: -x[1])[:30]:
            stops = journeys[vj]
            sched = origin_scheduled_dt(stops)
            hub = hub_delay_sec(stops)
            if status == "ANNULÉ":
                delay_str = (
                    f"+{eff_delay // 60} min (train suivant)" if eff_delay >= 0 else "—"
                )
                hub_str = "—"
            else:
                delay_str = f"+{eff_delay // 60} min"
                hub_str = f"+{hub // 60} min" if hub is not None else "—"
            train_rows.append([
                train_label(stops),
                sched.strftime("%d/%m") if sched else "?",
                sched.strftime("%H:%M") if sched else "?",
                origin_stop_name(stops),
                (stops[-1].get("direction") or "?")[:40],
                status,
                delay_str,
                hub_str,
            ])
        lines.append(fmt_table(
            ["Train", "Jour", "Heure prévue", "Origine", "Destination", "Statut", "Retard ressenti", "Retard à St-Étienne"],
            train_rows,
        ))
        lines.append("")

    if all_conn:
        # Tag each connection with its direction so we can show "Lyon → Le Puy"
        # vs "Le Puy → Lyon" in the table.
        for c in conn_to_lepuy:
            c["sens"] = "Lyon → Le Puy"
        for c in conn_to_lyon:
            c["sens"] = "Le Puy → Lyon"

        lines.append("## Correspondances à St-Étienne Châteaucreux")
        lines.append("")
        lines.append(
            f"{len(all_conn)} correspondances observées, dont **{len(missed)} loupées** "
            f"(gap < {MIN_CONNECTION_GAP_MIN} min après l'arrivée réelle)."
        )
        lines.append("")
        rows_out = []
        # Show missed first, then on-time, ordered by experienced delay desc
        def conn_sort_key(c):
            if c["missed"] and "next_dep_real" in c:
                exp = c["added_delay_sec"]
            else:
                exp = int((c["intended_dep_real"] - c["intended_dep_base"]).total_seconds())
            return (-int(c["missed"]), -exp)

        for c in sorted(all_conn, key=conn_sort_key)[:30]:
            sens = c.get("sens", "?")
            inbound_name = train_label(c["inbound_stops"])
            origin = origin_stop_name(c["inbound_stops"])
            day_str = c["arr_base"].strftime("%d/%m")
            arr_delay_sec = c.get("arr_delay_sec") or 0
            arr_str = c["arr_real"].strftime("%H:%M") + (
                f" (+{arr_delay_sec // 60}m)" if arr_delay_sec >= 60 else ""
            )
            if c["missed"]:
                if "next_dep_real" in c:
                    taken_stops = c["next_stops"]
                    taken_real = c["next_dep_real"]
                    exp_sec = c["added_delay_sec"]
                    status = "LOUPÉE"
                else:
                    taken_stops = None
                    exp_sec = None
                    status = "LOUPÉE (hors fenêtre)"
            else:
                taken_stops = c["intended_stops"]
                taken_real = c["intended_dep_real"]
                exp_sec = int((c["intended_dep_real"] - c["intended_dep_base"]).total_seconds())
                status = "à l'heure"
            if taken_stops is not None:
                taken_name = train_label(taken_stops)
                taken_dest = (taken_stops[-1].get("direction") or "?")[:40]
                taken_str = f"{taken_name} {taken_real:%H:%M}"
            else:
                taken_name = "—"
                taken_dest = "—"
                taken_str = "—"
            exp_str = f"+{max(0, exp_sec) // 60} min" if exp_sec is not None else "—"
            rows_out.append([
                day_str,
                sens,
                inbound_name,
                origin,
                arr_str,
                taken_str,
                taken_dest,
                status,
                exp_str,
            ])
        lines.append(fmt_table(
            ["Jour", "Sens", "Train arr.", "Origine", "Arr. St-Étienne", "Train pris", "Destination", "Statut", "Retard ressenti"],
            rows_out,
        ))
        lines.append("")

    OUT_FILE.write_text("\n".join(lines))
    print(
        f"Wrote {OUT_FILE} ({len(rows)} rows, {total_observed} trains, "
        f"{len(delayed)} delayed, {len(cancelled)} cancelled, "
        f"{len(missed)} missed connections)."
    )


if __name__ == "__main__":
    main()
