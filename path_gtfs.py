"""PATH (Port Authority Trans-Hudson) GTFS loaders.

Mirrors the shapes the MTA loaders in build_commute_site_data.py produce so the
two feeds can be merged into shared in-memory structures before graph
construction.

The PATH feed has these quirks vs. the MTA feed:
- No shapes.txt: route polylines are synthesized from stop sequences.
- No parent_station linking and empty location_type on every stop: stations are
  deduped here by name.
- Route IDs Special1 / Special5 are operational short-turn variants we drop.
"""

from __future__ import annotations

import csv
import math
import statistics
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

Point = Tuple[float, float]

PATH_GTFS_PATH = Path(__file__).resolve().parent / "data" / "path_gtfs.zip"

# Operational short-turn variants — drop so they don't distort headways or
# clutter the route legend.
PATH_DROP_ROUTE_IDS = frozenset({"Special1", "Special5"})

# Stable string complex IDs in a high-numbered range. The MTA loader sorts
# complexes by int(complex_id); these values keep that sort working and are
# guaranteed not to collide with real MTA complex IDs.
PATH_STATION_COMPLEX_IDS: Dict[str, str] = {
    "Newark": "9001",
    "Harrison": "9002",
    "Journal Square": "9003",
    "Grove Street": "9004",
    "Newport": "9005",
    "Exchange Place": "9006",
    "Hoboken": "9007",
    "World Trade Center": "9008",
    "Christopher Street": "9009",
    "9th Street": "9010",
    "14th Street": "9011",
    "23rd Street": "9012",
    "33rd Street": "9013",
}

# Cross-system walking transfers that sit outside the 260m auto-walk radius but
# matter in real life. Format: (path_complex_id, mta_complex_id, walk_meters).
# WTC PATH ↔ Fulton St (MTA complex 628) is the canonical case (~380m underground).
PATH_EXPLICIT_TRANSFERS: List[Tuple[str, str, float]] = [
    ("9008", "628", 380.0),
]


def _read_csv_from_zip(member: str) -> Iterable[dict]:
    with zipfile.ZipFile(PATH_GTFS_PATH) as archive:
        with archive.open(member) as handle:
            reader = csv.DictReader(line.decode("utf-8-sig") for line in handle)
            yield from reader


def _parse_gtfs_time(value: str) -> int:
    hours, minutes, seconds = map(int, value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _lonlat_to_xy(lon: float, lat: float, lat0: float) -> Point:
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = meters_per_deg_lat * math.cos(math.radians(lat0))
    return lon * meters_per_deg_lon, lat * meters_per_deg_lat


def load_path_stations(lat0: float) -> Tuple[List[dict], Dict[str, str]]:
    """Build PATH complex_info records and a stop_id -> complex_id map.

    Returns:
        stations: list of {"id", "name", "point", "routes"} dicts ready to be
            merged into the MTA complex_info table. routes is an empty set —
            it gets filled in by build_graph as trips are streamed.
        stop_to_complex: maps every PATH stop_id (and its N/S suffixed variants,
            for parity with the MTA loader's behavior) to the synthesized
            complex_id.
    """
    stations_by_complex: Dict[str, dict] = {}
    stop_to_complex: Dict[str, str] = {}

    for row in _read_csv_from_zip("stops.txt"):
        name = row["stop_name"].strip()
        complex_id = PATH_STATION_COMPLEX_IDS.get(name)
        if complex_id is None:
            continue
        stop_id = row["stop_id"]
        stop_to_complex[stop_id] = complex_id
        stop_to_complex[f"{stop_id}N"] = complex_id
        stop_to_complex[f"{stop_id}S"] = complex_id

        if complex_id not in stations_by_complex:
            lon = float(row["stop_lon"])
            lat = float(row["stop_lat"])
            stations_by_complex[complex_id] = {
                "id": complex_id,
                "name": name,
                "point": _lonlat_to_xy(lon, lat, lat0),
                "routes": set(),
            }

    stations = sorted(stations_by_complex.values(), key=lambda s: int(s["id"]))
    return stations, stop_to_complex


def load_path_routes_and_shapes(
    lat0: float,
    bbox: Tuple[float, float, float, float],
    stop_to_complex: Dict[str, str],
) -> Tuple[Dict[str, dict], List[dict], Dict[str, dict]]:
    """Build PATH route_styles, synthesized route_shapes, and trips_by_id.

    PATH has no shapes.txt — for each (route_id, direction_id) we pick the
    trip with the most stops and polyline through its stop coordinates in
    stop_sequence order.
    """

    route_styles: Dict[str, dict] = {}
    for row in _read_csv_from_zip("routes.txt"):
        route_id = row["route_id"]
        if route_id in PATH_DROP_ROUTE_IDS:
            continue
        if row.get("route_type") != "1":
            continue
        route_styles[route_id] = {
            "color": f"#{row['route_color'] or '808183'}",
            "textColor": f"#{row['route_text_color'] or 'FFFFFF'}",
            "label": row["route_short_name"] or row["route_id"],
        }

    trips_by_id: Dict[str, dict] = {}
    for row in _read_csv_from_zip("trips.txt"):
        route_id = row["route_id"]
        if route_id not in route_styles:
            continue
        trips_by_id[row["trip_id"]] = {
            "route_id": route_id,
            "direction_id": row.get("direction_id", "0"),
            "service_id": row.get("service_id", ""),
        }

    stop_coords: Dict[str, Point] = {}
    for row in _read_csv_from_zip("stops.txt"):
        if row["stop_id"] in stop_to_complex:
            stop_coords[row["stop_id"]] = _lonlat_to_xy(
                float(row["stop_lon"]), float(row["stop_lat"]), lat0
            )

    trip_stop_counts: Counter = Counter()
    trip_stop_seq: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for row in _read_csv_from_zip("stop_times.txt"):
        trip_id = row["trip_id"]
        if trip_id not in trips_by_id:
            continue
        trip_stop_counts[trip_id] += 1
        trip_stop_seq[trip_id].append((int(row["stop_sequence"]), row["stop_id"]))

    longest_trip_per_dir: Dict[Tuple[str, str], str] = {}
    for trip_id, count in trip_stop_counts.items():
        trip = trips_by_id[trip_id]
        key = (trip["route_id"], trip["direction_id"])
        existing = longest_trip_per_dir.get(key)
        if existing is None or count > trip_stop_counts[existing]:
            longest_trip_per_dir[key] = trip_id

    route_shapes: List[dict] = []
    for (route_id, _direction), trip_id in longest_trip_per_dir.items():
        ordered = sorted(trip_stop_seq[trip_id])
        points: List[Point] = []
        for _, stop_id in ordered:
            point = stop_coords.get(stop_id)
            if point is not None and (not points or point != points[-1]):
                points.append(point)
        if len(points) < 2:
            continue
        style = route_styles[route_id]
        route_shapes.append(
            {
                "routeId": route_id,
                "color": style["color"],
                "textColor": style["textColor"],
                "label": style["label"],
                "points": [[round(x, 1), round(y, 1)] for x, y in points],
            }
        )

    return route_styles, route_shapes, trips_by_id


def compute_path_route_waits(trips_by_id: Dict[str, dict]) -> Dict[str, float]:
    """Estimate per-route board waits from PATH stop_times.txt.

    Same algorithm as build_commute_site_data.build_route_waits: median gap
    between consecutive first-stop departures within each (route, service),
    divided by 2, then median across services, then clamped to [1.5, 8.0].
    """
    departures_by_route_service: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    current_trip_id = None
    first_departure: int | None = None

    for row in _read_csv_from_zip("stop_times.txt"):
        trip_id = row["trip_id"]
        stop_sequence = int(row["stop_sequence"])
        if trip_id != current_trip_id:
            if (
                current_trip_id
                and first_departure is not None
                and current_trip_id in trips_by_id
            ):
                trip = trips_by_id[current_trip_id]
                departures_by_route_service[(trip["route_id"], trip["service_id"])].append(first_departure)
            current_trip_id = trip_id
            first_departure = (
                _parse_gtfs_time(row["departure_time"]) if stop_sequence == 1 else None
            )
        elif stop_sequence == 1 and first_departure is None:
            first_departure = _parse_gtfs_time(row["departure_time"])

    if current_trip_id and first_departure is not None and current_trip_id in trips_by_id:
        trip = trips_by_id[current_trip_id]
        departures_by_route_service[(trip["route_id"], trip["service_id"])].append(first_departure)

    waits_by_route: Dict[str, List[float]] = defaultdict(list)
    for (route_id, _service_id), departures in departures_by_route_service.items():
        departures = sorted(set(departures))
        gaps = [
            (departures[i + 1] - departures[i]) / 60.0
            for i in range(len(departures) - 1)
            if 2 * 60 <= departures[i + 1] - departures[i] <= 30 * 60
        ]
        if gaps:
            waits_by_route[route_id].append(statistics.median(gaps) / 2.0)

    route_waits: Dict[str, float] = {}
    for route_id, waits in waits_by_route.items():
        route_waits[route_id] = round(max(1.5, min(8.0, statistics.median(waits))), 2)
    return route_waits


def compute_path_segment_durations(
    trips_by_id: Dict[str, dict],
    stop_to_complex: Dict[str, str],
    station_index_by_id: Dict[str, int],
    out_durations_by_edge: Dict[Tuple[int, int, str], List[float]],
    out_station_routes: Dict[int, set],
) -> None:
    """Stream stop_times.txt and append PATH segment durations to the shared
    durations_by_edge dict, identical to build_commute_site_data.build_graph's
    first phase. Mutates out_durations_by_edge and out_station_routes in place
    so the caller can run a single graph-construction pass over the union.
    """
    current_trip_id = None
    current_rows: List[dict] = []

    def process_trip(trip_id: str, rows: List[dict]) -> None:
        trip = trips_by_id.get(trip_id)
        if not trip or len(rows) < 2:
            return
        route_id = trip["route_id"]
        ordered = sorted(rows, key=lambda row: int(row["stop_sequence"]))
        for row in ordered:
            complex_id = stop_to_complex.get(row["stop_id"])
            if complex_id in station_index_by_id:
                out_station_routes.setdefault(station_index_by_id[complex_id], set()).add(route_id)
        for prev, nxt in zip(ordered, ordered[1:]):
            from_complex = stop_to_complex.get(prev["stop_id"])
            to_complex = stop_to_complex.get(nxt["stop_id"])
            if not from_complex or not to_complex or from_complex == to_complex:
                continue
            if from_complex not in station_index_by_id or to_complex not in station_index_by_id:
                continue
            duration_seconds = _parse_gtfs_time(nxt["arrival_time"]) - _parse_gtfs_time(prev["departure_time"])
            if 20 <= duration_seconds <= 1800:
                from_index = station_index_by_id[from_complex]
                to_index = station_index_by_id[to_complex]
                out_durations_by_edge[(from_index, to_index, route_id)].append(duration_seconds / 60.0)

    for row in _read_csv_from_zip("stop_times.txt"):
        trip_id = row["trip_id"]
        if current_trip_id is None:
            current_trip_id = trip_id
        if trip_id != current_trip_id:
            process_trip(current_trip_id, current_rows)
            current_trip_id = trip_id
            current_rows = []
        current_rows.append(row)
    if current_trip_id and current_rows:
        process_trip(current_trip_id, current_rows)
