import os
from pathlib import Path
from urllib.parse import urlparse

from neo4j import GraphDatabase

import config

# All node labels / relationship types this project owns (for reset + counts).
NODE_LABELS = [
    "TransitStop",
    "MSOAZone",
    "Building",
    "StreetSegment",
    "Ward",
    "POI",
    "Agency",
    "Route",
    "Trip",
    "ServiceCalendar",
    "LandUse",
    "Water",
    "Infrastructure",
    "Shape",
    "Frequency",
    "ServiceException",
    "Junction",
    "Place",
]

REL_TYPES = [
    "CONNECTS",
    "MIGRATION",
    "ADJACENT_TO",
    "CONNECTED_TO",
    "FACES",
    "IN_ZONE",
    "CONTIGUOUS_WITH",
    "COVERS",
    "NEAR",
    "OPERATED_BY",
    "ON_ROUTE",
    "STOPS_AT",
    "RUNS_ON",
    "HAS_SHAPE",
    "HAS_FREQUENCY",
    "HAS_EXCEPTION",
    "ROAD_LINK",
    "COMMUTE",
]

# unique key per label.
KEYS = {
    "TransitStop": "stop_id",
    "MSOAZone": "MSOA21CD",
    "Building": "place_id",
    "StreetSegment": "movement_id",
    "Ward": "GSS_CODE",
    "POI": "poi_id",
    "Agency": "agency_id",
    "Route": "route_id",
    "Trip": "trip_id",
    "ServiceCalendar": "service_id",
    "LandUse": "feature_id",
    "Water": "feature_id",
    "Infrastructure": "feature_id",
    "Shape": "shape_id",
    "Frequency": "freq_id",
    "ServiceException": "exc_id",
    "Junction": "junction_id",
    "Place": "place_key",
}


def get_driver():
    if not config.NEO4J_PASSWORD:
        raise SystemExit(
            "NEO4J_PASSWORD is empty. Copy .env.example to .env and set the password."
        )
    return GraphDatabase.driver(
        config.NEO4J_URI, auth=(config.NEO4J_USERNAME, config.NEO4J_PASSWORD)
    )


# The server's import directory (None if the server doesn't expose it, e.g. Aura).
def server_import_dir(driver):
    try:
        with driver.session(database=config.NEO4J_DATABASE) as s:
            row = s.run(
                "CALL dbms.listConfig() YIELD name, value "
                "WHERE name = 'server.directories.import' RETURN value"
            ).single()
        return row["value"] if row else None
    except Exception:  # noqa: BLE001 - Aura restricts dbms.listConfig
        return None


# Pick the loading strategy from the target:
#   local  -> client shares the server filesystem; stage into the import dir and
#             use fast server-side `LOAD CSV file:///`.
#   remote -> Aura / any server we can't write files to; stage locally and load
#             with client-side chunked `UNWIND` over Bolt.
# Override with NEO4J_LOAD_MODE=local|remote.
def detect_mode(driver):
    override = os.environ.get("NEO4J_LOAD_MODE", "").strip().lower()
    if override in ("local", "remote"):
        return override
    host = urlparse(config.NEO4J_URI).hostname or ""
    if host.endswith("databases.neo4j.io"):
        return "remote"
    imp = server_import_dir(driver)
    # Path visible to this client => shared filesystem => local LOAD CSV is usable.
    if imp and Path(imp).is_dir():
        return "local"
    return "remote"


# Staging dir for CSVs: the server import dir (local) or a local dir (remote).
def import_staging(driver, mode):
    if mode == "local":
        imp = server_import_dir(driver)
        base = Path(imp)
    else:
        base = config.STAGING_DIR
    p = base / "londonmap"
    p.mkdir(parents=True, exist_ok=True)
    return p


def create_constraints(driver):
    with driver.session(database=config.NEO4J_DATABASE) as session:
        for label, key in KEYS.items():
            name = f"london_{label.lower()}_key"
            session.run(
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.`{key}` IS UNIQUE"
            )


def reset(driver):
    with driver.session(database=config.NEO4J_DATABASE) as session:
        for label in NODE_LABELS:
            while True:
                c = session.run(
                    f"MATCH (n:{label}) WITH n LIMIT 20000 DETACH DELETE n RETURN count(*) AS c"
                ).single()["c"]
                if c == 0:
                    break


# Shared scalar coercion vocabulary, used by both the loader (on CSV `row` cells)
# and the in-place migration (on existing `n` props). `ref` is the Cypher value
# expression to coerce (e.g. "row.`lat`" or "n.`lat`").
#   float          -> Float
#   int            -> Integer ("3", "200", "20211231")
#   int_from_float -> Integer from a float-formatted string ("1.0" -> 1)
#   bool_tf        -> Boolean from "True"/"False"
#   bool_num       -> Boolean from "1.0"/"0.0"
def coerce_expr(typ, ref):
    if typ == "int":
        return f"toInteger({ref})"
    if typ == "int_from_float":
        return f"toInteger(toFloat({ref}))"
    if typ == "bool_tf":
        return f"({ref} = 'True')"
    if typ == "bool_num":
        return f"(toFloat({ref}) <> 0.0)"
    return f"toFloat({ref})"


def _coerce_clause(numeric):
    parts = []
    for col, typ in (numeric or {}).items():
        parts.append(f"n.`{col}` = {coerce_expr(typ, f'row.`{col}`')}")
    return (" SET " + ", ".join(parts)) if parts else ""


# Stream a staged CSV as lists of row maps (for the remote UNWIND path), one
# chunk at a time so memory stays bounded on huge files. Empty cells are dropped
# so `SET x += row` skips them, matching LOAD CSV's null handling; the escape/quote
# settings mirror how the CSVs were written.
def _iter_row_chunks(path, batch):
    import pandas as pd

    reader = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        doublequote=False,
        escapechar="\\",
        chunksize=batch,
    )
    for chunk in reader:
        cols = list(chunk.columns)
        yield [
            {c: v for c, v in zip(cols, rec) if v != ""}
            for rec in chunk.itertuples(index=False, name=None)
        ]


# Run an inner Cypher body (operating on `row`) over staged data, in batches.
#   local  -> server-side LOAD CSV ... CALL (row) {} IN TRANSACTIONS
#   remote -> client-side UNWIND $rows, one transaction per chunk
def _run_batched(driver, body, csv_name, staging, mode, batch):
    batch = batch or config.LOAD_BATCH
    with driver.session(database=config.NEO4J_DATABASE) as s:
        if mode == "local":
            cypher = (
                f"LOAD CSV WITH HEADERS FROM 'file:///londonmap/{csv_name}' AS row "
                f"CALL (row) {{ {body} }} IN TRANSACTIONS OF {batch} ROWS"
            )
            s.run(cypher).consume()
        else:
            for rows in _iter_row_chunks(staging / csv_name, batch):
                if rows:
                    s.run(f"UNWIND $rows AS row {body}", rows=rows).consume()


def _node_body(label, numeric, point_field, merge_key):
    coerce = _coerce_clause(numeric)
    point = ""
    if point_field:
        # Every node CSV with a point also carries lon/lat; store them as floats
        # (not the raw CSV strings) alongside the native point.
        point = (
            f" FOREACH (_ IN CASE WHEN row.lon IS NULL THEN [] ELSE [1] END | "
            f"SET n.{point_field} = "
            f"point({{longitude: toFloat(row.lon), latitude: toFloat(row.lat)}}), "
            f"n.lon = toFloat(row.lon), n.lat = toFloat(row.lat))"
        )
    create = (
        f"MERGE (n:{label} {{`{merge_key}`: row.`{merge_key}`}})"
        if merge_key
        else f"CREATE (n:{label})"
    )
    return f"{create} SET n += row{coerce}{point}"


def load_node_csv(
    driver,
    csv_name,
    label,
    numeric=None,
    point_field=None,
    mode="local",
    staging=None,
    batch=None,
    merge_key=None,
):
    body = _node_body(label, numeric, point_field, merge_key)
    _run_batched(driver, body, csv_name, staging, mode, batch)


def _rel_set_props(props):
    if not props:
        return ""
    parts = []
    for col, typ in props.items():
        if typ in ("int", "float", "int_from_float", "bool_tf", "bool_num"):
            parts.append(f"r.`{col}` = {coerce_expr(typ, f'row.`{col}`')}")
        else:
            parts.append(f"r.`{col}` = row.`{col}`")
    return " SET " + ", ".join(parts)


def load_rel_csv(
    driver,
    csv_name,
    rel_type,
    src_label,
    src_key,
    tgt_label,
    tgt_key,
    props=None,
    mode="local",
    staging=None,
    batch=None,
    merge=False,
):
    set_props = _rel_set_props(props)
    rel = (
        f"MERGE (a)-[r:{rel_type}]->(b)" if merge else f"CREATE (a)-[r:{rel_type}]->(b)"
    )
    body = (
        f"MATCH (a:{src_label} {{`{src_key}`: row.source}}) "
        f"MATCH (b:{tgt_label} {{`{tgt_key}`: row.target}}) "
        f"{rel}{set_props}"
    )
    _run_batched(driver, body, csv_name, staging, mode, batch)


def counts(driver):
    out = {}
    with driver.session(database=config.NEO4J_DATABASE) as session:
        for label in NODE_LABELS:
            out[label] = session.run(
                f"MATCH (n:{label}) RETURN count(n) AS c"
            ).single()["c"]
        for rel in REL_TYPES:
            out[rel] = session.run(
                f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c"
            ).single()["c"]
    return out
