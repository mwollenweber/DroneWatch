#!/usr/bin/env python3
"""
AWS Lambda: poll the public NOPD drone flight layer and tweet new flights.

Flights are uploaded in batches days after they fly, and ObjectIds are not in
takeoff order, so "new" means "a flight_id not seen before", not "took off
since the last poll". Each run fetches every flight (attributes only, ~150 KB),
diffs against a DynamoDB table of flights already handled, and tweets the new
ones: up to GROUP_OVER of them one tweet each, oldest first; more than that as
one tweet summarizing the batch.

  - Empty table (first run): record every existing flight, tweet nothing.
  - More than FLOOD_THRESHOLD new at once: tweet nothing and fail the run. That
    usually means the layer was rebuilt; invoke with {"seed": true} to accept.
  - At most MAX_TWEETS_PER_RUN per run; the rest wait for the next run.
  - A flight is recorded only after its tweet succeeds, so failures retry.

Event overrides: {"dry_run": true} logs tweets without posting them;
{"seed": true} records everything as seen without tweeting.

# preview tweet text for the 5 latest flights; needs no AWS or X credentials
python app.py --preview 5

# run the monitor locally: same new-flight detection, but the "already handled"
# set lives in a JSON file and each new flight is printed as JSON, not tweeted
python app.py --watch --every 15

Copyright 2026 mjw@insomniac.tech
"""

import argparse, json, logging, math, os, re, sys, time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

from dump import BASE, LOCAL, fetch_pages

log = logging.getLogger()
log.setLevel(logging.INFO)

TWEET_URL = "https://api.x.com/2/tweets"
EPOCH = datetime(2000, 1, 1)
DESC_MAX = 80
GROUP_OVER = 3  # more new flights than this go out as one batch tweet
LAYER = BASE.removesuffix("/query")
DATA_URL = ("https://www.arcgis.com/apps/mapviewer/index.html"
            "?layers=918de1931f864bf3a0c10dd6b00499e9")
TWEET_MAX, URL_LEN = 280, 23  # X counts every link as 23 characters


class RateLimited(Exception):
    pass


class Duplicate(Exception):
    pass


def fetch_flights(session):
    """Every flight on the layer, as attribute dicts."""
    end = datetime.now(LOCAL).replace(tzinfo=None) + timedelta(days=2)
    return [f["attributes"] for pg in fetch_pages(EPOCH, end, session=session)
            for f in pg.get("features", [])]


def flight_center(object_id, session):
    """(lat, lon) at the middle of a flight's bounding box, or None.

    A flight path can be megabytes, so ask the server for the extent only.
    """
    try:
        r = session.get(BASE, params={"objectIds": object_id,
                                      "returnExtentOnly": "true", "f": "json"},
                        timeout=30)
        r.raise_for_status()
        e = r.json()["extent"]
        lat, lon = (e["ymin"] + e["ymax"]) / 2, (e["xmin"] + e["xmax"]) / 2
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning("no extent for ObjectId %s: %s", object_id, exc)
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) else None


def layer_edited(session):
    """When NOPD last changed the layer's data, as local time, or None.

    ArcGIS bumps this when a batch of flights is uploaded, so it stands in for
    "when these flights were logged".
    """
    try:
        r = session.get(LAYER, params={"f": "json"}, timeout=30)
        r.raise_for_status()
        ms = r.json()["editingInfo"]["dataLastEditDate"]
        return datetime.fromtimestamp(ms / 1000, LOCAL)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning("no layer edit date: %s", exc)
        return None


def clock(t):
    return t.strftime("%I:%M %p").lstrip("0")


def day_range(d0, d1):
    """"Oct 20–26, 2026", "Sep 28 – Oct 3, 2026", or "Oct 20, 2026"."""
    if d0 == d1:
        return f"{d0:%b} {d0.day}, {d0.year}"
    if d0.year != d1.year:
        return f"{d0:%b} {d0.day}, {d0.year} – {d1:%b} {d1.day}, {d1.year}"
    if d0.month != d1.month:
        return f"{d0:%b} {d0.day} – {d1:%b} {d1.day}, {d1.year}"
    return f"{d0:%b} {d0.day}–{d1.day}, {d1.year}"


def days_later(n0, n1):
    """Gap from flights to upload: "10–16 days later", "1 day later", "same day"."""
    if n1 <= 0:
        return "same day"
    if n0 == n1:
        return f"{n1} day{'s' if n1 != 1 else ''} later"
    return f"{max(n0, 0)}–{n1} days later"


def tweet_len(text):
    return len(re.sub(r"https?://\S+", "x" * URL_LEN, text))


def compose_group(flights, logged=None):
    """One tweet summarizing a batch of new flights.

    logged is when the batch hit the layer (layer_edited); None means now.
    """
    logged = logged or datetime.now(LOCAL)
    days = sorted(datetime.fromtimestamp(a["takeoff"] / 1000, LOCAL).date()
                  for a in flights)
    first, last = days[0], days[-1]
    gap = days_later((logged.date() - last).days, (logged.date() - first).days)

    purposes = Counter(a.get("flight_purpose") or "" for a in flights)
    mix = [f"{n} {p or 'no purpose given'}" for p, n in
           sorted(purposes.items(), key=lambda pn: (-pn[1], not pn[0]))]

    lines = [f"NOPD logged {len(flights)} drone flights at once",
             f"Flown {day_range(first, last)}",
             f"Logged {logged:%a %b} {logged.day}, {clock(logged)}, {gap}",
             None,
             f"Data: {DATA_URL}"]
    # drop the smallest purposes until the tweet fits
    while mix:
        lines[3] = ", ".join(mix)
        if tweet_len("\n".join(lines)) <= TWEET_MAX:
            break
        mix.pop()
        if mix:
            mix[-1] += ", …"
    return "\n".join(l for l in lines if l)


def compose(a, center=None):
    """Tweet text for one flight's attributes."""
    t0 = datetime.fromtimestamp(a["takeoff"] / 1000, LOCAL)
    lines = [f"NOPD drone flight: {a.get('flight_purpose') or 'purpose not given'}"]

    desc = (a.get("description") or "").strip()
    if desc:
        if len(desc) > DESC_MAX:
            desc = desc[:DESC_MAX - 1].rstrip() + "…"
        lines.append(f"“{desc}”")

    when = f"{t0:%a %b} {t0.day}, {t0:%Y}, "
    if a.get("landing"):
        t1 = datetime.fromtimestamp(a["landing"] / 1000, LOCAL)
        start, end = clock(t0), clock(t1)
        if start[-2:] == end[-2:]:
            start = start[:-3]
        mins = round((a["landing"] - a["takeoff"]) / 60000)
        when += f"{start}–{end} ({mins} min)" if mins else f"{start}–{end} (<1 min)"
    else:
        when += clock(t0)
    lines.append(when)

    if a.get("external_id"):
        lines.append(f"Item #{a['external_id']}")
    if center:
        lines.append("https://www.google.com/maps/search/?api=1&query="
                     f"{center[0]:.5f},{center[1]:.5f}")
    return "\n".join(lines)


class Store:
    """DynamoDB table of flights already handled, keyed by flight_id."""

    def __init__(self, table_name):
        import boto3
        self.table = boto3.resource("dynamodb").Table(table_name)

    def seen(self):
        ids, kw = set(), {"ProjectionExpression": "flight_id"}
        while True:
            page = self.table.scan(**kw)
            ids.update(i["flight_id"] for i in page["Items"])
            if "LastEvaluatedKey" not in page:
                return ids
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def record(self, a, status, tweet_id=None):
        self.table.put_item(Item=_item(a, status, tweet_id))

    def record_all(self, flights, status, tweet_id=None):
        with self.table.batch_writer(overwrite_by_pkeys=["flight_id"]) as b:
            for a in flights:
                b.put_item(Item=_item(a, status, tweet_id))


def _item(a, status, tweet_id=None):
    item = {"flight_id": a["flight_id"], "object_id": a["ObjectId"],
            "takeoff": a["takeoff"], "status": status,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if tweet_id:
        item["tweet_id"] = tweet_id
    return item


class LocalStore:
    """JSON file of flights already handled; the local stand-in for DynamoDB.

    Same records as the table, keyed by flight_id. Writes go through a temp
    file so a crash mid-save can't leave a half-written file behind.
    """

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.items = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.items = {}
        if not isinstance(self.items, dict):
            raise ValueError(f"{self.path} should hold a JSON object keyed by flight_id")

    def seen(self):
        return set(self.items)

    def record(self, a, status, tweet_id=None):
        self.items[a["flight_id"]] = _item(a, status, tweet_id)
        self._save()

    def record_all(self, flights, status, tweet_id=None):
        for a in flights:
            self.items[a["flight_id"]] = _item(a, status, tweet_id)
        self._save()

    def _save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.items, indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(self.path)


def flight_details(a, center=None, text=None):
    """A flight's attributes as a readable dict, for --watch output."""
    local = lambda ms: datetime.fromtimestamp(ms / 1000, LOCAL).isoformat(timespec="seconds")
    d = {"flight_id": a["flight_id"], "object_id": a["ObjectId"],
         "purpose": a.get("flight_purpose"), "description": a.get("description"),
         "external_id": a.get("external_id"),
         "takeoff": local(a["takeoff"]),
         "landing": local(a["landing"]) if a.get("landing") else None,
         "duration_min": (round((a["landing"] - a["takeoff"]) / 60000)
                          if a.get("landing") else None),
         "lat": round(center[0], 5) if center else None,
         "lon": round(center[1], 5) if center else None,
         "vehicle_serial": a.get("vehicle_serial"), "dock_serial": a.get("dock_serial"),
         "operation_id": a.get("operation_id")}
    if text:
        d["tweet"] = text
    return d


def print_post(text, flight=None, center=None, flights=None):
    """Stand-in for X.post in --watch: prints the flight as JSON, sends nothing.

    A batch tweet prints as {"tweet": ..., "flights": [...]}. Returns "stdout"
    where X.post returns a tweet id, so the record says where the flight went.
    """
    d = ({"tweet": text, "flights": [flight_details(a) for a in flights]}
         if flights else flight_details(flight, center, text))
    print(json.dumps(d, indent=2, ensure_ascii=False), flush=True)
    return "stdout"


class X:
    """Posts tweets with OAuth 1.0a user credentials from Secrets Manager."""

    def __init__(self, secret_id, session):
        self.secret_id, self.session, self._auth = secret_id, session, None

    def auth(self):
        # loaded lazily so seeding and dry runs work before the secret is filled
        if self._auth is None:
            import boto3
            c = json.loads(boto3.client("secretsmanager").get_secret_value(
                SecretId=self.secret_id)["SecretString"])
            self._auth = OAuth1(c["api_key"], c["api_secret"],
                                c["access_token"], c["access_token_secret"])
        return self._auth

    def post(self, text, flight=None, center=None, flights=None):
        # flight, center and flights are for print_post; X only needs the text
        r = self.session.post(TWEET_URL, json={"text": text}, auth=self.auth(),
                              timeout=30)
        if r.status_code == 429:
            raise RateLimited(f"resets at {r.headers.get('x-rate-limit-reset')}")
        if r.status_code == 403 and "duplicate" in r.text.lower():
            raise Duplicate(r.text[:300])
        if r.status_code >= 400:
            raise RuntimeError(f"X API {r.status_code}: {r.text[:500]}")
        return r.json()["data"]["id"]


def run(store, post, session, *, seed=False, dry_run=False,
        max_tweets=10, flood_threshold=200):
    flights = fetch_flights(session)
    seen = store.seen()
    new = sorted((a for a in flights if a["flight_id"] not in seen),
                 key=lambda a: (a["takeoff"], a["ObjectId"]))
    summary = {"flights": len(flights), "seen": len(seen), "new": len(new),
               "tweeted": 0, "failed": 0}

    if seed or not seen:
        store.record_all(new, "seeded")
        log.info("seeded %d flights without tweeting", len(new))
        return {**summary, "seeded": len(new)}

    if len(new) > flood_threshold:
        # raise, not return, so it shows in the Lambda Errors metric
        raise RuntimeError(f'{len(new)} new flights exceeds FLOOD_THRESHOLD='
                           f'{flood_threshold}; not tweeting. Invoke with '
                           '{"seed": true} to record them as seen.')

    if len(new) > GROUP_OVER:
        return post_group(store, post, session, new, summary, dry_run)

    for i, a in enumerate(new[:max_tweets]):
        center = flight_center(a["ObjectId"], session)
        text = compose(a, center)
        if dry_run:
            log.info("DRY RUN, would tweet %s:\n%s", a["flight_id"], text)
            continue
        try:
            tweet_id = post(text, flight=a, center=center)
        except RateLimited as exc:
            log.warning("rate limited (%s); %d flights left for the next run",
                        exc, len(new) - i)
            break
        except Duplicate as exc:
            # already posted, e.g. the table write failed after a previous tweet
            log.warning("X says %s is a duplicate, recording it: %s",
                        a["flight_id"], exc)
            store.record(a, "duplicate")
            continue
        except Exception:
            log.exception("tweet failed for %s; will retry next run", a["flight_id"])
            summary["failed"] += 1
            continue
        store.record(a, "tweeted", tweet_id)
        summary["tweeted"] += 1
        log.info("tweeted %s as %s", a["flight_id"], tweet_id)

    if len(new) > max_tweets:
        log.info("%d new flights over MAX_TWEETS_PER_RUN wait for the next run",
                 len(new) - max_tweets)
    log.info("summary %s", json.dumps(summary))
    if summary["failed"]:
        # surface in the Lambda Errors metric; the flights retry next run
        raise RuntimeError(f"{summary['failed']} tweet(s) failed: {summary}")
    return summary


def post_group(store, post, session, new, summary, dry_run):
    """Tweet a batch of new flights as one summary and record them all."""
    text = compose_group(new, layer_edited(session))
    if dry_run:
        log.info("DRY RUN, would tweet %d flights as one:\n%s", len(new), text)
        return summary
    try:
        tweet_id = post(text, flights=new)
    except RateLimited as exc:
        log.warning("rate limited (%s); %d flights left for the next run",
                    exc, len(new))
        return summary
    except Duplicate as exc:
        log.warning("X says the batch tweet is a duplicate, recording it: %s", exc)
        store.record_all(new, "duplicate")
        return summary
    store.record_all(new, "grouped", tweet_id)
    log.info("tweeted %d flights as %s", len(new), tweet_id)
    summary = {**summary, "tweeted": 1, "grouped": len(new)}
    log.info("summary %s", json.dumps(summary))
    return summary


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    session = requests.Session()
    return run(Store(os.environ["TABLE_NAME"]),
               X(os.environ["SECRET_ID"], session).post, session,
               seed=bool(event.get("seed")),
               dry_run=bool(event.get("dry_run"))
                       or os.environ.get("DRY_RUN", "").lower() == "true",
               max_tweets=int(os.environ.get("MAX_TWEETS_PER_RUN", 10)),
               flood_threshold=int(os.environ.get("FLOOD_THRESHOLD", 200)))


def watch(args):
    """Run the monitor locally: a JSON file for state, stdout instead of X."""
    store, session, seed = LocalStore(args.state), requests.Session(), args.seed
    log.info("watching with state in %s", store.path)
    while True:
        try:
            out = run(store, print_post, session, seed=seed,
                      max_tweets=args.max_tweets,
                      flood_threshold=args.flood_threshold)
            log.info("pass done: %s", json.dumps(out))
        except requests.RequestException as exc:
            if not args.every:
                raise
            log.warning("fetch failed, will retry next pass: %s", exc)
        if not args.every:
            return
        seed = False  # --seed applies to the first pass only
        try:
            time.sleep(args.every * 60)
        except KeyboardInterrupt:
            log.info("stopped")
            return


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", type=int, metavar="N",
                      help="print tweet text for the N latest flights and exit")
    mode.add_argument("--watch", action="store_true",
                      help="run the monitor locally: print new flights, no X or AWS")
    w = p.add_argument_group("--watch options")
    w.add_argument("--state", default="nopd-flights.json", metavar="FILE",
                   help="JSON file of flights already handled (default: %(default)s)")
    w.add_argument("--every", type=float, default=0, metavar="MINUTES",
                   help="keep polling at this interval instead of one pass")
    w.add_argument("--seed", action="store_true",
                   help="record every current flight as handled without printing")
    w.add_argument("--max-tweets", type=int, default=10, metavar="N",
                   help="most flights printed per pass, like MaxTweetsPerRun "
                        "(default: %(default)s)")
    w.add_argument("--flood-threshold", type=int, default=200, metavar="N",
                   help="print nothing if more new flights than this appear at "
                        "once, like FloodThreshold (default: %(default)s)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, datefmt="%H:%M:%S",
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.watch:
        return watch(args)
    session = requests.Session()
    latest = sorted(fetch_flights(session), key=lambda a: -a["takeoff"])
    for a in latest[:args.preview]:
        print(compose(a, flight_center(a["ObjectId"], session)), end="\n\n")


if __name__ == "__main__":
    main()
