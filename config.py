import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
OVERTURE_DIR = RAW_DIR / "overture"

load_dotenv(BASE_DIR / ".env.local")

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")

WGS84 = "EPSG:4326"
BNG = "EPSG:27700"  # British National Grid, metric

GREATER_LONDON = "Greater London, UK"

# Per-borough morphology tessellation clipping buffer (full build).
MORPHOLOGY_CLIP_BUFFER_M = 300

# Optional explicit GTFS service window; empty -> auto-detect from the feed calendar.
GTFS_CALENDAR_START = os.environ.get("GTFS_CALENDAR_START", "")
GTFS_CALENDAR_END = os.environ.get("GTFS_CALENDAR_END", "")

OVERTURE_TYPES = ["segment", "building", "connector"]

# ---- full (all-Greater-London) mode ----
LOAD_BATCH = 10000  # rows per LOAD CSV transaction
STAGING_DIR = DATA_DIR / "staging"  # local CSV staging fallback
BOROUGH_BUFFER_M = 500  # overlap buffer for per-borough morphology tessellation
# Extra Overture themes loaded as their own node labels in full mode.
OVERTURE_EXTRA_THEMES = ["land_use", "water", "infrastructure"]
# Broad POI tags for full mode (all common OSM POI categories). `natural` is a
# value list (excludes `tree` to avoid millions of point features).
POI_FULL_TAGS = {
    "amenity": True,
    "shop": True,
    "leisure": True,
    "tourism": True,
    "office": True,
    "healthcare": True,
    "historic": True,
    "emergency": True,
    "craft": True,
    "man_made": True,
    "public_transport": True,
    "aeroway": True,
    "railway": True,
    "government": True,
    "military": True,
    "club": True,
    "sport": True,
    "natural": [
        "water",
        "wood",
        "scrub",
        "heath",
        "grassland",
        "beach",
        "peak",
        "cliff",
        "spring",
        "wetland",
        "bay",
    ],
}
POI_FETCH_TIMEOUT_S = 300  # hard per-borough Overpass timeout (combined query)
GEOFABRIK_AREA = "Greater London"  # pyrosm dataset for the local OSM POI extract

# ONS Census 2021 origin-destination workplace (commuting) bulk (verified).
ODWP_URL = "https://www.nomisweb.co.uk/output/census/2021/odwp01ew.zip"
ODWP_ZIP = RAW_DIR / "odwp01ew.zip"
ODWP_MEMBER = "ODWP01EW_MSOA.csv"
ODWP_FILE = RAW_DIR / "ODWP01EW_MSOA.csv"
ODWP_RES_COL = "Middle layer Super Output Areas code"
ODWP_WORK_COL = "MSOA of workplace code"
ODWP_COUNT_COL = "Count"

# Data sources.
GTFS_URL = "https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/london/"
GTFS_ZIP = RAW_DIR / "itm_london_gtfs.zip"

# London Overground + Elizabeth line are National Rail services (absent from the
# BODS feed above). This GB National Rail GTFS (NaPTAN-geocoded, CC BY 4.0) supplies
# them; we filter to the two TfL-branded operators and merge them onto the same
# :TransitStop/:Route/:Trip model. Native ids are kept (verified collision-free with
# the BODS feed); stations use NaPTAN `9100...` codes, disjoint from bus `490`/tube `940`.
# Whole lines are kept (incl. stations beyond Greater London, e.g. Reading/Shenfield/Watford).
RAIL_GTFS_URL = "https://storage.travelwhiz.app/generated-gtfs/gb-nationalrail.gtfs.zip"
RAIL_GTFS_ZIP = RAW_DIR / "gb-nationalrail.gtfs.zip"
# Elizabeth line = XR; London Overground = its 6 current line-brands (post-2024).
RAIL_AGENCIES = [
    "XR",
    "LO-Liberty",
    "LO-Lioness",
    "LO-Mildmay",
    "LO-Suffragette",
    "LO-Weaver",
    "LO-Windrush",
]

# ONS Open Geography Portal: MSOA (Dec 2021) generalised-clipped boundaries (verified).
MSOA_SERVICE = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Middle_layer_Super_Output_Areas_December_2021_Boundaries_EW_BGC_V3/FeatureServer/0/query"
)
# Greater London bounding box (lon/lat) used to fetch only London MSOAs (~1200).
GREATER_LONDON_BBOX = (-0.5103, 51.2868, 0.3340, 51.6919)
MSOA_PAGE_SIZE = 2000
MSOA_BOUNDARY_FILE = RAW_DIR / "msoa_london.geojson"

# ONS Census 2021 origin-destination migration bulk (verified). The zip holds per-geography
# CSVs; we use the MSOA-level member.
OD_MIGRATION_URL = "https://www.nomisweb.co.uk/output/census/2021/odmg01ew.zip"
OD_MIGRATION_ZIP = RAW_DIR / "odmg01ew.zip"
OD_MIGRATION_MEMBER = "ODMG01EW_MSOA.csv"
OD_MIGRATION_FILE = RAW_DIR / "ODMG01EW_MSOA.csv"
OD_SOURCE_COL = "Migrant MSOA one year ago code"
OD_TARGET_COL = "Middle layer Super Output Areas code"
OD_WEIGHT_COL = "Count"
OD_ZONE_ID_COL = "MSOA21CD"

# London Datastore: 2018 statistical-GIS boundary files (contains the ward shapefiles).
WARDS_URL = (
    "https://data.london.gov.uk/download/statistical-gis-boundary-files-london/"
    "9ba8c833-6370-4b11-abdc-314aa020d5e0/statistical-gis-boundaries-london.zip"
)
WARDS_ZIP = RAW_DIR / "statistical-gis-boundaries-london.zip"
WARDS_DIR = RAW_DIR / "wards"
WARDS_MEMBER = "ESRI/London_Ward_CityMerged"  # shapefile stem inside the zip
WARDS_SHP = WARDS_DIR / "London_Ward_CityMerged.shp"
WARD_ID_COL = "GSS_CODE"
