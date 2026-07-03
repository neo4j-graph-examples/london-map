import argparse
import json
import zipfile

import requests

import config

USER_AGENT = "Mozilla/5.0 (london-map data loader)"


def ensure_dirs():
    for d in (config.RAW_DIR, config.PROCESSED_DIR, config.OVERTURE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _stream_download(url, dest, force=False):
    if dest.exists() and not force:
        print(f"  cached: {dest.name}")
        return dest
    print(f"  downloading {url}")
    with requests.get(
        url, stream=True, timeout=120, headers={"User-Agent": USER_AGENT}
    ) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.rename(dest)
    print(f"  saved: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def download_gtfs(force=False):
    print("GTFS (BODS London):")
    return _stream_download(config.GTFS_URL, config.GTFS_ZIP, force=force)


def download_rail(force=False):
    print("National Rail GTFS (source for Overground + Elizabeth line):")
    return _stream_download(config.RAIL_GTFS_URL, config.RAIL_GTFS_ZIP, force=force)


def download_msoa_boundaries(force=False):
    print("MSOA boundaries (ONS, London bbox):")
    if config.MSOA_BOUNDARY_FILE.exists() and not force:
        print(f"  cached: {config.MSOA_BOUNDARY_FILE.name}")
        return config.MSOA_BOUNDARY_FILE
    minx, miny, maxx, maxy = config.GREATER_LONDON_BBOX
    base = {
        "where": "1=1",
        "geometry": f"{minx},{miny},{maxx},{maxy}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(config.MSOA_PAGE_SIZE),
    }
    features = []
    offset = 0
    while True:
        params = dict(base, resultOffset=str(offset))
        r = requests.get(config.MSOA_SERVICE, params=params, timeout=120)
        r.raise_for_status()
        page = r.json().get("features", [])
        features.extend(page)
        print(f"  fetched {len(features)} zones...")
        if len(page) < config.MSOA_PAGE_SIZE:
            break
        offset += config.MSOA_PAGE_SIZE
    fc = {"type": "FeatureCollection", "features": features}
    config.MSOA_BOUNDARY_FILE.write_text(json.dumps(fc))
    print(f"  saved {len(features)} MSOA zones")
    return config.MSOA_BOUNDARY_FILE


def download_od_migration(force=False):
    print("OD migration (ONS Census 2021 ODMG01EW):")
    if config.OD_MIGRATION_FILE.exists() and not force:
        print(f"  cached: {config.OD_MIGRATION_FILE.name}")
        return config.OD_MIGRATION_FILE
    try:
        _stream_download(config.OD_MIGRATION_URL, config.OD_MIGRATION_ZIP, force=force)
        with zipfile.ZipFile(config.OD_MIGRATION_ZIP) as zf:
            names = zf.namelist()
            csv_name = next(
                (n for n in names if n.endswith(config.OD_MIGRATION_MEMBER)),
                next(n for n in names if n.lower().endswith(".csv")),
            )
            with zf.open(csv_name) as src, open(config.OD_MIGRATION_FILE, "wb") as dst:
                dst.write(src.read())
        print(f"  extracted: {config.OD_MIGRATION_FILE.name}")
        return config.OD_MIGRATION_FILE
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: could not fetch OD migration data ({e}).")
        print(f"  Get it manually from: {config.OD_MIGRATION_URL}")
        print(f"  then place the CSV at: {config.OD_MIGRATION_FILE}")
        return None


def download_odwp(force=False):
    print("OD workplace/commuting (ONS Census 2021 ODWP01EW):")
    if config.ODWP_FILE.exists() and not force:
        print(f"  cached: {config.ODWP_FILE.name}")
        return config.ODWP_FILE
    try:
        _stream_download(config.ODWP_URL, config.ODWP_ZIP, force=force)
        with zipfile.ZipFile(config.ODWP_ZIP) as zf:
            names = zf.namelist()
            member = next(n for n in names if n.endswith(config.ODWP_MEMBER))
            with zf.open(member) as src, open(config.ODWP_FILE, "wb") as dst:
                dst.write(src.read())
        print(f"  extracted: {config.ODWP_FILE.name}")
        return config.ODWP_FILE
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: could not fetch ODWP ({e}). Get it from: {config.ODWP_URL}")
        return None


def download_wards(force=False):
    print("London wards (London Datastore 2018):")
    if config.WARDS_SHP.exists() and not force:
        print(f"  cached: {config.WARDS_SHP.name}")
        return config.WARDS_SHP
    try:
        _stream_download(config.WARDS_URL, config.WARDS_ZIP, force=force)
        config.WARDS_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(config.WARDS_ZIP) as zf:
            members = [n for n in zf.namelist() if config.WARDS_MEMBER in n]
            for n in members:
                data = zf.read(n)
                (config.WARDS_DIR / n.rsplit("/", 1)[-1]).write_bytes(data)
        print(f"  extracted {len(members)} ward shapefile parts")
        return config.WARDS_SHP
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: could not fetch ward boundaries ({e}).")
        print(f"  Get it manually from: {config.WARDS_URL}")
        return None


def download_all(force=False):
    ensure_dirs()
    download_gtfs(force=force)
    download_rail(force=force)
    download_msoa_boundaries(force=force)
    download_od_migration(force=force)
    download_odwp(force=force)
    download_wards(force=force)


def main():
    parser = argparse.ArgumentParser(description="Download London map source data")
    parser.add_argument(
        "--force", action="store_true", help="re-download even if cached"
    )
    args = parser.parse_args()
    download_all(force=args.force)


if __name__ == "__main__":
    main()
