# London Map (city2graph → Neo4j)

A self-contained loader that builds **one rich, spatially-coherent, Greater-London-wide map** in
Neo4j by combining several [city2graph](https://city2graph.net) pipelines plus OSM/Overture/ONS
sources. It is completely separate from the repo's existing `import_data.py` / `dataset/london.json`
and touches no other code. The single orchestrator is **`load_full.py`**.

## What it builds

| Layer | Source | Nodes (key) | Relationships |
|-------|--------|-------------|---------------|
| Transit | TfL/BODS GTFS (bus, tube, DLR, tram, river, cable car) → `travel_summary_graph` | `:TransitStop (stop_id)` | `(:TransitStop)-[:CONNECTS]->(:TransitStop)` |
| Rail | GB National Rail GTFS, filtered to **London Overground + Elizabeth line** | (same labels: `:TransitStop`, `:Route`, `:Trip`, …) | (same: `STOPS_AT`, `ON_ROUTE`, `CONNECTS`, …) |
| Full timetable | GTFS (London-clipped) | `:Agency`, `:Route`, `:Trip`, `:ServiceCalendar`, `:Shape`, `:Frequency`, `:ServiceException` | `STOPS_AT`, `ON_ROUTE`, `OPERATED_BY`, `RUNS_ON`, `HAS_SHAPE`, `HAS_FREQUENCY`, `HAS_EXCEPTION` |
| Zones + migration | ONS MSOA boundaries + Census 2021 OD migration/commuting → `od_matrix_to_graph` | `:MSOAZone (MSOA21CD)` | `(:MSOAZone)-[:MIGRATION]->(:MSOAZone)`, `(:MSOAZone)-[:COMMUTE {count}]->(:MSOAZone)` |
| Urban form | Overture Maps → `morphological_graph` | `:Building (place_id)`, `:StreetSegment (movement_id)` | `:Building-[:ADJACENT_TO]->:Building`, `:StreetSegment-[:CONNECTED_TO]->:StreetSegment`, `:Building-[:FACES]->:StreetSegment` |
| Street network | Overture connectors → `segments_to_graph` | `:Junction (junction_id)` | `(:Junction)-[:ROAD_LINK]->(:Junction)` |
| Wards | London Datastore 2018 wards → `contiguity_graph` | `:Ward (GSS_CODE)` | `(:Ward)-[:CONTIGUOUS_WITH]->(:Ward)` (queen adjacency, stored once per pair), `(:Ward)-[:COVERS]->(:TransitStop)` |
| POIs | OSM (Geofabrik `.osm.pbf` via `pyrosm`) + Overture `place` | `:POI (poi_id)`, `:Place (place_key)` | `(:POI)-[:NEAR {distance_m}]->(:TransitStop)` (nearest stop), `IN_ZONE` |
| Extra themes | Overture | `:LandUse`, `:Water`, `:Infrastructure` (`feature_id`) | `IN_ZONE` |

The layers are unified by **MSOA zones**: every stop, building, street segment, junction and POI is
spatially joined into its containing zone via `(...)-[:IN_ZONE]->(:MSOAZone)`. Wards add a second
(administrative) geography over the same stops via `COVERS`.

> POI note: `:POI` (OSM) and `:Place` (Overture) are kept as **separate labels** (distinct sources/schemas,
> not de-duplicated against each other). Query all POIs with `MATCH (n) WHERE n:POI OR n:Place`.

> Rail note: London Overground + Elizabeth line are **National Rail** services (absent from the BODS
> feed), so they come from a separate GB National Rail GTFS and merge onto the **same** transit model.
> Their stations use NaPTAN `9100…` ids (disjoint from bus `490…`/tube `940…`); all ids are kept native
> (verified collision-free). Find them via the agencies `Elizabeth line` and the six Overground
> line-brands (`Liberty`, `Lioness`, `Mildmay`, `Suffragette`, `Weaver`, `Windrush`). Unlike the rest
> of the map, the **whole lines are kept** (stations beyond Greater London — Reading, Shenfield,
> Watford Jn, Cheshunt … — are included with coordinates + rail connections, but have no `IN_ZONE`/
> `COVERS` since there are no MSOA zones/wards outside London).

**Complete data, nothing dropped.** Every column produced by city2graph is carried onto the
nodes/edges verbatim. Conversions only: each geometry column → a `<name>_wkt` string (WGS84;
e.g. `geometry_wkt`, `building_geometry_wkt`, `tessellation_geometry_wkt`, `segment_geometry_wkt`,
`barrier_geometry_wkt`), and nested structures (`names`, `sources`, `connectors`, …) →
JSON strings (Neo4j can't store nested maps natively). Node keys are the native c2g identifiers.
Each node additionally gets a Neo4j `point` (`location`/`centroid`, WGS84) plus `lon`/`lat`;
`:Building` also gets derived `area`/`perimeter`/`compactness` (the morphology example's features).
Edges keep their full attributes too (e.g. `CONNECTS` → `travel_time_sec`, `frequency`;
`MIGRATION` → `Count`, `weight`).

**Property types.** Scalar properties are stored with their proper Neo4j types, not as strings:
coordinates (`lon`/`lat`, `stop_lat`/`stop_lon`) and metric/morphometric values are `Float`;
GTFS enum/flag codes (`route_type`, `location_type`, `wheelchair_boarding`, `direction_id`,
`exact_times`, `exception_type`, `ServiceCalendar.monday…sunday`), `Building.enclosure_index`,
and Overture `level` are `Integer`; Overture flags (`Building.is_underground`/`has_parts`,
`Water.is_intermittent`/`is_salt`) are `Boolean`. The coercion vocabulary lives in
`neo4j_loader.coerce_expr`; per-label specs are the `numeric` dicts in `load_full.py`. GTFS
clock fields (`arrival`/`departure`/`start_time`/`end_time`) stay strings because GTFS allows
values past `24:00:00`; service dates (`YYYYMMDD`) stay strings; WKT geometry stays a string
(OGC standard, self-describing, lossless — Neo4j has no native polygon/line type). Free-text OSM
`:POI` tags remain strings, as in OSM.

### Extent
- **Greater London only.** Transit, full timetable, MSOA zones (~1000, clipped to the GLA boundary),
  migration/commuting, wards, POIs and Overture themes all cover Greater London.
- **All-GL buildings & streets with full morphology**, computed by tiling Greater London into its
  **33 boroughs** (dissolved from the ward shapefile). Each borough is processed independently with
  a `BOROUGH_BUFFER_M` overlap for tessellation context; a building/street is *owned* by the borough
  containing its centroid (ids prefixed `LB_GSS_CD:…`), and `ADJACENT_TO`/`FACES`/`CONNECTED_TO`
  edges are kept between same-borough core features. (Trade-off: a small number of adjacency edges
  that cross a borough boundary are dropped — the only approximation vs. a single global tessellation,
  which is infeasible at ~3–4 M buildings.) Resumable per borough via `_done_<code>` markers.

## Data sources (cached under `data/`)
- GTFS: `https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/london/` (BODS, public — TfL multimodal: bus/tube/DLR/tram/river/cable car)
- National Rail GTFS (Overground + Elizabeth line): `https://storage.travelwhiz.app/generated-gtfs/gb-nationalrail.gtfs.zip` (NaPTAN-geocoded, CC BY 4.0)
- Overture Maps: via `overturemaps` (per-borough bboxes for morphology/junctions; Greater-London bbox for `place`/themes)
- MSOA 2021 boundaries: ONS Open Geography Portal (London bbox, ~1200 zones, then clipped to GLA)
- Migration: ONS Census 2021 `odmg01ew.zip` → `ODMG01EW_MSOA.csv` (nomis)
- Commuting: ONS Census 2021 `odwp01ew.zip` → `ODWP01EW_MSOA.csv` (nomis)
- Wards: London Datastore `statistical-gis-boundaries-london.zip` → `London_Ward_CityMerged.shp` (625 wards)
- POIs: OSM via a local Geofabrik `Greater London` `.osm.pbf` (`pyrosm`), all common POI categories
  (amenity, shop, leisure, tourism, office, healthcare, man_made, public_transport, railway, sport, natural, …)

Run `uv run python download_data.py` to fetch + cache the GTFS / MSOA / migration / commuting / ward
sources up front; Overture and the OSM `.pbf` are fetched on demand during the build and cached.

## Setup
```bash
cd london_map
uv sync                         # isolated env, python 3.11+ (does not touch mcp_server)
cp .env.local.example .env.local && $EDITOR .env.local   # set NEO4J_* for your target DB
```

## Run
```bash
# Full overnight build + load. caffeinate keeps macOS awake for the whole job.
caffeinate -i -s uv run python load_full.py --reset 2>&1 | tee data/full_run.log

# Resume after interruption (cached downloads + per-borough markers are reused):
caffeinate -i -s uv run python load_full.py --reset

# Useful flags:
#   --skip-build        load already-staged CSVs only
#   --skip-load         build/stage CSVs only
#   --max-boroughs N    limit morphology/junction boroughs (testing)
#   --only PHASES       comma list of build phases to run

# Add just the Overground + Elizabeth line layer onto an existing DB (no reset,
# idempotent MERGE; also refreshes POI NEAR to consider rail stations):
uv run python load_full.py --only rail
```
Tip: for much faster local loading, raise Neo4j Desktop's heap/page-cache (you have the RAM) before running.

## Target: local Neo4j or AuraDB
The same scripts load into **either a local Neo4j or a remote AuraDB**, chosen automatically from
`NEO4J_URI` (override with `NEO4J_LOAD_MODE=local|remote`):
- **local** (the client and server share a filesystem, e.g. Neo4j Desktop) — CSVs are staged into the
  server's import directory and loaded with fast **server-side `LOAD CSV … CALL{} IN TRANSACTIONS`**.
- **remote / Aura** (no filesystem access, `*.databases.neo4j.io`) — CSVs are staged locally under
  `data/staging/` and loaded with **client-side chunked `UNWIND`** over Bolt (slower, but needs no
  import directory or `file://` access). Same data, same types either way.

Set credentials in `.env.local` (`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`).

**Scale & loading.** Millions of nodes / tens of millions of relationships. Loading uses periodic
commit (heap-safe, no APOC / `neo4j-admin` needed). Each build phase is fault-isolated, so one
failing layer won't abort the run.

## Files
- `config.py` — connection, extent, source URLs, full-build tuning
- `download_data.py` — fetch + cache raw data (GTFS, National Rail GTFS, ONS MSOA/migration/commuting, wards)
- `build_layers.py` — shared c2g helpers (node/edge finalize → WKT/JSON, migration, wards, spatial joins)
- `full_build.py` — all-GL CSV builders (full GTFS + extras, borough-tiled morphology & junctions,
  pyrosm POIs, Overture place/themes, commuting, `build_rail` for Overground/Elizabeth + `build_near_all`)
- `neo4j_loader.py` — constraints, target detection, scalar type coercion (`coerce_expr`), and the
  dual-mode loaders (server-side `LOAD CSV` + client-side `UNWIND`)
- `load_full.py` — **the orchestrator** (`--reset` rebuilds the complete graph; flags `--skip-build`,
  `--skip-load`, `--max-boroughs N`, `--only PHASES`)

POIs use a local OSM extract (Geofabrik `Greater London` `.osm.pbf` via `pyrosm`) — minutes, not the
hours/rate-limits of public Overpass. (`build_pois_full` keeps an Overpass per-borough path as a fallback.)
