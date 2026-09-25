#!/usr/bin/env python3
"""
Dump raw NOPD drone flight JSON from the public Skydio/ArcGIS feature layer.

python dump.py 2026-01-01 2026-12-31 -o flights.json

# with flight paths, as GeoJSON — opens in QGIS
python dump.py 2026-01-01 2026-12-31 --geojson -g -o flights.geojson

# one flight per line, for jq
python dump.py 2026-09-01 2026-09-26 --ndjson | jq -r '.attributes.flight_purpose'

Copyright 2026 mjw@insomniac.tech
"""

import argparse, json, sys, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

BASE = ("https://services7.arcgis.com/mnhQTdIYDA7UoY2l/arcgis/rest/services/"
        "bbe82a3b-5931-40c7-b848-f921b8da0373-production/FeatureServer/0/query")
LOCAL = ZoneInfo("America/Chicago")
PAGE = 1000
DELAY = 0.3


def exceeded(page):
    """True if the server truncated this page.

    f=json puts the flag at the top level; f=geojson nests it under "properties".
    """
    return bool(page.get("exceededTransferLimit")
                or (page.get("properties") or {}).get("exceededTransferLimit"))


def fetch_pages(start_local, end_local, fmt="json", geometry=False, session=None):
    """Yield each raw decoded server response, one per page."""
    lo = start_local.replace(tzinfo=LOCAL).astimezone(timezone.utc)
    hi = end_local.replace(tzinfo=LOCAL).astimezone(timezone.utc)
    where = (f"takeoff >= TIMESTAMP '{lo:%Y-%m-%d %H:%M:%S}' "
             f"AND takeoff < TIMESTAMP '{hi:%Y-%m-%d %H:%M:%S}'")

    s = session or requests.Session()
    offset = 0
    while True:
        r = s.get(BASE, params={
            "where": where,
            "outFields": "*",
            "returnGeometry": str(geometry).lower(),
            "orderByFields": "takeoff ASC,ObjectId ASC",
            "f": fmt,
            "resultOffset": offset,
            "resultRecordCount": PAGE,
        }, timeout=60)
        r.raise_for_status()
        page = r.json()
        if isinstance(page, dict) and "error" in page:
            raise RuntimeError(page["error"])

        yield page

        feats = page.get("features", [])
        if not feats or not exceeded(page):
            break
        offset += len(feats)
        time.sleep(DELAY)


def parse_when(s):
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad datetime: {s!r}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("start", type=parse_when, help="local start, e.g. 2026-01-01")
    p.add_argument("end", type=parse_when, help="local end (exclusive), e.g. 2026-10-01")
    p.add_argument("-o", "--out", help="output file (default: stdout)")
    p.add_argument("-g", "--geometry", action="store_true", help="include flight paths")
    p.add_argument("--geojson", action="store_true", help="request f=geojson")
    p.add_argument("--pages", action="store_true", help="emit raw pages verbatim")
    p.add_argument("--ndjson", action="store_true", help="one raw feature per line")
    p.add_argument("--indent", type=int, default=2, help="indent; 0 for compact")
    args = p.parse_args()

    fmt = "geojson" if args.geojson else "json"
    pages = list(fetch_pages(args.start, args.end, fmt, args.geometry))
    indent = args.indent or None

    fh = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        if args.ndjson:
            for page in pages:
                for feat in page.get("features", []):
                    fh.write(json.dumps(feat, ensure_ascii=False) + "\n")
        elif args.pages:
            json.dump(pages, fh, indent=indent, ensure_ascii=False)
            fh.write("\n")
        else:
            merged = dict(pages[0]) if pages else {}
            merged["features"] = [f for pg in pages for f in pg.get("features", [])]
            merged.pop("exceededTransferLimit", None)
            if isinstance(merged.get("properties"), dict):
                props = dict(merged["properties"])
                props.pop("exceededTransferLimit", None)
                if props:
                    merged["properties"] = props
                else:
                    merged.pop("properties")
            json.dump(merged, fh, indent=indent, ensure_ascii=False)
            fh.write("\n")
    finally:
        if args.out:
            fh.close()

    n = sum(len(pg.get("features", [])) for pg in pages)
    print(f"{n} features across {len(pages)} page(s)", file=sys.stderr)


if __name__ == "__main__":
    main()