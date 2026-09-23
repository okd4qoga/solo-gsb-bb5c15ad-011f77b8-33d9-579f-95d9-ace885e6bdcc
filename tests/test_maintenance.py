"""Tests for scheduled-maintenance subtraction semantics.

Engine-level tests cover window merging, duration reset and evidence
boundaries; the integration test drives services + SQLite like the HTTP API.
"""

from app.engine import (
    Condition,
    Event,
    Interval,
    MaintenanceWindow,
    evaluate_rule,
    evaluate_rule_with_maintenance,
    subtract_intervals,
    union,
)


def ev(eid, ts, value, device="d", metric="m"):
    return Event(id=eid, device=device, metric=metric, ts=ts, value=value)


def minute_series(eid_base, predicate_fn, device="d", metric="m"):
    return [
        ev(eid_base + m, m * 60, predicate_fn(m), device=device, metric=metric)
        for m in range(31)
    ]


def ivs(faults):
    return [(f.start, f.end) for f in faults]


def ivs_supp(suppressed):
    return [(s.start, s.end) for s in suppressed]


TEMP_COND = Condition("pump-1", "temp", ">=", 80)


def high_temp_series():
    # >=80 continuously on [6,17); an isolated spike at 20
    return minute_series(100, lambda m: 90 if (6 <= m <= 16 or m == 20) else 10,
                         device="pump-1", metric="temp")


HORIZON = 30 * 60


# --------------------------------------------------------------------------- #
# interval subtraction
# --------------------------------------------------------------------------- #


def test_subtract_inner_window_splits_into_two_pieces():
    pieces, removed = subtract_intervals([Interval(0, 20)], [Interval(8, 12)])
    assert pieces == [Interval(0, 8), Interval(12, 20)]
    assert removed == [Interval(8, 12)]


def test_subtract_touching_boundary_does_not_split():
    # a window ending exactly at the interval start (or starting at its end)
    # covers nothing: half-open touch is not suppression
    pieces, removed = subtract_intervals([Interval(10, 20)], [Interval(0, 10), Interval(20, 30)])
    assert pieces == [Interval(10, 20)]
    assert removed == []


def test_subtract_multiple_windows_and_merges_removed_pieces():
    pieces, removed = subtract_intervals(
        [Interval(0, 30)], [Interval(5, 8), Interval(8, 10)]
    )
    assert pieces == [Interval(0, 5), Interval(10, 30)]
    # the two touching removed pieces are one merged suppressed fragment
    assert removed == [Interval(5, 10)]


def test_maintenance_window_validation():
    for kwargs in (
        dict(device="d", start=10, end=10, name="x"),  # zero length
        dict(device="  ", start=0, end=1, name="x"),   # blank device
    ):
        try:
            MaintenanceWindow(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kwargs}")


def test_same_device_overlapping_windows_merge_first():
    merged = union([Interval(5, 9), Interval(8, 12)])
    assert merged == [Interval(5, 12)]
    merged_touch = union([Interval(5, 9), Interval(9, 12)])
    assert merged_touch == [Interval(5, 12)]


# --------------------------------------------------------------------------- #
# duration reset
# --------------------------------------------------------------------------- #


def test_maintenance_resets_duration_post_window_piece_too_short():
    # truth [6,17); maintenance [11,13): pre piece 5 min passes (>=300s),
    # post piece 4 min is filtered — the 11 minutes do not bridge the window.
    events = {(TEMP_COND.device, TEMP_COND.metric): high_temp_series()}
    faults, suppressed = evaluate_rule_with_maintenance(
        1, "AND", 300, [TEMP_COND], events, HORIZON,
        {"pump-1": [Interval(11 * 60, 13 * 60)]},
    )
    assert ivs(faults) == [(6 * 60, 11 * 60)]
    assert ivs_supp(suppressed) == [(11 * 60, 13 * 60)]
    # surviving fault is confirmed relative to its own start
    assert faults[0].confirmed_at == 6 * 60 + 300


def test_duration_reenumerates_after_window():
    # duration 240s: pre piece 5 min and post piece 4 min both qualify, and
    # the post piece gets a fresh confirmation timestamp.
    events = {(TEMP_COND.device, TEMP_COND.metric): high_temp_series()}
    faults, suppressed = evaluate_rule_with_maintenance(
        1, "AND", 240, [TEMP_COND], events, HORIZON,
        {"pump-1": [Interval(11 * 60, 13 * 60)]},
    )
    assert ivs(faults) == [(6 * 60, 11 * 60), (13 * 60, 17 * 60)]
    assert faults[1].confirmed_at == 13 * 60 + 240
    assert ivs_supp(suppressed) == [(11 * 60, 13 * 60)]


def test_maintenance_touching_truth_boundary_does_not_suppress():
    # window ends exactly when truth begins at 00:06: nothing is removed
    events = {(TEMP_COND.device, TEMP_COND.metric): high_temp_series()}
    faults, suppressed = evaluate_rule_with_maintenance(
        1, "AND", 300, [TEMP_COND], events, HORIZON,
        {"pump-1": [Interval(4 * 60, 6 * 60)]},
    )
    assert ivs(faults) == [(6 * 60, 17 * 60)]
    assert suppressed == []


def test_other_device_maintenance_does_not_affect_rule():
    events = {(TEMP_COND.device, TEMP_COND.metric): high_temp_series()}
    faults, suppressed = evaluate_rule_with_maintenance(
        1, "AND", 300, [TEMP_COND], events, HORIZON,
        {"pump-2": [Interval(6 * 60, 17 * 60)]},
    )
    assert ivs(faults) == [(6 * 60, 17 * 60)]
    assert suppressed == []


def test_or_rule_maintenance_suppresses_one_condition_branch():
    # vib high [3,6) (device pump-2); press low [6,9)+[10,14); maintenance on
    # pump-2 [7,10) cuts [6,9) into [6,7) and removes the touching bridge at
    # minute 9? [6,9)-[7,10) = [6,7); vib piece [3,6) then touches [6,7):
    # [3,7) is 4 min >= 200s -> one fault; [10,14) is another.
    vib = minute_series(300, lambda m: 6.0 if 3 <= m <= 5 else 2.0,
                        device="pump-2", metric="vib")
    press = minute_series(400, lambda m: 26.0 if (6 <= m <= 8 or 10 <= m <= 13) else 52.0,
                          device="pump-2", metric="press")
    conds = [Condition("pump-2", "vib", ">", 5), Condition("pump-2", "press", "<", 30)]
    faults, suppressed = evaluate_rule_with_maintenance(
        3, "OR", 200, conds,
        {("pump-2", "vib"): vib, ("pump-2", "press"): press},
        HORIZON, {"pump-2": [Interval(7 * 60, 10 * 60)]},
    )
    assert ivs(faults) == [(3 * 60, 7 * 60), (10 * 60, 14 * 60)]
    assert ivs_supp(suppressed) == [(7 * 60, 9 * 60)]


def test_and_rule_maintenance_on_one_device_cuts_intersection():
    temp = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 60,
                         device="pump-1", metric="temp")
    vib = minute_series(200, lambda m: 3.6 if 12 <= m <= 13 else 1.0,
                        device="pump-1", metric="vib")
    # base AND truth is [12,14); maintenance on the single device removes it
    faults, suppressed = evaluate_rule_with_maintenance(
        2, "AND", 0,
        [Condition("pump-1", "temp", ">=", 80), Condition("pump-1", "vib", ">", 3)],
        {("pump-1", "temp"): temp, ("pump-1", "vib"): vib},
        HORIZON, {"pump-1": [Interval(12 * 60, 14 * 60)]},
    )
    assert faults == []
    assert ivs_supp(suppressed) == [(12 * 60, 14 * 60)]


def test_evidence_cannot_cross_maintenance_boundary():
    # maintenance [12,13) splits the episode; post piece [13,17) is a 4-min
    # fault only at duration 0; its evidence must be clipped to [13,17)
    events = {(TEMP_COND.device, TEMP_COND.metric): high_temp_series()}
    faults, _ = evaluate_rule_with_maintenance(
        1, "AND", 0, [TEMP_COND], events, HORIZON,
        {"pump-1": [Interval(12 * 60, 13 * 60)]},
    )
    pre, post = faults[0], faults[1]
    assert (pre.start, pre.end) == (6 * 60, 12 * 60)
    assert (post.start, post.end) == (13 * 60, 17 * 60)
    ev_ts = [e.ts for e in post.evidence[0].events]
    assert ev_ts == [m * 60 for m in range(13, 17)]
    assert post.evidence[0].held_intervals == [Interval(13 * 60, 17 * 60)]
    pre_ts = [e.ts for e in pre.evidence[0].events]
    assert pre_ts == [m * 60 for m in range(6, 12)]


def test_evaluate_rule_backward_compatible_without_maintenance():
    events = {(TEMP_COND.device, TEMP_COND.metric): high_temp_series()}
    faults = evaluate_rule(1, "AND", 300, [TEMP_COND], events, HORIZON)
    assert ivs(faults) == [(6 * 60, 17 * 60)]


# --------------------------------------------------------------------------- #
# integration: services + sqlite (CRUD, merge, persistence, export payload)
# --------------------------------------------------------------------------- #


def test_maintenance_integration():
    """CRUD + evaluation through services against an isolated SQLite file."""
    import json
    import os
    import tempfile
    import time

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    os.environ["SENSOR_DB_PATH"] = db_path

    from app import db, services

    # point the lazy singleton at the isolated database
    db.DB_PATH = db_path
    db._conn = None
    db.init_db()

    lines = "\n".join(
        json.dumps({"device": "d", "metric": "m", "ts": i * 60, "value": 90})
        for i in range(10)
    )
    services.import_events(lines)

    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        ("R", "AND", 300, time.time()),
    )
    rule_id = cur.lastrowid
    conn.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord) VALUES (?,?,?,?,?,?)",
        (rule_id, "d", "m", ">=", 80, 0),
    )
    conn.commit()

    # baseline: [0,540) held by the last sample, one fault of 9 minutes
    result = services.evaluate_all_rules()
    assert result["fault_intervals"] == 1
    assert result["suppressed_intervals"] == 0

    # two overlapping windows merge per device -> one suppressed fragment
    services.create_maintenance_window("d", 200, 260, "M1")
    services.create_maintenance_window("d", 240, 300, "M2")
    faults = services.query_faults()
    suppressed = services.query_suppressed()
    # pieces [0,200)=200s and [300,540)=240s both fall short of 300s
    assert faults == []
    assert len(suppressed) == 1
    assert (suppressed[0]["start"], suppressed[0]["end"]) == (200.0, 300.0)
    assert suppressed[0]["rule_id"] == rule_id
    assert {w["name"] for w in suppressed[0]["windows"]} == {"M1", "M2"}

    # timeline payload carries both windows and suppressed fragments
    tl = services.timeline_data(None, None)
    assert len(tl["maintenance_windows"]) == 2
    assert len(tl["suppressed"]) == 1
    assert dict(tl["rule_devices"]) == {rule_id: ["d"]}

    # editing a window re-evaluates; linked names follow
    windows = services.query_maintenance_windows()
    m2 = next(w for w in windows if w["name"] == "M2")
    services.update_maintenance_window(m2["id"], "d", 240, 300, "M2-edit", None)
    suppressed2 = services.query_suppressed()
    assert len(suppressed2) == 1
    assert {w["name"] for w in suppressed2[0]["windows"]} == {"M1", "M2-edit"}

    # deleting all windows restores the original single fault
    for w in services.query_maintenance_windows():
        services.delete_maintenance_window(w["id"])
    assert len(services.query_faults()) == 1
    assert services.query_suppressed() == []

    # reset clears maintenance windows too
    services.create_maintenance_window("d", 100, 200, "M")
    db.reset_all()
    assert services.query_maintenance_windows() == []
