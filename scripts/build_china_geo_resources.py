#!/usr/bin/env python3
"""Build RunLog's China administrative-boundary and enriched peak databases."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import osmium


Point = tuple[float, float]  # longitude, latitude
Ring = list[Point]
Polygon = list[Ring]
MultiPolygon = list[Polygon]


@dataclass(frozen=True)
class OfficialArea:
    code: str
    name: str
    level: int
    province_code: str
    prefecture_code: str


@dataclass
class BoundaryArea:
    osm_id: int
    level: int
    name: str
    ref_code: str | None
    polygons: MultiPolygon
    bounds: tuple[float, float, float, float]
    center: Point
    code: str | None = None
    province_code: str | None = None
    province_name: str | None = None
    prefecture_code: str | None = None
    prefecture_name: str | None = None

    @property
    def bounds_area(self) -> float:
        min_lon, min_lat, max_lon, max_lat = self.bounds
        return max(max_lon - min_lon, 0) * max(max_lat - min_lat, 0)


def official_level(code: str) -> int:
    if code.endswith("0000"):
        return 4
    if code.endswith("00"):
        return 5
    return 6


def load_official_areas(path: Path) -> dict[str, OfficialArea]:
    raw: list[tuple[str, str]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            code = row["code"].strip()
            name = row["name"].strip()
            if len(code) == 6 and name:
                raw.append((code, name))

    codes = {code for code, _ in raw}
    result: dict[str, OfficialArea] = {}
    for code, name in raw:
        level = official_level(code)
        province_code = f"{code[:2]}0000"
        expected_prefecture = f"{code[:4]}00"
        prefecture_code = expected_prefecture if expected_prefecture in codes else province_code
        result[code] = OfficialArea(
            code=code,
            name=name,
            level=level,
            province_code=province_code,
            prefecture_code=prefecture_code,
        )
    if len(result) < 2_500:
        raise RuntimeError(f"official code snapshot contains only {len(result)} rows")
    return result


def normalized_ref_code(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 2:
        return f"{digits}0000"
    if len(digits) == 4:
        return f"{digits}00"
    if len(digits) == 6:
        return digits
    return None


def squared_distance_to_segment(point: Point, start: Point, end: Point) -> float:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return (px - x1) ** 2 + (py - y1) ** 2
    ratio = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    x = x1 + ratio * dx
    y = y1 + ratio * dy
    return (px - x) ** 2 + (py - y) ** 2


def simplify_line(points: list[Point], tolerance: float) -> list[Point]:
    if len(points) <= 2 or tolerance <= 0:
        return points
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    threshold = tolerance * tolerance
    while stack:
        first, last = stack.pop()
        best_index = -1
        best_distance = threshold
        for index in range(first + 1, last):
            distance = squared_distance_to_segment(points[index], points[first], points[last])
            if distance > best_distance:
                best_index = index
                best_distance = distance
        if best_index >= 0:
            keep.add(best_index)
            stack.append((first, best_index))
            stack.append((best_index, last))
    return [points[index] for index in sorted(keep)]


def simplify_ring(ring: Iterable[Iterable[float]], tolerance: float) -> Ring:
    points: Ring = []
    for raw_point in ring:
        lon, lat = float(raw_point[0]), float(raw_point[1])
        point = (round(lon, 6), round(lat, 6))
        if not points or point != points[-1]:
            points.append(point)
    if len(points) < 4:
        return []
    if points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        return []

    opposite = max(range(1, len(points)), key=lambda index: (
        (points[index][0] - points[0][0]) ** 2 + (points[index][1] - points[0][1]) ** 2
    ))
    first_half = simplify_line(points[: opposite + 1], tolerance)
    second_half = simplify_line(points[opposite:] + [points[0]], tolerance)
    simplified = first_half[:-1] + second_half[:-1]
    if len(simplified) < 3:
        simplified = points
    simplified.append(simplified[0])
    return simplified


def simplify_geometry(raw: dict, tolerance: float) -> MultiPolygon:
    coordinates = raw.get("coordinates", [])
    if raw.get("type") == "Polygon":
        coordinates = [coordinates]
    if raw.get("type") not in {"Polygon", "MultiPolygon"}:
        return []

    polygons: MultiPolygon = []
    for raw_polygon in coordinates:
        rings = [simplify_ring(ring, tolerance) for ring in raw_polygon]
        rings = [ring for ring in rings if len(ring) >= 4]
        if rings:
            polygons.append(rings)
    return polygons


def geometry_bounds(polygons: MultiPolygon) -> tuple[float, float, float, float]:
    points = [point for polygon in polygons for ring in polygon for point in ring]
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def point_in_ring(point: Point, ring: Ring) -> bool:
    x, y = point
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if squared_distance_to_segment(point, previous, current) < 1e-18:
            return True
        if (y1 > y) != (y2 > y):
            intersection = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection:
                inside = not inside
        previous = current
    return inside


def point_in_geometry(point: Point, polygons: MultiPolygon) -> bool:
    for polygon in polygons:
        if not polygon or not point_in_ring(point, polygon[0]):
            continue
        if any(point_in_ring(point, hole) for hole in polygon[1:]):
            continue
        return True
    return False


def ring_area_and_centroid(ring: Ring) -> tuple[float, Point]:
    area_twice = 0.0
    cx = 0.0
    cy = 0.0
    for first, second in zip(ring, ring[1:]):
        cross = first[0] * second[1] - second[0] * first[1]
        area_twice += cross
        cx += (first[0] + second[0]) * cross
        cy += (first[1] + second[1]) * cross
    if abs(area_twice) < 1e-12:
        return 0.0, ring[0]
    return area_twice / 2.0, (cx / (3.0 * area_twice), cy / (3.0 * area_twice))


def representative_point(polygons: MultiPolygon) -> Point:
    outer_rings = [polygon[0] for polygon in polygons if polygon]
    largest = max(outer_rings, key=lambda ring: abs(ring_area_and_centroid(ring)[0]))
    _, centroid = ring_area_and_centroid(largest)
    if point_in_geometry(centroid, polygons):
        return centroid

    min_lon, min_lat, max_lon, max_lat = geometry_bounds(polygons)
    for grid_size in (5, 11, 21):
        for row in range(grid_size):
            for column in range(grid_size):
                candidate = (
                    min_lon + (column + 0.5) * (max_lon - min_lon) / grid_size,
                    min_lat + (row + 0.5) * (max_lat - min_lat) / grid_size,
                )
                if point_in_geometry(candidate, polygons):
                    return candidate
    return largest[0]


class AdministrativeAreaCollector(osmium.SimpleHandler):
    def __init__(self, tolerance: float) -> None:
        super().__init__()
        self.tolerance = tolerance
        self.factory = osmium.geom.GeoJSONFactory()
        self.areas: dict[int, BoundaryArea] = {}
        self.geometry_failures = 0

    def area(self, area: osmium.osm.Area) -> None:
        if area.from_way() or area.tags.get("boundary") != "administrative":
            return
        try:
            level = int(area.tags.get("admin_level", "0"))
        except ValueError:
            return
        if level not in {4, 5, 6}:
            return
        name = area.tags.get("name:zh-Hans") or area.tags.get("name:zh") or area.tags.get("name")
        if not name:
            return
        try:
            raw_geometry = json.loads(self.factory.create_multipolygon(area))
            polygons = simplify_geometry(raw_geometry, self.tolerance)
            if not polygons:
                return
            bounds = geometry_bounds(polygons)
            center = representative_point(polygons)
        except (RuntimeError, ValueError, KeyError, TypeError):
            self.geometry_failures += 1
            return

        self.areas[area.orig_id()] = BoundaryArea(
            osm_id=area.orig_id(),
            level=level,
            name=name.strip(),
            ref_code=normalized_ref_code(area.tags.get("ref:admin:CN") or area.tags.get("ref")),
            polygons=polygons,
            bounds=bounds,
            center=center,
        )


def containing_area(point: Point, candidates: Iterable[BoundaryArea]) -> BoundaryArea | None:
    matches: list[BoundaryArea] = []
    for area in candidates:
        min_lon, min_lat, max_lon, max_lat = area.bounds
        if min_lon <= point[0] <= max_lon and min_lat <= point[1] <= max_lat:
            if point_in_geometry(point, area.polygons):
                matches.append(area)
    return min(matches, key=lambda area: area.bounds_area) if matches else None


def resolve_hierarchy(areas: list[BoundaryArea], official: dict[str, OfficialArea]) -> None:
    official_by_level_name: dict[tuple[int, str], list[OfficialArea]] = {}
    for item in official.values():
        official_by_level_name.setdefault((item.level, item.name), []).append(item)

    provinces = [area for area in areas if area.level == 4]
    prefectures = [area for area in areas if area.level == 5]
    counties = [area for area in areas if area.level == 6]

    for area in provinces:
        item = official.get(area.ref_code or "")
        if item is None or item.level != 4:
            matches = official_by_level_name.get((4, area.name), [])
            item = matches[0] if len(matches) == 1 else None
        if item:
            area.code = item.code
            area.province_code = item.code
            area.province_name = item.name

    for area in prefectures:
        item = official.get(area.ref_code or "")
        parent = containing_area(area.center, provinces)
        if item is None or item.level != 5:
            matches = official_by_level_name.get((5, area.name), [])
            if parent and parent.code:
                matches = [match for match in matches if match.province_code == parent.code]
            item = matches[0] if len(matches) == 1 else None
        if item:
            area.code = item.code
            area.province_code = item.province_code
            area.province_name = official.get(item.province_code, item).name
            area.prefecture_code = item.code
            area.prefecture_name = item.name
        elif parent:
            area.province_code = parent.code
            area.province_name = parent.province_name or parent.name
            area.prefecture_code = f"osm:{area.osm_id}"
            area.prefecture_name = area.name

    for area in counties:
        item = official.get(area.ref_code or "")
        parent_prefecture = containing_area(area.center, prefectures)
        parent_province = containing_area(area.center, provinces)
        if item is None or item.level != 6:
            matches = official_by_level_name.get((6, area.name), [])
            if parent_prefecture and parent_prefecture.code:
                matches = [match for match in matches if match.prefecture_code == parent_prefecture.code]
            elif parent_province and parent_province.code:
                matches = [match for match in matches if match.province_code == parent_province.code]
            item = matches[0] if len(matches) == 1 else None

        if item:
            area.code = item.code
            area.province_code = item.province_code
            area.province_name = official.get(item.province_code, item).name
            area.prefecture_code = item.prefecture_code
            area.prefecture_name = official.get(item.prefecture_code, official.get(item.province_code, item)).name
        else:
            area.code = f"osm:{area.osm_id}"
            if parent_prefecture:
                area.province_code = parent_prefecture.province_code
                area.province_name = parent_prefecture.province_name
                area.prefecture_code = parent_prefecture.code or f"osm:{parent_prefecture.osm_id}"
                area.prefecture_name = parent_prefecture.name
            elif parent_province:
                area.province_code = parent_province.code or f"osm:{parent_province.osm_id}"
                area.province_name = parent_province.province_name or parent_province.name
                area.prefecture_code = area.province_code
                area.prefecture_name = area.province_name


def encode_geometry(polygons: MultiPolygon) -> bytes:
    output = bytearray(b"RLB1")
    output.extend(struct.pack("<I", len(polygons)))
    for polygon in polygons:
        output.extend(struct.pack("<I", len(polygon)))
        for ring in polygon:
            output.extend(struct.pack("<I", len(ring)))
            for lon, lat in ring:
                output.extend(struct.pack("<ff", lon, lat))
    return bytes(output)


def create_boundary_database(path: Path, counties: list[BoundaryArea], version: str) -> None:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE admin_area (
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                province_code TEXT,
                province_name TEXT,
                prefecture_code TEXT,
                prefecture_name TEXT,
                centroid_lat REAL NOT NULL,
                centroid_lon REAL NOT NULL,
                min_lat REAL NOT NULL,
                min_lon REAL NOT NULL,
                max_lat REAL NOT NULL,
                max_lon REAL NOT NULL,
                geometry BLOB NOT NULL
            );
            CREATE VIRTUAL TABLE admin_area_rtree USING rtree(id, min_lon, max_lon, min_lat, max_lat);
            CREATE INDEX idx_admin_area_code ON admin_area(code);
            CREATE INDEX idx_admin_area_prefecture ON admin_area(prefecture_code);
            """
        )
        metadata = {
            "schema_version": "1",
            "boundary_version": version,
            "boundary_source": "OpenStreetMap via Geofabrik",
            "geometry_format": "RLB1-float32",
            "license": "ODbL-1.0",
        }
        connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items())
        for area in counties:
            min_lon, min_lat, max_lon, max_lat = area.bounds
            connection.execute(
                """
                INSERT INTO admin_area(
                    id, code, name, province_code, province_name, prefecture_code, prefecture_name,
                    centroid_lat, centroid_lon, min_lat, min_lon, max_lat, max_lon, geometry
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    area.osm_id,
                    area.code or f"osm:{area.osm_id}",
                    area.name,
                    area.province_code,
                    area.province_name,
                    area.prefecture_code,
                    area.prefecture_name,
                    area.center[1],
                    area.center[0],
                    min_lat,
                    min_lon,
                    max_lat,
                    max_lon,
                    encode_geometry(area.polygons),
                ),
            )
            connection.execute(
                "INSERT INTO admin_area_rtree(id, min_lon, max_lon, min_lat, max_lat) VALUES (?, ?, ?, ?, ?)",
                (area.osm_id, min_lon, max_lon, min_lat, max_lat),
            )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


class BoundaryMatcher:
    def __init__(self, counties: list[BoundaryArea], cell_size: float = 1.0) -> None:
        self.counties = {area.osm_id: area for area in counties}
        self.cell_size = cell_size
        self.grid: dict[tuple[int, int], list[int]] = {}
        for area in counties:
            min_lon, min_lat, max_lon, max_lat = area.bounds
            for x in range(math.floor(min_lon / cell_size), math.floor(max_lon / cell_size) + 1):
                for y in range(math.floor(min_lat / cell_size), math.floor(max_lat / cell_size) + 1):
                    self.grid.setdefault((x, y), []).append(area.osm_id)

    def match(self, lon: float, lat: float) -> BoundaryArea | None:
        identifiers = self.grid.get((math.floor(lon / self.cell_size), math.floor(lat / self.cell_size)), [])
        matches: list[BoundaryArea] = []
        for identifier in identifiers:
            area = self.counties[identifier]
            min_lon, min_lat, max_lon, max_lat = area.bounds
            if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
                if point_in_geometry((lon, lat), area.polygons):
                    matches.append(area)
        return min(matches, key=lambda area: area.bounds_area) if matches else None


def add_column_if_needed(connection: sqlite3.Connection, name: str, declaration: str) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(osm_peak)")}
    if name not in columns:
        connection.execute(f"ALTER TABLE osm_peak ADD COLUMN {name} {declaration}")


def enrich_peak_database(source: Path, destination: Path, matcher: BoundaryMatcher, version: str) -> tuple[int, int]:
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    connection = sqlite3.connect(destination)
    try:
        for name in (
            "admin_province_code", "admin_province_name",
            "admin_prefecture_code", "admin_prefecture_name",
            "admin_county_code", "admin_county_name", "admin_boundary_version",
        ):
            add_column_if_needed(connection, name, "TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_osm_peak_admin_county ON osm_peak(admin_county_code)")
        connection.execute("CREATE TABLE IF NOT EXISTS geo_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT OR REPLACE INTO geo_metadata(key, value) VALUES ('admin_boundary_version', ?)",
            (version,),
        )

        total = 0
        matched = 0
        updates = []
        for peak_id, lat, lon in connection.execute("SELECT id, lat, lon FROM osm_peak"):
            total += 1
            area = matcher.match(float(lon), float(lat))
            if area:
                matched += 1
                updates.append((
                    area.province_code,
                    area.province_name,
                    area.prefecture_code,
                    area.prefecture_name,
                    area.code,
                    area.name,
                    version,
                    peak_id,
                ))
            if len(updates) >= 1_000:
                connection.executemany(
                    """
                    UPDATE osm_peak SET
                        admin_province_code=?, admin_province_name=?,
                        admin_prefecture_code=?, admin_prefecture_name=?,
                        admin_county_code=?, admin_county_name=?, admin_boundary_version=?
                    WHERE id=?
                    """,
                    updates,
                )
                updates.clear()
        if updates:
            connection.executemany(
                """
                UPDATE osm_peak SET
                    admin_province_code=?, admin_province_name=?,
                    admin_prefecture_code=?, admin_prefecture_name=?,
                    admin_county_code=?, admin_county_name=?, admin_boundary_version=?
                WHERE id=?
                """,
                updates,
            )
        connection.commit()
        connection.execute("VACUUM")
        return matched, total
    finally:
        connection.close()


DEFAULT_REQUIRED_CODES = {
    "110109", "110114", "430621", "430626",
    "440303", "440304", "440305", "440306", "440307",
    "440308", "440309", "440310", "440311",
}


def validate_counties(
    counties: list[BoundaryArea],
    required_codes: set[str],
    minimum_count: int,
) -> None:
    codes = {area.code for area in counties}
    missing = sorted(required_codes - codes)
    if missing:
        raise RuntimeError(f"required administrative boundaries are missing: {', '.join(missing)}")
    if len(counties) < minimum_count:
        raise RuntimeError(f"only {len(counties)} county-level boundaries were generated")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--osm-pbf", type=Path, required=True)
    parser.add_argument("--peak-db", type=Path, required=True)
    parser.add_argument("--codes", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--simplify-tolerance", type=float, default=0.0005)
    parser.add_argument("--location-index", default="sparse_file_array")
    parser.add_argument("--minimum-county-count", type=int, default=2_000)
    parser.add_argument("--required-code", action="append")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    official = load_official_areas(args.codes)
    collector = AdministrativeAreaCollector(args.simplify_tolerance)
    collector.apply_file(str(args.osm_pbf), locations=True, idx=args.location_index)
    areas = list(collector.areas.values())
    resolve_hierarchy(areas, official)
    counties = [area for area in areas if area.level == 6 and area.province_name and area.prefecture_name]
    required_codes = set(args.required_code) if args.required_code else DEFAULT_REQUIRED_CODES
    validate_counties(counties, required_codes, args.minimum_county_count)

    boundaries_path = args.output_dir / "RunLogChinaAdminBoundaries.sqlite"
    peaks_path = args.output_dir / "RunLogChinaPeaks.sqlite"
    create_boundary_database(boundaries_path, counties, args.source_version)
    matched, total = enrich_peak_database(
        args.peak_db,
        peaks_path,
        BoundaryMatcher(counties),
        args.source_version,
    )
    print(
        f"built {boundaries_path.name}: {len(counties)} counties; "
        f"enriched {matched}/{total} peaks; geometry failures={collector.geometry_failures}"
    )


if __name__ == "__main__":
    main()
