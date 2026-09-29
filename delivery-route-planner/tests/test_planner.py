from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from planner.data import fmt_time, normalise_columns, parse_time, validate_orders
from planner.export import build_excel, gmaps_route_urls
from planner.geo import build_haversine_matrix, decode_plus_code, haversine_km, is_valid_plus_code
from planner.solver import SolverSettings, Vehicle, evaluate_current_plan, solve_routes

ROOT = Path(__file__).resolve().parents[1]
DEPOT = (13.7247, 100.5452)


# ------------------------------------------------------------------ geo ---
def test_haversine_known_distance():
    # Bangkok (Victory Monument) -> Chiang Mai (Tha Phae Gate) ≈ 580 km straight line
    assert 570 < haversine_km(13.7650, 100.5383, 18.7877, 98.9937) < 600


@pytest.mark.parametrize(
    "code,lat,lon",
    [("8FVC9G8F+6X", 47.3655625, 8.5249375), ("9C3W9QCJ+2VX", 51.3701125, -1.217765625)],
)
def test_plus_code_full(code, lat, lon):
    got = decode_plus_code(code)
    assert got == pytest.approx((lat, lon), abs=1e-6)


@pytest.mark.parametrize("short", ["9QCJ+2VX", "CJ+2VX", "+2VX"])
def test_plus_code_short_recovery(short):
    got = decode_plus_code(short, 51.3701125, -1.217765625)
    assert got == pytest.approx((51.3701125, -1.217765625), abs=1e-6)


def test_plus_code_with_locality_suffix():
    assert decode_plus_code("QGFV+X5 Bangkok", *DEPOT) == pytest.approx(
        decode_plus_code("7P52QGFV+X5"), abs=1e-9
    )


def test_invalid_plus_codes():
    for bad in ["", "hello", "8FVC9G8F6X", "8FVC9G8F+6", "8FV00G8F+6X"]:
        assert not is_valid_plus_code(bad)


# ----------------------------------------------------------------- data ---
@pytest.mark.parametrize(
    "value,expected",
    [("08:30", 510), ("8.30", 510), ("0830", 510), ("8", 480), (0.5, 720),
     (510, 510), ("2:15 pm", 855), ("12:00 am", 0), (None, None)],
)
def test_parse_time(value, expected):
    assert parse_time(value) == expected


def test_fmt_time():
    assert fmt_time(510) == "08:30"
    assert fmt_time(None) == ""


def test_column_aliases():
    df = normalise_columns(pd.DataFrame(columns=["OrderNo", "Latitude", "LNG", "Qty", "ชื่อลูกค้า"]))
    assert list(df.columns) == ["order_id", "lat", "lon", "demand", "customer"]


def test_validation_rejects_bad_rows():
    raw = pd.DataFrame(
        {
            "order_id": ["A", "B", "C", "D"],
            "lat": [13.73, None, 13.74, 100.55],
            "lon": [100.53, None, 100.56, 13.74],  # D has lat/lon swapped
            "tw_start": ["08:00", "08:00", "12:00", "08:00"],
            "tw_end": ["11:00", "11:00", "09:00", "11:00"],  # C window reversed
        }
    )
    res = validate_orders(raw, *DEPOT)
    assert list(res.orders["order_id"]) == ["A"]
    assert set(res.rejected["order_id"]) == {"B", "C", "D"}


def test_sample_file_is_valid():
    res = validate_orders(pd.read_csv(ROOT / "sample_data" / "orders_sample.csv"), *DEPOT)
    assert len(res.orders) == 30 and res.rejected.empty


# --------------------------------------------------------------- solver ---
def _orders(points, demand=None, tw=None, prio=None):
    n = len(points)
    return pd.DataFrame(
        {
            "order_id": [f"O{i}" for i in range(n)],
            "customer": [f"C{i}" for i in range(n)],
            "address": [""] * n,
            "lat": [p[0] for p in points],
            "lon": [p[1] for p in points],
            "demand": demand or [1] * n,
            "tw_start": [t[0] for t in tw] if tw else [420] * n,
            "tw_end": [t[1] for t in tw] if tw else [1080] * n,
            "service_min": [10] * n,
            "priority": prio or [1] * n,
            "note": [""] * n,
            "current_route": [""] * n,
            "current_seq": [None] * n,
        }
    )


def _matrix(orders):
    coords = [DEPOT] + list(zip(orders["lat"], orders["lon"]))
    return build_haversine_matrix(coords)


def test_solver_serves_everything_when_feasible():
    rng = np.random.default_rng(1)
    pts = [(DEPOT[0] + rng.uniform(-0.05, 0.05), DEPOT[1] + rng.uniform(-0.05, 0.05)) for _ in range(15)]
    orders = _orders(pts)
    tm = _matrix(orders)
    sol = solve_routes(orders, [Vehicle("V1", 100, 420, 1080)], tm.distance_m, tm.time_min,
                       SolverSettings(time_limit_s=2))
    assert sol.served == 15 and not sol.unassigned
    assert all(r.late_stops == 0 for r in sol.routes)


def test_capacity_drops_lowest_priority():
    pts = [(13.73, 100.53), (13.74, 100.55), (13.72, 100.56)]
    orders = _orders(pts, demand=[6, 6, 6], prio=[3, 1, 3])
    tm = _matrix(orders)
    sol = solve_routes(orders, [Vehicle("V1", 12, 420, 1080)], tm.distance_m, tm.time_min,
                       SolverSettings(time_limit_s=2))
    assert sol.unassigned == [1]  # the priority-1 order is dropped


def test_time_windows_respected():
    pts = [(13.73, 100.53), (13.76, 100.57), (13.70, 100.60)]
    tw = [(600, 630), (480, 510), (540, 560)]
    orders = _orders(pts, tw=tw)
    tm = _matrix(orders)
    sol = solve_routes(orders, [Vehicle("V1", 100, 420, 1080)], tm.distance_m, tm.time_min,
                       SolverSettings(time_limit_s=2))
    assert sol.served == 3
    sched = sol.routes[0].schedule
    assert list(sched["order_id"]) == ["O1", "O2", "O0"]
    assert (sched["service_start"] >= sched["tw_start"]).all()
    assert (sched["service_start"] <= sched["tw_end"]).all()


def test_baseline_and_excel_export():
    raw = pd.read_csv(ROOT / "sample_data" / "orders_sample.csv")
    orders = validate_orders(raw, *DEPOT).orders
    tm = _matrix(orders)
    vehicles = [Vehicle("รถ 1", 180, 420, 1080, 7), Vehicle("รถ 2", 180, 420, 1080, 7)]
    sol = solve_routes(orders, vehicles, tm.distance_m, tm.time_min, SolverSettings(time_limit_s=3))
    base = evaluate_current_plan(orders, tm.distance_m, tm.time_min, vehicles[0])
    assert base is not None and base.covered_orders == 30
    assert sol.total_km < base.total_km
    xlsx = build_excel(sol, orders, DEPOT, base)
    assert xlsx[:2] == b"PK"  # zip container


def test_gmaps_links_are_chunked():
    pts = [(13.7 + i * 0.001, 100.5) for i in range(25)]
    urls = gmaps_route_urls(pts)
    assert len(urls) == 3
    assert all(u.count("%7C") <= 8 for u in urls)  # ≤ 9 waypoints each
