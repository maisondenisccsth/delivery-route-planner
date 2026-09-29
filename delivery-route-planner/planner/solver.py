"""Vehicle routing optimiser (CVRPTW) built on Google OR-Tools.

Model
-----
* Node 0 is the depot; node ``i + 1`` is order row ``i``.
* Minimises total driven distance.
* Hard constraints: vehicle capacity, customer time windows, driver shift
  (start and return to depot inside the shift).
* Orders that cannot fit are *dropped* with a penalty instead of making the
  whole problem infeasible; higher priority = bigger penalty = kept first.
* Optional: balance workload between drivers (global span cost on time) and
  prefer using fewer vehicles (fixed cost per used vehicle).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

PRIORITY_WEIGHT = {1: 1, 2: 3, 3: 10}


@dataclass
class Vehicle:
    name: str
    capacity: int
    shift_start: int  # minutes after midnight
    shift_end: int
    cost_per_km: float = 0.0  # used for cost reporting
    fixed_cost: float = 0.0  # cost per day if the vehicle is used (reporting)


@dataclass
class SolverSettings:
    time_limit_s: int = 10
    balance_coef: int = 0  # 0 = off; higher = more even route durations
    minimize_vehicles: bool = False
    max_wait_min: int = 240
    drop_penalty: int = 10_000_000


@dataclass
class RoutePlan:
    vehicle: Vehicle
    order_idx: list[int]  # order row indices in visiting order
    schedule: pd.DataFrame  # one row per stop
    distance_km: float
    drive_min: int
    service_min: int
    wait_min: int
    start: int
    end: int
    load: int
    late_stops: int

    @property
    def duration_min(self) -> int:
        return self.end - self.start

    @property
    def cost(self) -> float:
        return self.distance_km * self.vehicle.cost_per_km + (
            self.vehicle.fixed_cost if self.order_idx else 0.0
        )


@dataclass
class Solution:
    routes: list[RoutePlan]
    unassigned: list[int]
    status: str
    solve_seconds: float
    messages: list[str] = field(default_factory=list)

    @property
    def used_routes(self) -> list[RoutePlan]:
        return [r for r in self.routes if r.order_idx]

    @property
    def total_km(self) -> float:
        return sum(r.distance_km for r in self.routes)

    @property
    def total_drive_min(self) -> int:
        return sum(r.drive_min for r in self.routes)

    @property
    def total_duration_min(self) -> int:
        return sum(r.duration_min for r in self.used_routes)

    @property
    def total_cost(self) -> float:
        return sum(r.cost for r in self.routes)

    @property
    def served(self) -> int:
        return sum(len(r.order_idx) for r in self.routes)


# --------------------------------------------------------------------------- #
# Route simulation (shared by optimiser output and the "current plan" baseline)
# --------------------------------------------------------------------------- #
def simulate_route(
    order_idx: list[int],
    orders: pd.DataFrame,
    dist_m: np.ndarray,
    time_min: np.ndarray,
    vehicle: Vehicle,
    depart_depot: int | None = None,
    service_start_times: list[int] | None = None,
) -> RoutePlan:
    """Walk a fixed stop sequence and compute arrival/wait/late times.

    ``service_start_times`` lets the optimiser pass its own schedule; without it
    the driver leaves the depot at ``depart_depot`` (default: shift start) and
    waits whenever they arrive before a window opens.
    """
    t = vehicle.shift_start if depart_depot is None else depart_depot
    start = t
    prev = 0
    rows = []
    total_m = drive = svc_total = wait_total = late = 0
    load = 0
    for k, oi in enumerate(order_idx):
        node = oi + 1
        o = orders.iloc[oi]
        leg_m = int(dist_m[prev, node])
        leg_t = int(time_min[prev, node])
        arrival = t + leg_t
        if service_start_times is not None:
            begin = max(arrival, int(service_start_times[k]))
        else:
            begin = max(arrival, int(o["tw_start"]))
        wait = begin - arrival
        is_late = begin > int(o["tw_end"])
        depart = begin + int(o["service_min"])
        load += int(o["demand"])
        rows.append(
            {
                "seq": k + 1,
                "order_id": o["order_id"],
                "customer": o.get("customer", ""),
                "address": o.get("address", ""),
                "lat": float(o["lat"]),
                "lon": float(o["lon"]),
                "demand": int(o["demand"]),
                "load_after": load,
                "tw_start": int(o["tw_start"]),
                "tw_end": int(o["tw_end"]),
                "arrival": arrival,
                "wait_min": wait,
                "service_start": begin,
                "depart": depart,
                "leg_km": leg_m / 1000.0,
                "leg_min": leg_t,
                "late": bool(is_late),
                "priority": int(o.get("priority", 1)),
                "note": o.get("note", ""),
            }
        )
        total_m += leg_m
        drive += leg_t
        svc_total += int(o["service_min"])
        wait_total += wait
        late += int(is_late)
        t = depart
        prev = node
    if order_idx:
        total_m += int(dist_m[prev, 0])
        drive += int(time_min[prev, 0])
        t += int(time_min[prev, 0])
    else:
        t = start
    return RoutePlan(
        vehicle=vehicle,
        order_idx=list(order_idx),
        schedule=pd.DataFrame(rows),
        distance_km=total_m / 1000.0,
        drive_min=drive,
        service_min=svc_total,
        wait_min=wait_total,
        start=start,
        end=t,
        load=load,
        late_stops=late,
    )


# --------------------------------------------------------------------------- #
# Optimiser
# --------------------------------------------------------------------------- #
_STATUS = {
    0: "NOT_SOLVED",
    1: "SUCCESS",
    2: "NO_SOLUTION_FOUND",
    3: "TIMEOUT",
    4: "INVALID",
    5: "INFEASIBLE",
    7: "OPTIMAL",
}


def solve_routes(
    orders: pd.DataFrame,
    vehicles: list[Vehicle],
    dist_m: np.ndarray,
    time_min: np.ndarray,
    settings: SolverSettings | None = None,
) -> Solution:
    settings = settings or SolverSettings()
    t0 = _time.time()
    n_orders = len(orders)
    if not vehicles:
        raise ValueError("ต้องมีรถอย่างน้อย 1 คัน")
    if n_orders == 0:
        return Solution([], [], "NO_ORDERS", 0.0)

    n_nodes = n_orders + 1
    assert dist_m.shape == (n_nodes, n_nodes), "distance matrix size mismatch"

    demand = [0] + [int(d) for d in orders["demand"]]
    service = [0] + [int(s) for s in orders["service_min"]]
    tw = [(0, 24 * 60)] + list(zip(orders["tw_start"].astype(int), orders["tw_end"].astype(int)))
    dist = dist_m.astype(int).tolist()
    ttime = time_min.astype(int).tolist()

    manager = pywrapcp.RoutingIndexManager(n_nodes, len(vehicles), 0)
    routing = pywrapcp.RoutingModel(manager)

    def dist_cb(i, j):
        return dist[manager.IndexToNode(i)][manager.IndexToNode(j)]

    def time_cb(i, j):
        a, b = manager.IndexToNode(i), manager.IndexToNode(j)
        return ttime[a][b] + service[a]

    def demand_cb(i):
        return demand[manager.IndexToNode(i)]

    dist_idx = routing.RegisterTransitCallback(dist_cb)
    time_idx = routing.RegisterTransitCallback(time_cb)
    dem_idx = routing.RegisterUnaryTransitCallback(demand_cb)

    routing.SetArcCostEvaluatorOfAllVehicles(dist_idx)

    routing.AddDimensionWithVehicleCapacity(
        dem_idx, 0, [int(v.capacity) for v in vehicles], True, "Load"
    )

    horizon = 24 * 60
    routing.AddDimension(time_idx, int(settings.max_wait_min), horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    messages: list[str] = []
    earliest_shift = min(v.shift_start for v in vehicles)
    latest_shift = max(v.shift_end for v in vehicles)
    for i in range(n_orders):
        idx = manager.NodeToIndex(i + 1)
        lo, hi = tw[i + 1]
        lo_c, hi_c = max(lo, earliest_shift), min(hi, latest_shift)
        if lo_c > hi_c:
            messages.append(
                f"{orders.iloc[i]['order_id']}: ช่วงเวลารับของไม่ทับกับกะของรถคันใดเลย"
            )
            lo_c, hi_c = lo, hi  # leave as-is; solver will drop it
        time_dim.CumulVar(idx).SetRange(int(lo_c), int(hi_c))
        weight = PRIORITY_WEIGHT.get(int(orders.iloc[i].get("priority", 1)), 1)
        routing.AddDisjunction([idx], int(settings.drop_penalty * weight))

    for v_id, v in enumerate(vehicles):
        s_idx, e_idx = routing.Start(v_id), routing.End(v_id)
        time_dim.CumulVar(s_idx).SetRange(int(v.shift_start), int(v.shift_end))
        time_dim.CumulVar(e_idx).SetRange(int(v.shift_start), int(v.shift_end))
        routing.AddVariableMaximizedByFinalizer(time_dim.CumulVar(s_idx))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(e_idx))

    if settings.balance_coef > 0:
        time_dim.SetGlobalSpanCostCoefficient(int(settings.balance_coef))
    if settings.minimize_vehicles:
        routing.SetFixedCostOfAllVehicles(200_000)  # ~200 km worth of distance

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(max(1, int(settings.time_limit_s)))

    assignment = routing.SolveWithParameters(params)
    status = _STATUS.get(routing.status(), str(routing.status()))
    if assignment is None:
        return Solution(
            routes=[simulate_route([], orders, dist_m, time_min, v) for v in vehicles],
            unassigned=list(range(n_orders)),
            status=status,
            solve_seconds=_time.time() - t0,
            messages=messages + ["ไม่พบคำตอบที่เป็นไปได้ ลองเพิ่มรถ/ขยายกะ/ขยายช่วงเวลา"],
        )

    routes: list[RoutePlan] = []
    visited: set[int] = set()
    for v_id, v in enumerate(vehicles):
        idx = routing.Start(v_id)
        depart = assignment.Value(time_dim.CumulVar(idx))
        seq, starts = [], []
        idx = assignment.Value(routing.NextVar(idx))
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            seq.append(node - 1)
            starts.append(assignment.Value(time_dim.CumulVar(idx)))
            visited.add(node - 1)
            idx = assignment.Value(routing.NextVar(idx))
        if not seq:
            depart = v.shift_start
        routes.append(
            simulate_route(seq, orders, dist_m, time_min, v, depart, starts if seq else None)
        )

    unassigned = [i for i in range(n_orders) if i not in visited]
    return Solution(
        routes=routes,
        unassigned=unassigned,
        status=status,
        solve_seconds=_time.time() - t0,
        messages=messages,
    )


# --------------------------------------------------------------------------- #
# Baseline: the plan the user is running today (current_route + current_seq)
# --------------------------------------------------------------------------- #
@dataclass
class Baseline:
    routes: list[RoutePlan]
    covered_orders: int

    @property
    def total_km(self) -> float:
        return sum(r.distance_km for r in self.routes)

    @property
    def total_drive_min(self) -> int:
        return sum(r.drive_min for r in self.routes)

    @property
    def late_stops(self) -> int:
        return sum(r.late_stops for r in self.routes)


def evaluate_current_plan(
    orders: pd.DataFrame,
    dist_m: np.ndarray,
    time_min: np.ndarray,
    template_vehicle: Vehicle,
) -> Baseline | None:
    """Score the existing manual plan so it can be compared with the optimised one."""
    if "current_route" not in orders.columns:
        return None
    has_route = orders["current_route"].astype(str).str.strip().ne("")
    if not has_route.any():
        return None
    routes = []
    covered = 0
    tmp = orders[has_route].copy()
    tmp["_pos"] = np.arange(len(tmp))
    tmp["_seq"] = tmp["current_seq"].astype(float).fillna(1e9)
    for name, grp in tmp.groupby(tmp["current_route"].astype(str).str.strip(), sort=True):
        grp = grp.sort_values(["_seq", "_pos"])
        order_idx = [int(i) for i in grp.index]
        veh = Vehicle(
            name=str(name),
            capacity=10**9,
            shift_start=template_vehicle.shift_start,
            shift_end=template_vehicle.shift_end,
            cost_per_km=template_vehicle.cost_per_km,
            fixed_cost=template_vehicle.fixed_cost,
        )
        routes.append(simulate_route(order_idx, orders, dist_m, time_min, veh))
        covered += len(order_idx)
    return Baseline(routes=routes, covered_orders=covered)
