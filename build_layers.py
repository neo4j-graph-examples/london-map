import datetime as dt
import json
import math

import geopandas as gpd
import numpy as np
import pandas as pd

import city2graph as c2g

import config


# Convert one attribute cell to a Neo4j-storable scalar. Nested dict/list
# structures are JSON-serialized so nothing is dropped.
def _cell(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, np.ndarray):
        v = v.tolist()
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:  # noqa: BLE001
            return v
    return v


def _is_geom(series):
    return str(series.dtype) == "geometry"


def _geom_wkt(series, crs):
    g = gpd.GeoSeries(list(series.values), crs=crs).to_crs(config.WGS84)
    return g.to_wkt().values


def _lonlat(active, crs):
    g = gpd.GeoSeries(list(active.values), crs=crs).to_crs(config.WGS84)
    reps = g.representative_point()
    return reps.x.values, reps.y.values


# Carry every column from a node GeoDataFrame: geometry columns -> WKT (WGS84),
# nested values -> JSON, primitives verbatim. The index becomes key_name.
def _finalize_nodes(gdf, key_name, src_crs=None):
    crs = gdf.crs or src_crs
    out = {}
    for col in gdf.columns:
        s = gdf[col]
        if _is_geom(s):
            out[f"{col}_wkt"] = _geom_wkt(s, crs)
        else:
            out[col] = [_cell(v) for v in s.values]
    lon, lat = _lonlat(gdf.geometry, crs)
    out["lon"] = lon
    out["lat"] = lat
    out[key_name] = gdf.index.astype(str).values
    return pd.DataFrame(out)


# Carry every column from an edge GeoDataFrame whose index levels are the endpoints.
def _finalize_edges(gdf, src_level, tgt_level, src_crs=None):
    crs = gdf.crs or src_crs
    e = gdf.reset_index()
    out = {
        "source": e[src_level].astype(str).values,
        "target": e[tgt_level].astype(str).values,
    }
    for col in e.columns:
        if col in (src_level, tgt_level):
            continue
        s = e[col]
        if _is_geom(s):
            out[f"{col}_wkt"] = _geom_wkt(s, crs)
        else:
            out[col] = [_cell(v) for v in s.values]
    return pd.DataFrame(out)


# ----- transit (GTFS) -----
def _resolve_service_day(con):
    if config.GTFS_CALENDAR_START:
        return (
            config.GTFS_CALENDAR_START,
            config.GTFS_CALENDAR_END or config.GTFS_CALENDAR_START,
        )
    row = con.execute("SELECT min(start_date), max(end_date) FROM calendar").fetchone()
    start = dt.datetime.strptime(row[0], "%Y%m%d").date()
    end = dt.datetime.strptime(row[1], "%Y%m%d").date()
    d = start
    while d.weekday() != 2 and d < end:  # first Wednesday in range
        d += dt.timedelta(days=1)
    day = d.strftime("%Y%m%d")
    return day, day


# ----- migration (ONS) -----
def build_migration(zones, od_min_count=1):
    if not config.OD_MIGRATION_FILE.exists():
        print("  migration: OD CSV missing, skipping migration edges")
        return {"migration_edges": pd.DataFrame(columns=["source", "target"])}
    codes = set(zones[config.OD_ZONE_ID_COL].astype(str))
    od = pd.read_csv(config.OD_MIGRATION_FILE)
    od = od[od[config.OD_SOURCE_COL].isin(codes) & od[config.OD_TARGET_COL].isin(codes)]
    print(f"  migration: {len(od)} London OD rows")
    _, oe = c2g.od_matrix_to_graph(
        od,
        zones,
        zone_id_col=config.OD_ZONE_ID_COL,
        matrix_type="edgelist",
        source_col=config.OD_SOURCE_COL,
        target_col=config.OD_TARGET_COL,
        weight_cols=[config.OD_WEIGHT_COL],
        threshold=None,
        include_self_loops=False,
        compute_edge_geometry=True,
        directed=False,
        as_nx=False,
    )
    mig = _finalize_edges(oe, "source", "target", src_crs=config.WGS84)
    if od_min_count > 1 and config.OD_WEIGHT_COL in mig.columns:
        mig = mig[mig[config.OD_WEIGHT_COL] >= od_min_count]
    print(f"  migration edges: {len(mig)}")
    return {"migration_edges": mig}


# ----- cross-layer spatial glue -----
# Takes a pre-projected BNG zones GDF (id_col + geometry), so it can be reused
# across many tiles without reprojecting zones each time.
def build_in_zone_gdf(feature_df, zones_bng, id_col="place_id"):
    if feature_df is None or len(feature_df) == 0:
        return pd.DataFrame(columns=["source", "target"])
    feats = gpd.GeoDataFrame(
        feature_df[[id_col]].rename(columns={id_col: "_fid"}).copy(),
        geometry=gpd.GeoSeries.from_wkt(feature_df["geometry_wkt"], crs=config.WGS84),
    ).to_crs(config.BNG)
    feats["geometry"] = feats.geometry.representative_point()
    joined = gpd.sjoin(feats, zones_bng, how="inner", predicate="within")
    return pd.DataFrame(
        {
            "source": joined["_fid"].astype(str),
            "target": joined[config.OD_ZONE_ID_COL].astype(str),
        }
    )


# Reconstruct a metric (BNG) point GeoDataFrame from a finalized table's WKT.
def _points_bng(df, id_col):
    g = gpd.GeoDataFrame(
        df[[id_col]].copy(),
        geometry=gpd.GeoSeries.from_wkt(df["geometry_wkt"], crs=config.WGS84),
    ).to_crs(config.BNG)
    g["geometry"] = g.geometry.representative_point()
    return g


# Ward COVERS stop: each transit stop falling within a ward polygon.
def build_covers(wards, stops_df):
    if stops_df.empty:
        return pd.DataFrame(columns=["source", "target"])
    wb = wards[[config.WARD_ID_COL, "geometry"]].to_crs(config.BNG)
    pts = _points_bng(stops_df, "stop_id")
    joined = gpd.sjoin(pts, wb, how="inner", predicate="within")
    return pd.DataFrame(
        {
            "source": joined[config.WARD_ID_COL].astype(str),
            "target": joined["stop_id"].astype(str),
        }
    )


# POI NEAR stop: each POI's nearest transit stop, with distance in metres.
def build_near(pois_df, stops_df):
    if pois_df.empty or stops_df.empty:
        return pd.DataFrame(columns=["source", "target"])
    p = _points_bng(pois_df, "poi_id")
    s = _points_bng(stops_df, "stop_id")
    joined = gpd.sjoin_nearest(p, s, how="inner", distance_col="distance_m")
    return pd.DataFrame(
        {
            "source": joined["poi_id"].astype(str),
            "target": joined["stop_id"].astype(str),
            "distance_m": joined["distance_m"].round(1).values,
        }
    )


# ----- wards (London Datastore) -----
def build_wards():
    wards = gpd.read_file(config.WARDS_SHP)
    wi = wards.set_index(config.WARD_ID_COL)
    nodes = _finalize_nodes(wi, config.WARD_ID_COL, src_crs=config.BNG)
    _, ce = c2g.contiguity_graph(wi, contiguity="queen", as_nx=False)
    edges = _finalize_edges(ce, "level_0", "level_1", src_crs=config.BNG)
    print(f"  wards: {len(nodes)} wards, {len(edges)} contiguity edges")
    return wards, {"wards": nodes, "ward_contiguity": edges}
