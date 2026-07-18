# RunLogGeoResources

Public offline geo resources for RunLog.

## Release Assets

- `RunLogChinaPeaks.sqlite.zip`
  - China OSM peak database with precomputed province, prefecture, and county assignments.
- `RunLogChinaAdminBoundaries.sqlite.zip`
  - Simplified county-level administrative polygons and an SQLite R-tree for local route matching.
- `RunLogGlobalPeaks.sqlite.zip`
  - Existing global OSM peak database. This resource is not rebuilt by the China workflow.
- `RunLogPeaks.sqlite`
  - Temporary compatibility alias for RunLog versions released before the China-specific filename.
- `geo-resources-manifest.json`
  - Asset sizes and SHA-256 values for update checks.

All OSM-derived resources are distributed under ODbL 1.0 and require OpenStreetMap attribution.
Administrative names and codes use the committed 2023 Ministry of Civil Affairs snapshot.

## Build

The `Build China geo resources` workflow downloads the latest Geofabrik China extract, keeps
administrative relations and referenced geometry, then publishes the generated SQLite files to
the `geo-resources` release. The full OSM extract is build-only and is never shipped to the app.

To refresh the official code snapshot:

```bash
python scripts/fetch_china_admin_codes.py \
  --output data/china-admin-codes-2023.csv
```

For local sample builds, install `osmium` from `requirements.txt` and pass a Geofabrik PBF plus
an existing `RunLogPeaks.sqlite` to `scripts/build_china_geo_resources.py`.
