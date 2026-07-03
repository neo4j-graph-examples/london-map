import argparse

import config
import full_build as fb
import neo4j_loader as nl

# Single-file node layers: (csv, label, numeric-coercions, point_field)
NODE_LOADS = [
    (
        "zones.csv",
        "MSOAZone",
        {
            "BNG_E": "float",
            "BNG_N": "float",
            "LAT": "float",
            "LONG": "float",
            "Shape__Area": "float",
            "Shape__Length": "float",
        },
        "centroid",
    ),
    ("wards.csv", "Ward", {"HECTARES": "float", "NONLD_AREA": "float"}, "centroid"),
    (
        "transit_stops.csv",
        "TransitStop",
        {
            "stop_lat": "float",
            "stop_lon": "float",
            "location_type": "int",
            "wheelchair_boarding": "int",
        },
        "location",
    ),
    ("agency.csv", "Agency", {}, None),
    ("routes.csv", "Route", {"route_type": "int"}, None),
    (
        "trips.csv",
        "Trip",
        {"direction_id": "int", "wheelchair_accessible": "int"},
        None,
    ),
    (
        "service.csv",
        "ServiceCalendar",
        {
            "monday": "int",
            "tuesday": "int",
            "wednesday": "int",
            "thursday": "int",
            "friday": "int",
            "saturday": "int",
            "sunday": "int",
        },
        None,
    ),
    ("shapes.csv", "Shape", {}, None),
    (
        "frequencies.csv",
        "Frequency",
        {"headway_secs": "int", "exact_times": "int"},
        None,
    ),
    ("service_exceptions.csv", "ServiceException", {"exception_type": "int"}, None),
    ("places.csv", "Place", {"confidence": "float"}, "location"),
    ("pois.csv", "POI", {}, "location"),
    (
        "theme_land_use.csv",
        "LandUse",
        {"elevation": "float", "level": "int_from_float"},
        "centroid",
    ),
    (
        "theme_water.csv",
        "Water",
        {"is_intermittent": "bool_num", "is_salt": "bool_num"},
        "centroid",
    ),
    (
        "theme_infrastructure.csv",
        "Infrastructure",
        {"height": "float", "level": "int_from_float"},
        "centroid",
    ),
]

# Per-borough node layers (glob under morph/ and junctions/).
GLOB_NODE_LOADS = [
    ("junctions/junctions__*.csv", "Junction", {}, "location"),
    (
        "morph/buildings__*.csv",
        "Building",
        {
            "area": "float",
            "perimeter": "float",
            "compactness": "float",
            "height": "float",
            "num_floors": "float",
            "min_height": "float",
            "roof_height": "float",
            "roof_direction": "float",
            "min_floor": "float",
            "num_floors_underground": "float",
            "enclosure_index": "int",
            "level": "int_from_float",
            "is_underground": "bool_tf",
            "has_parts": "bool_tf",
        },
        "centroid",
    ),
    ("morph/streets__*.csv", "StreetSegment", {"length": "float"}, "centroid"),
]

# Single-file relationship layers: (csv, type, src_label, src_key, tgt_label, tgt_key, props)
REL_LOADS = [
    (
        "migration_edges.csv",
        "MIGRATION",
        "MSOAZone",
        "MSOA21CD",
        "MSOAZone",
        "MSOA21CD",
        {"weight": "int", "Count": "int"},
    ),
    (
        "ward_contiguity.csv",
        "CONTIGUOUS_WITH",
        "Ward",
        "GSS_CODE",
        "Ward",
        "GSS_CODE",
        {"weight": "float"},
    ),
    (
        "transit_edges.csv",
        "CONNECTS",
        "TransitStop",
        "stop_id",
        "TransitStop",
        "stop_id",
        {"frequency": "int", "travel_time_sec": "float"},
    ),
    (
        "stops_at.csv",
        "STOPS_AT",
        "Trip",
        "trip_id",
        "TransitStop",
        "stop_id",
        {"sequence": "int", "arrival": "str", "departure": "str"},
    ),
    ("on_route.csv", "ON_ROUTE", "Trip", "trip_id", "Route", "route_id", {}),
    ("operated_by.csv", "OPERATED_BY", "Route", "route_id", "Agency", "agency_id", {}),
    ("runs_on.csv", "RUNS_ON", "Trip", "trip_id", "ServiceCalendar", "service_id", {}),
    ("has_shape.csv", "HAS_SHAPE", "Trip", "trip_id", "Shape", "shape_id", {}),
    (
        "has_frequency.csv",
        "HAS_FREQUENCY",
        "Trip",
        "trip_id",
        "Frequency",
        "freq_id",
        {},
    ),
    (
        "has_exception.csv",
        "HAS_EXCEPTION",
        "ServiceCalendar",
        "service_id",
        "ServiceException",
        "exc_id",
        {},
    ),
    ("place_in_zone.csv", "IN_ZONE", "Place", "place_key", "MSOAZone", "MSOA21CD", {}),
    (
        "commute_edges.csv",
        "COMMUTE",
        "MSOAZone",
        "MSOA21CD",
        "MSOAZone",
        "MSOA21CD",
        {"count": "int"},
    ),
    (
        "ward_covers_stop.csv",
        "COVERS",
        "Ward",
        "GSS_CODE",
        "TransitStop",
        "stop_id",
        {},
    ),
    (
        "stop_in_zone.csv",
        "IN_ZONE",
        "TransitStop",
        "stop_id",
        "MSOAZone",
        "MSOA21CD",
        {},
    ),
    ("poi_in_zone.csv", "IN_ZONE", "POI", "poi_id", "MSOAZone", "MSOA21CD", {}),
    (
        "poi_near_stop.csv",
        "NEAR",
        "POI",
        "poi_id",
        "TransitStop",
        "stop_id",
        {"distance_m": "float"},
    ),
    (
        "theme_land_use_in_zone.csv",
        "IN_ZONE",
        "LandUse",
        "feature_id",
        "MSOAZone",
        "MSOA21CD",
        {},
    ),
    (
        "theme_water_in_zone.csv",
        "IN_ZONE",
        "Water",
        "feature_id",
        "MSOAZone",
        "MSOA21CD",
        {},
    ),
    (
        "theme_infrastructure_in_zone.csv",
        "IN_ZONE",
        "Infrastructure",
        "feature_id",
        "MSOAZone",
        "MSOA21CD",
        {},
    ),
]

# Per-borough relationship layers (glob under morph/).
GLOB_REL_LOADS = [
    (
        "morph/adjacent__*.csv",
        "ADJACENT_TO",
        "Building",
        "place_id",
        "Building",
        "place_id",
        {"weight": "float"},
    ),
    (
        "morph/connected__*.csv",
        "CONNECTED_TO",
        "StreetSegment",
        "movement_id",
        "StreetSegment",
        "movement_id",
        {},
    ),
    (
        "morph/faces__*.csv",
        "FACES",
        "Building",
        "place_id",
        "StreetSegment",
        "movement_id",
        {},
    ),
    (
        "morph/building_in_zone__*.csv",
        "IN_ZONE",
        "Building",
        "place_id",
        "MSOAZone",
        "MSOA21CD",
        {},
    ),
    (
        "morph/street_in_zone__*.csv",
        "IN_ZONE",
        "StreetSegment",
        "movement_id",
        "MSOAZone",
        "MSOA21CD",
        {},
    ),
    (
        "junctions/road_link__*.csv",
        "ROAD_LINK",
        "Junction",
        "junction_id",
        "Junction",
        "junction_id",
        {"length": "float", "segment_id": "str", "road_class": "str"},
    ),
    (
        "junctions/junction_in_zone__*.csv",
        "IN_ZONE",
        "Junction",
        "junction_id",
        "MSOAZone",
        "MSOA21CD",
        {},
    ),
]


# Rail layer (Overground + Elizabeth line) -> same labels, loaded with MERGE so it's
# an idempotent incremental add onto the existing graph. (csv, label, numeric, point, merge_key)
RAIL_NODE_LOADS = [
    (
        "rail_transit_stops.csv",
        "TransitStop",
        {"stop_lat": "float", "stop_lon": "float"},
        "location",
        "stop_id",
    ),
    ("rail_agency.csv", "Agency", {}, None, "agency_id"),
    ("rail_routes.csv", "Route", {"route_type": "int"}, None, "route_id"),
    ("rail_trips.csv", "Trip", {"direction_id": "int"}, None, "trip_id"),
    (
        "rail_service.csv",
        "ServiceCalendar",
        {
            "monday": "int",
            "tuesday": "int",
            "wednesday": "int",
            "thursday": "int",
            "friday": "int",
            "saturday": "int",
            "sunday": "int",
        },
        None,
        "service_id",
    ),
    (
        "rail_service_exceptions.csv",
        "ServiceException",
        {"exception_type": "int"},
        None,
        "exc_id",
    ),
    ("rail_shapes.csv", "Shape", {}, None, "shape_id"),
]

# Rail relationships, all MERGE (idempotent). (csv, type, src_label, src_key, tgt_label, tgt_key, props)
RAIL_REL_LOADS = [
    (
        "rail_transit_edges.csv",
        "CONNECTS",
        "TransitStop",
        "stop_id",
        "TransitStop",
        "stop_id",
        {"frequency": "int", "travel_time_sec": "float"},
    ),
    (
        "rail_stops_at.csv",
        "STOPS_AT",
        "Trip",
        "trip_id",
        "TransitStop",
        "stop_id",
        {"sequence": "int", "arrival": "str", "departure": "str"},
    ),
    ("rail_on_route.csv", "ON_ROUTE", "Trip", "trip_id", "Route", "route_id", {}),
    (
        "rail_operated_by.csv",
        "OPERATED_BY",
        "Route",
        "route_id",
        "Agency",
        "agency_id",
        {},
    ),
    (
        "rail_runs_on.csv",
        "RUNS_ON",
        "Trip",
        "trip_id",
        "ServiceCalendar",
        "service_id",
        {},
    ),
    ("rail_has_shape.csv", "HAS_SHAPE", "Trip", "trip_id", "Shape", "shape_id", {}),
    (
        "rail_has_exception.csv",
        "HAS_EXCEPTION",
        "ServiceCalendar",
        "service_id",
        "ServiceException",
        "exc_id",
        {},
    ),
    (
        "rail_stop_in_zone.csv",
        "IN_ZONE",
        "TransitStop",
        "stop_id",
        "MSOAZone",
        "MSOA21CD",
        {},
    ),
    (
        "rail_ward_covers_stop.csv",
        "COVERS",
        "Ward",
        "GSS_CODE",
        "TransitStop",
        "stop_id",
        {},
    ),
]


def build(staging, max_boroughs=None, only=None):
    def want(name):
        return only is None or name in only

    def phase(name, fn):
        if not want(name):
            return
        print(f"-- phase: {name}")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - keep overnight run alive
            import traceback

            print(f"   PHASE {name} FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()

    # zones + wards are always needed (cheap; dependencies of other phases).
    zones = fb.build_zones_full(staging)
    wards = fb.build_wards_full(staging)
    phase("migration", lambda: fb.build_migration_full(staging, zones))
    phase("transit", lambda: fb.build_transit_full(staging))
    phase("gtfs_extras", lambda: fb.build_gtfs_extras(staging))
    phase(
        "morphology",
        lambda: fb.build_morphology_full(
            staging, wards, zones, max_boroughs=max_boroughs
        ),
    )
    # junctions reads the per-borough Overture segment extracts that morphology downloads.
    phase(
        "junctions",
        lambda: fb.build_junctions(staging, wards, zones, max_boroughs=max_boroughs),
    )
    phase("commuting", lambda: fb.build_commuting(staging, zones))
    phase("place", lambda: fb.build_overture_place(staging, zones))
    phase("pois", lambda: fb.build_pois_pbf(staging, zones))
    phase("themes", lambda: fb.build_overture_themes(staging, zones))
    phase("cross", lambda: fb.build_cross_links(staging, zones, wards))
    # rail reuses transit_stops.csv + pois.csv, so it runs after transit/pois/cross.
    phase("rail", lambda: fb.build_rail(staging, wards, zones))


def load(driver, staging, mode, only=None):
    import glob

    def exists(name):
        return (staging / name).exists()

    # "rail" loads the Overground/Elizabeth layer; everything else is the BODS feed.
    load_bods = only is None or bool(only - {"rail"})
    load_rail_layer = only is None or "rail" in only

    if load_bods:
        print("  nodes (single-file)...")
        for csv, label, num, pt in NODE_LOADS:
            if exists(csv):
                nl.load_node_csv(
                    driver,
                    csv,
                    label,
                    numeric=num,
                    point_field=pt,
                    mode=mode,
                    staging=staging,
                )
                print(f"    {label} <- {csv}")
        print("  nodes (per-borough)...")
        for pattern, label, num, pt in GLOB_NODE_LOADS:
            for f in sorted(glob.glob(str(staging / pattern))):
                rel = f.split("londonmap/", 1)[-1]
                nl.load_node_csv(
                    driver,
                    rel,
                    label,
                    numeric=num,
                    point_field=pt,
                    mode=mode,
                    staging=staging,
                )
            print(f"    {label} <- {pattern}")
        print("  relationships (single-file)...")
        for csv, rt, sl, sk, tl, tk, props in REL_LOADS:
            if exists(csv):
                nl.load_rel_csv(
                    driver,
                    csv,
                    rt,
                    sl,
                    sk,
                    tl,
                    tk,
                    props=props,
                    mode=mode,
                    staging=staging,
                )
                print(f"    {rt} <- {csv}")
        print("  relationships (per-borough)...")
        for pattern, rt, sl, sk, tl, tk, props in GLOB_REL_LOADS:
            for f in sorted(glob.glob(str(staging / pattern))):
                rel = f.split("londonmap/", 1)[-1]
                nl.load_rel_csv(
                    driver,
                    rel,
                    rt,
                    sl,
                    sk,
                    tl,
                    tk,
                    props=props,
                    mode=mode,
                    staging=staging,
                )
            print(f"    {rt} <- {pattern}")

    if load_rail_layer:
        load_rail(driver, staging, mode)


# Overground + Elizabeth line: idempotent MERGE add onto the existing graph.
def load_rail(driver, staging, mode):
    def exists(name):
        return (staging / name).exists()

    print("  rail (Overground + Elizabeth line)...")
    for csv, label, num, pt, mk in RAIL_NODE_LOADS:
        if exists(csv):
            nl.load_node_csv(
                driver,
                csv,
                label,
                numeric=num,
                point_field=pt,
                mode=mode,
                staging=staging,
                merge_key=mk,
            )
            print(f"    {label} <- {csv}")
    for csv, rt, sl, sk, tl, tk, props in RAIL_REL_LOADS:
        if exists(csv):
            nl.load_rel_csv(
                driver,
                csv,
                rt,
                sl,
                sk,
                tl,
                tk,
                props=props,
                mode=mode,
                staging=staging,
                merge=True,
            )
            print(f"    {rt} <- {csv}")
    _replace_near(driver, staging, mode)


# Replace POI NEAR edges with a recompute over all stops (now incl. rail stations).
def _replace_near(driver, staging, mode):
    n = fb.build_near_all(driver, staging)
    if not n:
        print("    NEAR not recomputed (no POIs)")
        return
    with driver.session(database=config.NEO4J_DATABASE) as s:
        while True:
            c = s.run(
                "MATCH ()-[r:NEAR]->() WITH r LIMIT 50000 DELETE r RETURN count(*) AS c"
            ).single()["c"]
            if c == 0:
                break
    nl.load_rel_csv(
        driver,
        "poi_near_stop_all.csv",
        "NEAR",
        "POI",
        "poi_id",
        "TransitStop",
        "stop_id",
        props={"distance_m": "float"},
        mode=mode,
        staging=staging,
        merge=True,
    )
    print("    NEAR recomputed over all stops (replaced)")


def main():
    p = argparse.ArgumentParser(description="Build + load the full Greater-London map")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--skip-load", action="store_true")
    p.add_argument(
        "--max-boroughs",
        type=int,
        default=None,
        help="limit morphology boroughs (testing)",
    )
    p.add_argument("--only", default=None, help="comma list of build phases to run")
    args = p.parse_args()

    driver = nl.get_driver()
    mode = nl.detect_mode(driver)
    staging = nl.import_staging(driver, mode)
    print(f"== Target mode: {mode} · staging dir: {staging}")
    only = set(args.only.split(",")) if args.only else None
    try:
        if not args.skip_build:
            print("== Build (stream CSVs) ==")
            build(staging, max_boroughs=args.max_boroughs, only=only)
        if not args.skip_load:
            print("== Load into Neo4j ==")
            if args.reset:
                print("  resetting...")
                nl.reset(driver)
            nl.create_constraints(driver)
            load(driver, staging, mode, only=only)
            print("== Summary ==")
            for k, v in nl.counts(driver).items():
                print(f"   {k}: {v}")
    finally:
        driver.close()
    print("Done.")


if __name__ == "__main__":
    main()
