import argparse, json

import pytest

import app

T0 = 1789568160000  # Wed Sep 16, 2026, 9:16 AM CDT


def flight(n, **kw):
    a = {"ObjectId": n, "flight_id": f"f{n}", "takeoff": T0 + n * 60000,
         "landing": T0 + n * 60000 + 20 * 60000, "external_id": "I-16149-26",
         "description": "Swat Roll", "flight_purpose": "Tactical Operations"}
    return {**a, **kw}


class FakeStore:
    def __init__(self, seen=()):
        self.ids, self.records = set(seen), []

    def seen(self):
        return set(self.ids)

    def record(self, a, status, tweet_id=None):
        self.ids.add(a["flight_id"])
        self.records.append((a["flight_id"], status, tweet_id))

    def record_all(self, flights, status):
        for a in flights:
            self.record(a, status)


class FakeX:
    def __init__(self, fail=()):
        self.posts, self.fail = [], dict(fail)

    def __call__(self, text, flight=None, center=None):
        n = len(self.posts)
        if n in self.fail:
            self.posts.append(None)
            raise self.fail[n]
        self.posts.append(text)
        return f"t{n}"


@pytest.fixture
def flights(monkeypatch):
    data = []
    monkeypatch.setattr(app, "fetch_flights", lambda s: list(data))
    monkeypatch.setattr(app, "flight_center", lambda oid, s: (29.95578, -90.01415))
    return data


def test_first_run_seeds_without_tweeting(flights):
    flights += [flight(1), flight(2)]
    store, x = FakeStore(), FakeX()
    out = app.run(store, x, None)
    assert out["seeded"] == 2 and x.posts == []
    assert {r[1] for r in store.records} == {"seeded"}


def test_new_flights_tweeted_oldest_first(flights):
    # ObjectId order differs from takeoff order, as on the real layer
    flights += [flight(1), flight(9, takeoff=T0 + 5 * 60000), flight(3)]
    store, x = FakeStore({"f1"}), FakeX()
    out = app.run(store, x, None)
    assert out["tweeted"] == 2
    assert [r[0] for r in store.records] == ["f3", "f9"]
    assert store.records[0] == ("f3", "tweeted", "t0")


def test_cap_leaves_rest_for_next_run(flights):
    flights += [flight(n) for n in range(1, 6)]
    store = FakeStore({"f1"})
    assert app.run(store, FakeX(), None, max_tweets=2)["tweeted"] == 2
    assert app.run(store, FakeX(), None, max_tweets=2)["tweeted"] == 2
    assert store.ids == {f"f{n}" for n in range(1, 6)}


def test_flood_guard_tweets_nothing(flights):
    flights += [flight(n) for n in range(1, 10)]
    store, x = FakeStore({"f1"}), FakeX()
    out = app.run(store, x, None, flood_threshold=5)
    assert out["flood"] and x.posts == [] and store.records == []
    app.run(store, x, None, seed=True, flood_threshold=5)
    assert x.posts == [] and len(store.ids) == 9


def test_rate_limit_stops_and_does_not_record(flights):
    flights += [flight(n) for n in range(1, 5)]
    store = FakeStore({"f1"})
    x = FakeX(fail={1: app.RateLimited("resets at 0")})
    out = app.run(store, x, None)
    assert out["tweeted"] == 1 and len(x.posts) == 2
    assert store.ids == {"f1", "f2"}


def test_failure_continues_then_raises(flights):
    flights += [flight(n) for n in range(1, 5)]
    store = FakeStore({"f1"})
    x = FakeX(fail={0: RuntimeError("X API 500")})
    with pytest.raises(RuntimeError, match="1 tweet"):
        app.run(store, x, None)
    assert store.ids == {"f1", "f3", "f4"}  # f2 retries next run


def test_duplicate_is_recorded(flights):
    flights += [flight(1), flight(2)]
    store = FakeStore({"f1"})
    app.run(store, FakeX(fail={0: app.Duplicate("duplicate content")}), None)
    assert store.records == [("f2", "duplicate", None)]


def test_dry_run_posts_and_records_nothing(flights):
    flights += [flight(1), flight(2)]
    store, x = FakeStore({"f1"}), FakeX()
    app.run(store, x, None, dry_run=True)
    assert x.posts == [] and store.records == []


def test_compose():
    assert app.compose(flight(0), (29.955781, -90.014152)) == (
        "NOPD drone flight: Tactical Operations\n"
        "“Swat Roll”\n"
        "Wed Sep 16, 2026, 9:16–9:36 AM (20 min)\n"
        "Item #I-16149-26\n"
        "https://www.google.com/maps/search/?api=1&query=29.95578,-90.01415")


def test_compose_crosses_midnight_and_sparse_fields():
    a = flight(0, takeoff=T0 + 14 * 3600000 + 35 * 60000, external_id=None,
               description="x" * 200, flight_purpose=None)
    a["landing"] = a["takeoff"] + 23 * 60000
    lines = app.compose(a).split("\n")
    assert lines[0] == "NOPD drone flight: purpose not given"
    assert len(lines[1]) == app.DESC_MAX + 2 and lines[1].endswith("…”")
    assert lines[2] == "Wed Sep 16, 2026, 11:51 PM–12:14 AM (23 min)"
    assert len(lines) == 3


def test_local_store_persists_like_the_table(tmp_path):
    path = tmp_path / "flights.json"
    store = app.LocalStore(path)
    assert store.seen() == set()
    store.record_all([flight(1), flight(2)], "seeded")
    store.record(flight(3), "tweeted", "t0")

    again = app.LocalStore(path)
    assert again.seen() == {"f1", "f2", "f3"}
    assert again.items["f3"]["tweet_id"] == "t0"
    assert again.items["f1"]["status"] == "seeded" and "tweet_id" not in again.items["f1"]
    assert not list(tmp_path.glob("*.tmp"))


def test_local_store_rejects_wrong_shape(tmp_path):
    path = tmp_path / "flights.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="keyed by flight_id"):
        app.LocalStore(path)


def test_watch_prints_new_flights_as_json_and_records_them(flights, tmp_path, capsys):
    path = tmp_path / "flights.json"
    flights += [flight(1), flight(2)]
    app.run(app.LocalStore(path), app.print_post, None)  # first pass seeds
    assert capsys.readouterr().out == ""

    flights.append(flight(3))
    out = app.run(app.LocalStore(path), app.print_post, None)
    assert out["tweeted"] == 1
    printed = capsys.readouterr().out
    assert printed.startswith("{\n  ")  # pretty, one flight per object
    d = json.loads(printed)
    assert d["flight_id"] == "f3" and d["object_id"] == 3
    assert d["purpose"] == "Tactical Operations" and d["description"] == "Swat Roll"
    assert d["takeoff"] == "2026-09-16T09:19:00-05:00"
    assert d["landing"] == "2026-09-16T09:39:00-05:00" and d["duration_min"] == 20
    assert (d["lat"], d["lon"]) == (29.95578, -90.01415)
    assert d["tweet"] == app.compose(flight(3), (29.95578, -90.01415))
    assert app.LocalStore(path).seen() == {"f1", "f2", "f3"}


def test_flight_details_sparse():
    d = app.flight_details(flight(0, landing=None, description=None, external_id=None))
    assert d["landing"] is None and d["duration_min"] is None
    assert d["lat"] is None and "tweet" not in d


def test_watch_once_then_stops(flights, tmp_path, monkeypatch):
    flights += [flight(1)]
    calls = []
    monkeypatch.setattr(app.time, "sleep", lambda s: calls.append(s))
    args = argparse.Namespace(state=tmp_path / "s.json", every=0, seed=False,
                              max_tweets=10, flood_threshold=50)
    app.watch(args)
    assert calls == [] and app.LocalStore(args.state).seen() == {"f1"}
