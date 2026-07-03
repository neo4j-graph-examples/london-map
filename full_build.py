import csv

import geopandas as gpd
import pandas as pd

import city2graph as c2g

import build_layers as bl
import config

# Backslash escaping to match Neo4j's db.import.csv.legacy_quote_escaping=true.
CSV_KW = dict(
    index=False, quoting=csv.QUOTE_MINIMAL, doublequote=False, escapechar="\\"
)


def write_csv(staging, name, df):
    df.to_csv(staging / name, **CSV_KW)
    print(f"    wrote {name}: {len(df)} rows")
    return len(df)


def read_csv_bs(path):
    return pd.read_csv(
        path, dtype=str, keep_default_na=False, doublequote=False, escapechar="\\"
    )


def _gl_boundary_wgs84():
    return c2g.get_boundaries(config.GREATER_LONDON)


# ---- zones (exact Greater London) + migration ----
def build_zones_full(staging):
    zones = gpd.read_file(config.MSOA_BOUNDARY_FILE)
    gl = _gl_boundary_wgs84().to_crs(config.BNG).geometry.union_all()
    zb = zones.to_crs(config.BNG)
    inside = zb.representative_point().within(gl)
    zones = zones.loc[inside.values].copy()
    df = bl._finalize_nodes(
        zones.set_index(config.OD_ZONE_ID_COL),
        config.OD_ZONE_ID_COL,
        src_crs=config.WGS84,
    )
    write_csv(staging, "zones.csv", df)
    return zones


def build_migration_full(staging, zones):
    mig = bl.build_migration(zones)["migration_edges"]
    write_csv(staging, "migration_edges.csv", mig)


# ---- wards ----
def build_wards_full(staging):
    wards, tables = bl.build_wards()
    write_csv(staging, "wards.csv", tables["wards"])
    write_csv(staging, "ward_contiguity.csv", tables["ward_contiguity"])
    return wards


# ---- full GTFS (London-clipped) via DuckDB COPY ----
def build_transit_full(staging):
    con = c2g.load_gtfs(config.GTFS_ZIP)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}

    stops_df = con.execute(
        "SELECT stop_id, ST_X(geometry) AS lon, ST_Y(geometry) AS lat FROM stops "
        "WHERE geometry IS NOT NULL"
    ).df()
    gl = _gl_boundary_wgs84()[["geometry"]]
    g = gpd.GeoDataFrame(
        stops_df,
        geometry=gpd.points_from_xy(stops_df.lon, stops_df.lat),
        crs=config.WGS84,
    )
    london = gpd.sjoin(g, gl, how="inner", predicate="within")
    con.register("london_stops", pd.DataFrame({"stop_id": london["stop_id"].unique()}))
    print(f"    london stops: {london['stop_id'].nunique()}")

    sp = staging.as_posix()

    def copy(sql, name):
        con.execute(f"COPY ({sql}) TO '{sp}/{name}' (HEADER, FORMAT CSV, ESCAPE '\\')")
        print(f"    wrote {name}")

    # nodes
    copy(
        "SELECT s.* EXCLUDE(geometry), ST_AsText(s.geometry) AS geometry_wkt, "
        "ST_X(s.geometry) AS lon, ST_Y(s.geometry) AS lat "
        "FROM stops s SEMI JOIN london_stops l ON s.stop_id = l.stop_id",
        "transit_stops.csv",
    )
    con.execute(
        "CREATE TEMP TABLE london_trips AS SELECT DISTINCT st.trip_id "
        "FROM stop_times st SEMI JOIN london_stops l ON st.stop_id = l.stop_id"
    )
    copy(
        "SELECT t.* FROM trips t SEMI JOIN london_trips lt ON t.trip_id = lt.trip_id",
        "trips.csv",
    )
    con.execute(
        "CREATE TEMP TABLE london_routes AS SELECT DISTINCT t.route_id "
        "FROM trips t SEMI JOIN london_trips lt ON t.trip_id = lt.trip_id"
    )
    copy(
        "SELECT r.* FROM routes r SEMI JOIN london_routes lr ON r.route_id = lr.route_id",
        "routes.csv",
    )
    if "agency" in tables:
        copy("SELECT * FROM agency", "agency.csv")
    if "calendar" in tables:
        copy("SELECT * FROM calendar", "service.csv")

    # relationships
    copy(
        "SELECT st.trip_id AS source, st.stop_id AS target, "
        "st.stop_sequence AS sequence, st.arrival_time AS arrival, "
        "st.departure_time AS departure "
        "FROM stop_times st SEMI JOIN london_stops l ON st.stop_id = l.stop_id",
        "stops_at.csv",
    )
    copy(
        "SELECT t.trip_id AS source, t.route_id AS target "
        "FROM trips t SEMI JOIN london_trips lt ON t.trip_id = lt.trip_id "
        "WHERE t.route_id IS NOT NULL",
        "on_route.csv",
    )
    copy(
        "SELECT r.route_id AS source, r.agency_id AS target "
        "FROM routes r SEMI JOIN london_routes lr ON r.route_id = lr.route_id "
        "WHERE r.agency_id IS NOT NULL",
        "operated_by.csv",
    )
    copy(
        "SELECT t.trip_id AS source, t.service_id AS target "
        "FROM trips t SEMI JOIN london_trips lt ON t.trip_id = lt.trip_id "
        "WHERE t.service_id IS NOT NULL",
        "runs_on.csv",
    )

    # CONNECTS summary graph (reuse the city2graph travel summary, clipped to GL)
    start, end = bl._resolve_service_day(con)
    nodes, edges = c2g.travel_summary_graph(con, calendar_start=start, calendar_end=end)
    nodes = nodes.to_crs(config.BNG)
    edges = edges.to_crs(config.BNG)
    boundary = c2g.get_boundaries(config.GREATER_LONDON).to_crs(config.BNG)
    _, edges = c2g.clip_graph((nodes, edges), boundary, keep_outer_neighbors=False)
    conn = bl._finalize_edges(edges, "from_stop_id", "to_stop_id", src_crs=config.BNG)
    write_csv(staging, "transit_edges.csv", conn)


# ---- morphology over all Greater London, tiled by borough ----
def _borough_polys(wards):
    bor = wards.dissolve(by="LB_GSS_CD", aggfunc="first").reset_index()
    return bor.to_crs(config.BNG)


def build_morphology_full(staging, wards, zones, max_boroughs=None):
    boroughs = _borough_polys(wards)
    if max_boroughs:
        boroughs = boroughs.iloc[:max_boroughs]
    morph = staging / "morph"
    morph.mkdir(exist_ok=True)
    zones_bng = zones[[config.OD_ZONE_ID_COL, "geometry"]].to_crs(config.BNG)
    done = 0
    for _, brow in boroughs.iterrows():
        bcode = str(brow["LB_GSS_CD"])
        marker = morph / f"_done_{bcode}"
        if marker.exists():
            done += 1
            continue
        poly = brow.geometry
        name = brow.get("BOROUGH", bcode)
        print(f"  borough {done + 1}/{len(boroughs)}: {name} ({bcode})")
        try:
            _morphology_one_borough(morph, bcode, poly, zones_bng)
            marker.write_text("ok")
        except Exception as e:  # noqa: BLE001
            print(f"    SKIP {bcode}: {type(e).__name__}: {str(e)[:160]}")
        done += 1


def _morphology_one_borough(morph, bcode, poly, zones_bng):
    buf = poly.buffer(config.BOROUGH_BUFFER_M)
    area = gpd.GeoSeries([buf], crs=config.BNG).to_crs(config.WGS84)
    bbox = list(area.total_bounds)
    odir = config.OVERTURE_DIR / "boroughs"
    odir.mkdir(parents=True, exist_ok=True)
    prefix = f"{bcode}_"
    expected = [odir / f"{prefix}{t}.geojson" for t in config.OVERTURE_TYPES]
    if not all(p.exists() for p in expected):
        c2g.load_overture_data(
            area=bbox,
            types=config.OVERTURE_TYPES,
            output_dir=str(odir),
            prefix=prefix,
            save_to_file=True,
            return_data=False,
        )
    b = gpd.read_file(expected[1]).to_crs(config.BNG)
    s = gpd.read_file(expected[0]).to_crs(config.BNG)
    cn = gpd.read_file(expected[2]).to_crs(config.BNG)
    s = s[s["subtype"] == "road"].copy()
    s = c2g.process_overture_segments(s, get_barriers=True, connectors_gdf=cn)

    center = gpd.GeoSeries([poly.centroid], crs=config.BNG)
    minx, miny, maxx, maxy = buf.bounds
    distance = ((maxx - minx) ** 2 + (maxy - miny) ** 2) ** 0.5 / 2 + 100
    mn, me = c2g.morphological_graph(
        b,
        s,
        center_point=center,
        distance=distance,
        clipping_buffer=config.MORPHOLOGY_CLIP_BUFFER_M,
        primary_barrier_col="barrier_geometry",
        contiguity="queen",
        keep_buildings=True,
        keep_segments=True,
    )
    place, movement = mn["place"], mn["movement"]
    # core ownership: representative point inside this borough polygon
    core_pl = set(place.index[place.geometry.representative_point().within(poly)])
    core_mv = set(movement.index[movement.geometry.representative_point().within(poly)])
    place = place.loc[place.index.isin(core_pl)].copy()
    movement = movement.loc[movement.index.isin(core_mv)].copy()
    place.index = [f"{bcode}:{i}" for i in place.index]
    movement.index = [f"{bcode}:{i}" for i in movement.index]

    buildings = bl._finalize_nodes(place, "place_id", src_crs=config.BNG)
    cell = gpd.GeoSeries(
        list(mn["place"].loc[list(core_pl), "tessellation_geometry"].values),
        crs=config.BNG,
    )
    buildings["area"] = cell.area.values
    buildings["perimeter"] = cell.length.values
    buildings["compactness"] = [
        (4 * 3.141592653589793 * a / (p * p)) if p and p > 0 else None
        for a, p in zip(cell.area.values, cell.length.values)
    ]
    buildings = buildings.drop_duplicates("place_id").reset_index(drop=True)
    streets = (
        bl._finalize_nodes(movement, "movement_id", src_crs=config.BNG)
        .drop_duplicates("movement_id")
        .reset_index(drop=True)
    )

    def core_edges(key, sl, tl, core_s, core_t):
        e = me[key].reset_index()
        e = e[e[sl].isin(core_s) & e[tl].isin(core_t)].copy()
        return e

    adj = core_edges(
        ("place", "touched_to", "place"),
        "from_place_id",
        "to_place_id",
        core_pl,
        core_pl,
    )
    adjacent = pd.DataFrame(
        {
            "source": [f"{bcode}:{x}" for x in adj["from_place_id"]],
            "target": [f"{bcode}:{x}" for x in adj["to_place_id"]],
            "weight": adj["weight"].astype(float).values,
        }
    )
    conn = core_edges(
        ("movement", "connected_to", "movement"),
        "from_movement_id",
        "to_movement_id",
        core_mv,
        core_mv,
    )
    connected = pd.DataFrame(
        {
            "source": [f"{bcode}:{x}" for x in conn["from_movement_id"]],
            "target": [f"{bcode}:{x}" for x in conn["to_movement_id"]],
        }
    )
    fac = core_edges(
        ("place", "faced_to", "movement"), "place_id", "movement_id", core_pl, core_mv
    )
    faces = pd.DataFrame(
        {
            "source": [f"{bcode}:{x}" for x in fac["place_id"]],
            "target": [f"{bcode}:{x}" for x in fac["movement_id"]],
        }
    )

    b_iz = bl.build_in_zone_gdf(buildings, zones_bng, id_col="place_id")
    s_iz = bl.build_in_zone_gdf(streets, zones_bng, id_col="movement_id")

    write_csv(morph, f"buildings__{bcode}.csv", buildings)
    write_csv(morph, f"streets__{bcode}.csv", streets)
    write_csv(morph, f"adjacent__{bcode}.csv", adjacent)
    write_csv(morph, f"connected__{bcode}.csv", connected)
    write_csv(morph, f"faces__{bcode}.csv", faces)
    write_csv(morph, f"building_in_zone__{bcode}.csv", b_iz)
    write_csv(morph, f"street_in_zone__{bcode}.csv", s_iz)


# ---- POIs + links ----
# Fetched per borough AND per tag-key with a hard SIGALRM timeout, so no single
# Overpass call can stall the run (public Overpass rate-limits large queries).
def build_pois_full(staging, zones, wards, tags=None, out_prefix=""):
    import signal

    import osmnx as ox

    tags = tags or config.POI_FULL_TAGS
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(config.RAW_DIR / "osmnx_cache")
    ox.settings.requests_timeout = 120
    # Don't wait for an Overpass slot between requests (the SIGALRM bound below
    # caps each call); the rate-limit pre-sleep is the dominant slowdown.
    ox.settings.overpass_rate_limit = False
    boroughs = _borough_polys(wards).to_crs(config.WGS84)

    class _Timeout(Exception):
        pass

    def _handler(signum, frame):
        raise _Timeout()

    prev = signal.signal(signal.SIGALRM, _handler)
    frames = []
    try:
        # One combined query per borough (all tags at once) = far fewer Overpass
        # round-trips; each call is SIGALRM-bounded so it can never stall.
        for _, br in boroughs.iterrows():
            name = br.get("BOROUGH", br["LB_GSS_CD"])
            signal.alarm(config.POI_FETCH_TIMEOUT_S)
            try:
                g = ox.features_from_polygon(br.geometry, tags=tags)
                if len(g):
                    frames.append(g)
                print(f"    POIs {name}: {len(g)}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"    POIs {name} SKIP: {type(e).__name__}", flush=True)
            finally:
                signal.alarm(0)
    finally:
        signal.signal(signal.SIGALRM, prev)

    poi = gpd.GeoDataFrame(pd.concat(frames, ignore_index=False))
    poi = poi.reset_index()
    elem = "element" if "element" in poi.columns else "element_type"
    oid = "id" if "id" in poi.columns else "osmid"
    poi["poi_id"] = poi[elem].astype(str) + "/" + poi[oid].astype(str)
    poi = poi.drop_duplicates("poi_id").set_index("poi_id")
    nodes = bl._finalize_nodes(poi, "poi_id", src_crs=config.WGS84)
    write_csv(staging, f"{out_prefix}pois.csv", nodes)
    zones_bng = zones[[config.OD_ZONE_ID_COL, "geometry"]].to_crs(config.BNG)
    write_csv(
        staging,
        f"{out_prefix}poi_in_zone.csv",
        bl.build_in_zone_gdf(nodes, zones_bng, id_col="poi_id"),
    )
    return nodes


def build_poi_near(staging, in_prefix=""):
    nodes = read_csv_bs(staging / f"{in_prefix}pois.csv")
    stops = read_csv_bs(staging / "transit_stops.csv")
    write_csv(staging, f"{in_prefix}poi_near_stop.csv", bl.build_near(nodes, stops))


# Fast, robust OSM POI builder: parse a local Geofabrik Greater London extract with
# pyrosm (no Overpass throttling/hangs). This is the canonical POI path.
def build_pois_pbf(staging, zones, tags=None, out_prefix=""):
    import pyrosm

    tags = tags or config.POI_FULL_TAGS
    pdir = config.RAW_DIR / "pyrosm"
    pdir.mkdir(parents=True, exist_ok=True)
    fp = pyrosm.get_data(config.GEOFABRIK_AREA, directory=str(pdir))
    osm = pyrosm.OSM(fp)
    pois = osm.get_pois(custom_filter=tags)
    pois = pois[~pois.geometry.isna()].copy()
    if pois.crs is None:
        pois = pois.set_crs(config.WGS84)
    pois["poi_id"] = pois["osm_type"].astype(str) + "/" + pois["id"].astype(str)
    pois = pois.drop_duplicates("poi_id").set_index("poi_id")
    nodes = bl._finalize_nodes(pois, "poi_id", src_crs=config.WGS84)
    print(f"  pois (pbf): {len(nodes)}")
    write_csv(staging, f"{out_prefix}pois.csv", nodes)
    zones_bng = zones[[config.OD_ZONE_ID_COL, "geometry"]].to_crs(config.BNG)
    write_csv(
        staging,
        f"{out_prefix}poi_in_zone.csv",
        bl.build_in_zone_gdf(nodes, zones_bng, id_col="poi_id"),
    )
    return nodes


# ---- cross-layer links that need other layers' CSVs ----
def build_cross_links(staging, zones, wards):
    stops = read_csv_bs(staging / "transit_stops.csv")
    zones_bng = zones[[config.OD_ZONE_ID_COL, "geometry"]].to_crs(config.BNG)
    write_csv(
        staging,
        "stop_in_zone.csv",
        bl.build_in_zone_gdf(stops, zones_bng, id_col="stop_id"),
    )
    write_csv(staging, "ward_covers_stop.csv", bl.build_covers(wards, stops))
    if (staging / "pois.csv").exists():
        pois = read_csv_bs(staging / "pois.csv")
        write_csv(staging, "poi_near_stop.csv", bl.build_near(pois, stops))


# ---- extra Overture themes (Greater London) ----
def build_overture_themes(staging, zones):
    gl = _gl_boundary_wgs84()
    bbox = list(gl.total_bounds)
    zones_bng = zones[[config.OD_ZONE_ID_COL, "geometry"]].to_crs(config.BNG)
    for theme in config.OVERTURE_EXTRA_THEMES:
        out = config.OVERTURE_DIR / f"gl_{theme}.geojson"
        if not out.exists():
            try:
                c2g.load_overture_data(
                    area=bbox,
                    types=[theme],
                    output_dir=str(config.OVERTURE_DIR),
                    prefix="gl_",
                    save_to_file=True,
                    return_data=False,
                )
            except Exception as e:  # noqa: BLE001
                print(f"    SKIP theme {theme}: {str(e)[:120]}")
                continue
        if not out.exists():
            continue
        g = gpd.read_file(out).to_crs(config.BNG)
        g = g[g.representative_point().within(zones_bng.geometry.union_all())].copy()
        g = g.reset_index(drop=True)
        g.index = [f"{theme}/{i}" for i in range(len(g))]
        df = bl._finalize_nodes(g, "feature_id", src_crs=config.BNG)
        write_csv(staging, f"theme_{theme}.csv", df)
        iz = bl.build_in_zone_gdf(df, zones_bng, id_col="feature_id")
        write_csv(staging, f"theme_{theme}_in_zone.csv", iz)


# ---- GTFS extras: shapes, frequencies, calendar_dates ----
def _gtfs_london(con):
    stops_df = con.execute(
        "SELECT stop_id, ST_X(geometry) AS lon, ST_Y(geometry) AS lat FROM stops "
        "WHERE geometry IS NOT NULL"
    ).df()
    gl = _gl_boundary_wgs84()[["geometry"]]
    g = gpd.GeoDataFrame(
        stops_df,
        geometry=gpd.points_from_xy(stops_df.lon, stops_df.lat),
        crs=config.WGS84,
    )
    london = gpd.sjoin(g, gl, how="inner", predicate="within")
    con.register("london_stops", pd.DataFrame({"stop_id": london["stop_id"].unique()}))
    con.execute(
        "CREATE TEMP TABLE london_trips AS SELECT DISTINCT st.trip_id "
        "FROM stop_times st SEMI JOIN london_stops l ON st.stop_id = l.stop_id"
    )


def build_gtfs_extras(staging):
    con = c2g.load_gtfs(config.GTFS_ZIP)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    _gtfs_london(con)
    sp = staging.as_posix()

    def copy(sql, name):
        con.execute(f"COPY ({sql}) TO '{sp}/{name}' (HEADER, FORMAT CSV, ESCAPE '\\')")
        print(f"    wrote {name}")

    if "shapes" in tables:
        con.execute(
            "CREATE TEMP TABLE london_shapes AS SELECT DISTINCT t.shape_id "
            "FROM trips t SEMI JOIN london_trips lt ON t.trip_id = lt.trip_id "
            "WHERE t.shape_id IS NOT NULL AND t.shape_id <> ''"
        )
        copy(
            "SELECT s.shape_id, ST_AsText(ST_MakeLine(list(ST_Point("
            "CAST(s.shape_pt_lon AS DOUBLE), CAST(s.shape_pt_lat AS DOUBLE)) "
            "ORDER BY CAST(s.shape_pt_sequence AS INTEGER)))) AS geometry_wkt "
            "FROM shapes s SEMI JOIN london_shapes ls ON s.shape_id = ls.shape_id "
            "GROUP BY s.shape_id",
            "shapes.csv",
        )
        copy(
            "SELECT t.trip_id AS source, t.shape_id AS target FROM trips t "
            "SEMI JOIN london_trips lt ON t.trip_id = lt.trip_id "
            "WHERE t.shape_id IS NOT NULL AND t.shape_id <> ''",
            "has_shape.csv",
        )
    if "frequencies" in tables:
        copy(
            "SELECT (f.trip_id || ':' || f.start_time) AS freq_id, f.trip_id, "
            "f.start_time, f.end_time, f.headway_secs, f.exact_times "
            "FROM frequencies f SEMI JOIN london_trips lt ON f.trip_id = lt.trip_id",
            "frequencies.csv",
        )
        copy(
            "SELECT f.trip_id AS source, (f.trip_id || ':' || f.start_time) AS target "
            "FROM frequencies f SEMI JOIN london_trips lt ON f.trip_id = lt.trip_id",
            "has_frequency.csv",
        )
    if "calendar_dates" in tables:
        copy(
            "SELECT DISTINCT (service_id || ':' || date) AS exc_id, service_id, date, "
            "exception_type FROM calendar_dates",
            "service_exceptions.csv",
        )
        copy(
            "SELECT DISTINCT service_id AS source, (service_id || ':' || date) AS target "
            "FROM calendar_dates",
            "has_exception.csv",
        )


# ---- street junctions (routable network) via borough-tiled segments_to_graph ----
def build_junctions(staging, wards, zones, max_boroughs=None):
    boroughs = _borough_polys(wards)
    if max_boroughs:
        boroughs = boroughs.iloc[:max_boroughs]
    jdir = staging / "junctions"
    jdir.mkdir(exist_ok=True)
    zones_bng = zones[[config.OD_ZONE_ID_COL, "geometry"]].to_crs(config.BNG)
    for i, (_, brow) in enumerate(boroughs.iterrows()):
        bcode = str(brow["LB_GSS_CD"])
        marker = jdir / f"_done_{bcode}"
        if marker.exists():
            continue
        print(f"  junctions {i + 1}/{len(boroughs)}: {brow.get('BOROUGH', bcode)}")
        try:
            _junctions_one_borough(jdir, bcode, brow.geometry, zones_bng)
            marker.write_text("ok")
        except Exception as e:  # noqa: BLE001
            print(f"    SKIP {bcode}: {type(e).__name__}: {str(e)[:140]}")


def _junctions_one_borough(jdir, bcode, poly, zones_bng):
    seg = config.OVERTURE_DIR / "boroughs" / f"{bcode}_segment.geojson"
    s = gpd.read_file(seg).to_crs(config.BNG)
    s = s[s["subtype"] == "road"].copy()
    n, e = c2g.segments_to_graph(s)
    core = set(n.index[n.geometry.within(poly)])
    n = n.loc[n.index.isin(core)].copy()
    n.index = [f"{bcode}:{i}" for i in n.index]
    junctions = _finalize_junction_nodes(n)
    junctions = junctions.drop_duplicates("junction_id").reset_index(drop=True)

    e = e.reset_index()
    e = e[e["from_node_id"].isin(core) & e["to_node_id"].isin(core)].copy()
    glen = gpd.GeoSeries(list(e["geometry"].values), crs=config.BNG).length.values
    links = pd.DataFrame(
        {
            "source": [f"{bcode}:{x}" for x in e["from_node_id"]],
            "target": [f"{bcode}:{x}" for x in e["to_node_id"]],
            "segment_id": e["id"].map(bl._cell).values if "id" in e.columns else None,
            "road_class": e["class"].map(bl._cell).values
            if "class" in e.columns
            else None,
            "length": glen,
        }
    )
    iz = bl.build_in_zone_gdf(junctions, zones_bng, id_col="junction_id")
    write_csv(jdir, f"junctions__{bcode}.csv", junctions)
    write_csv(jdir, f"road_link__{bcode}.csv", links)
    write_csv(jdir, f"junction_in_zone__{bcode}.csv", iz)


def _finalize_junction_nodes(n):
    lon, lat = _lonlat_simple(n.geometry)
    return pd.DataFrame(
        {
            "junction_id": list(n.index),
            "geometry_wkt": bl._geom_wkt(n.geometry, config.BNG),
            "lon": lon,
            "lat": lat,
        }
    )


def _lonlat_simple(geom_bng):
    g = gpd.GeoSeries(list(geom_bng.values), crs=config.BNG).to_crs(config.WGS84)
    return g.x.values, g.y.values


# ---- commuting (ODWP) ----
def build_commuting(staging, zones):
    if not config.ODWP_FILE.exists():
        print("  commuting: ODWP CSV missing, skipping")
        return
    codes = set(zones[config.OD_ZONE_ID_COL].astype(str))
    agg = {}
    usecols = [config.ODWP_RES_COL, config.ODWP_WORK_COL, config.ODWP_COUNT_COL]
    reader = pd.read_csv(
        config.ODWP_FILE, usecols=usecols, dtype=str, chunksize=1_000_000
    )
    for ch in reader:
        ch = ch[
            ch[config.ODWP_RES_COL].isin(codes) & ch[config.ODWP_WORK_COL].isin(codes)
        ]
        for r, w, c in zip(
            ch[config.ODWP_RES_COL], ch[config.ODWP_WORK_COL], ch[config.ODWP_COUNT_COL]
        ):
            k = (r, w)
            agg[k] = agg.get(k, 0) + int(c)
    df = pd.DataFrame(
        [(r, w, c) for (r, w), c in agg.items()], columns=["source", "target", "count"]
    )
    write_csv(staging, "commute_edges.csv", df)


# ---- Overture place POIs ----
def build_overture_place(staging, zones):
    out = config.OVERTURE_DIR / "gl_place.geojson"
    if not out.exists():
        gl = _gl_boundary_wgs84()
        c2g.load_overture_data(
            area=list(gl.total_bounds),
            types=["place"],
            output_dir=str(config.OVERTURE_DIR),
            prefix="gl_",
            save_to_file=True,
            return_data=False,
        )
    g = gpd.read_file(out).to_crs(config.BNG)
    zones_bng = zones[[config.OD_ZONE_ID_COL, "geometry"]].to_crs(config.BNG)
    g = g[g.representative_point().within(zones_bng.geometry.union_all())].reset_index(
        drop=True
    )
    g.index = [f"ovplace/{i}" for i in range(len(g))]
    nodes = _finalize_nodes_compat(g, "place_key")
    write_csv(staging, "places.csv", nodes)
    write_csv(
        staging,
        "place_in_zone.csv",
        bl.build_in_zone_gdf(nodes, zones_bng, id_col="place_key"),
    )


def _finalize_nodes_compat(g, key):
    return bl._finalize_nodes(g, key, src_crs=config.BNG)


# ---- London Overground + Elizabeth line (National Rail GTFS) ----
# Filters the GB National Rail feed in-place to the two TfL-branded operators and
# merges them onto the existing transit model. Native ids kept; whole lines kept
# (no Greater-London clip). Mirrors build_transit_full / build_gtfs_extras.
def build_rail(staging, wards, zones):
    if not config.RAIL_GTFS_ZIP.exists():
        import download_data

        download_data.download_rail()
    con = c2g.load_gtfs(config.RAIL_GTFS_ZIP)
    have = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    agl = "(" + ",".join("'" + a + "'" for a in config.RAIL_AGENCIES) + ")"

    # In-place filter to Overground + Elizabeth (all route_types, incl. replacement buses).
    con.execute(f"DELETE FROM routes WHERE agency_id NOT IN {agl}")
    con.execute("DELETE FROM trips WHERE route_id NOT IN (SELECT route_id FROM routes)")
    con.execute(
        "DELETE FROM stop_times WHERE trip_id NOT IN (SELECT trip_id FROM trips)"
    )
    con.execute(
        "DELETE FROM stops WHERE stop_id NOT IN (SELECT DISTINCT stop_id FROM stop_times)"
    )
    if "agency" in have:
        con.execute(f"DELETE FROM agency WHERE agency_id NOT IN {agl}")
    if "calendar" in have:
        con.execute(
            "DELETE FROM calendar WHERE service_id NOT IN (SELECT DISTINCT service_id FROM trips)"
        )
    if "calendar_dates" in have:
        con.execute(
            "DELETE FROM calendar_dates WHERE service_id NOT IN (SELECT DISTINCT service_id FROM trips)"
        )
    if "shapes" in have:
        con.execute(
            "DELETE FROM shapes WHERE shape_id NOT IN "
            "(SELECT DISTINCT shape_id FROM trips WHERE shape_id IS NOT NULL AND shape_id <> '')"
        )
    ntr = con.execute("SELECT count(*) FROM trips").fetchone()[0]
    nst = con.execute("SELECT count(*) FROM stops").fetchone()[0]
    print(f"  rail (Overground+Elizabeth): {ntr} trips, {nst} stations")

    sp = staging.as_posix()

    def copy(sql, name):
        con.execute(f"COPY ({sql}) TO '{sp}/{name}' (HEADER, FORMAT CSV, ESCAPE '\\')")
        print(f"    wrote {name}")

    # nodes
    copy(
        "SELECT s.* EXCLUDE(geometry), ST_AsText(s.geometry) AS geometry_wkt, "
        "ST_X(s.geometry) AS lon, ST_Y(s.geometry) AS lat FROM stops s",
        "rail_transit_stops.csv",
    )
    copy("SELECT * FROM trips", "rail_trips.csv")
    copy("SELECT * FROM routes", "rail_routes.csv")
    if "agency" in have:
        copy("SELECT * FROM agency", "rail_agency.csv")
    if "calendar" in have:
        copy("SELECT * FROM calendar", "rail_service.csv")

    # relationships
    copy(
        "SELECT st.trip_id AS source, st.stop_id AS target, "
        "st.stop_sequence AS sequence, st.arrival_time AS arrival, "
        "st.departure_time AS departure FROM stop_times st",
        "rail_stops_at.csv",
    )
    copy(
        "SELECT t.trip_id AS source, t.route_id AS target FROM trips t "
        "WHERE t.route_id IS NOT NULL",
        "rail_on_route.csv",
    )
    copy(
        "SELECT r.route_id AS source, r.agency_id AS target FROM routes r "
        "WHERE r.agency_id IS NOT NULL",
        "rail_operated_by.csv",
    )
    copy(
        "SELECT t.trip_id AS source, t.service_id AS target FROM trips t "
        "WHERE t.service_id IS NOT NULL",
        "rail_runs_on.csv",
    )
    if "shapes" in have:
        copy(
            "SELECT s.shape_id, ST_AsText(ST_MakeLine(list(ST_Point("
            "CAST(s.shape_pt_lon AS DOUBLE), CAST(s.shape_pt_lat AS DOUBLE)) "
            "ORDER BY CAST(s.shape_pt_sequence AS INTEGER)))) AS geometry_wkt "
            "FROM shapes s GROUP BY s.shape_id",
            "rail_shapes.csv",
        )
        copy(
            "SELECT t.trip_id AS source, t.shape_id AS target FROM trips t "
            "WHERE t.shape_id IS NOT NULL AND t.shape_id <> ''",
            "rail_has_shape.csv",
        )
    if "calendar_dates" in have:
        copy(
            "SELECT DISTINCT (service_id || ':' || date) AS exc_id, service_id, date, "
            "exception_type FROM calendar_dates",
            "rail_service_exceptions.csv",
        )
        copy(
            "SELECT DISTINCT service_id AS source, (service_id || ':' || date) AS target "
            "FROM calendar_dates",
            "rail_has_exception.csv",
        )

    # CONNECTS summary (same travel_summary_graph as the bus/tube layer, no clip).
    start, end = bl._resolve_service_day(con)
    print(f"  rail service window: {start}..{end}")
    _, edges = c2g.travel_summary_graph(con, calendar_start=start, calendar_end=end)
    conn = bl._finalize_edges(
        edges, "from_stop_id", "to_stop_id", src_crs=(edges.crs or config.WGS84)
    )
    write_csv(staging, "rail_transit_edges.csv", conn)

    # cross-links: rail stations -> zone / ward (out-of-GL stations simply won't join).
    rail_stops = read_csv_bs(staging / "rail_transit_stops.csv")
    zones_bng = zones[[config.OD_ZONE_ID_COL, "geometry"]].to_crs(config.BNG)
    write_csv(
        staging,
        "rail_stop_in_zone.csv",
        bl.build_in_zone_gdf(rail_stops, zones_bng, id_col="stop_id"),
    )
    write_csv(staging, "rail_ward_covers_stop.csv", bl.build_covers(wards, rail_stops))
    # NEAR is recomputed at load time from the DB's POI/stop coordinates (so it covers
    # all POIs and includes the new rail stations) - see build_near_all.


# Recompute POI->nearest-stop over ALL stops (bus+tube+rail) using the DB's own
# lon/lat (authoritative; the staged pois.csv can be stale). Writes a CSV the loader
# then uses to replace the NEAR edges.
def build_near_all(driver, staging):
    import config as cfg

    def read(label, key):
        with driver.session(database=cfg.NEO4J_DATABASE) as s:
            return s.run(
                f"MATCH (n:{label}) WHERE n.lon IS NOT NULL AND n.lat IS NOT NULL "
                f"RETURN n.`{key}` AS id, n.lon AS lon, n.lat AS lat"
            ).values()

    pois = read("POI", "poi_id")
    stops = read("TransitStop", "stop_id")
    print(f"  NEAR recompute: {len(pois)} POIs x {len(stops)} stops")
    pg = gpd.GeoDataFrame(
        {"poi_id": [r[0] for r in pois]},
        geometry=gpd.points_from_xy([r[1] for r in pois], [r[2] for r in pois]),
        crs=config.WGS84,
    ).to_crs(config.BNG)
    sg = gpd.GeoDataFrame(
        {"stop_id": [r[0] for r in stops]},
        geometry=gpd.points_from_xy([r[1] for r in stops], [r[2] for r in stops]),
        crs=config.WGS84,
    ).to_crs(config.BNG)
    joined = gpd.sjoin_nearest(pg, sg, how="inner", distance_col="distance_m")
    out = pd.DataFrame(
        {
            "source": joined["poi_id"].astype(str),
            "target": joined["stop_id"].astype(str),
            "distance_m": joined["distance_m"].round(1).values,
        }
    ).drop_duplicates("source")
    write_csv(staging, "poi_near_stop_all.csv", out)
    return len(out)
