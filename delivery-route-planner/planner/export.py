"""Exports: Excel driver sheets, CSV and Google Maps navigation links."""

from __future__ import annotations

import io
import re
from urllib.parse import quote

import pandas as pd

from .data import fmt_time
from .solver import Baseline, Solution

GMAPS_MAX_WAYPOINTS = 9  # Google Maps URLs accept origin + destination + 9 waypoints


def _ll(lat: float, lon: float) -> str:
    return f"{lat:.6f},{lon:.6f}"


def gmaps_point_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={_ll(lat, lon)}"


def gmaps_route_urls(points: list[tuple[float, float]]) -> list[str]:
    """Split a depot->stops->depot polyline into Google Maps direction links.

    Each link covers at most 11 points (origin, 9 waypoints, destination);
    consecutive links share their boundary point so the driver just opens the
    next one when the previous leg ends.
    """
    if len(points) < 2:
        return []
    urls = []
    chunk = GMAPS_MAX_WAYPOINTS + 2
    i = 0
    while i < len(points) - 1:
        seg = points[i : i + chunk]
        origin, dest, mids = seg[0], seg[-1], seg[1:-1]
        url = (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={_ll(*origin)}&destination={_ll(*dest)}&travelmode=driving"
        )
        if mids:
            url += "&waypoints=" + quote("|".join(_ll(*p) for p in mids), safe=",")
        urls.append(url)
        i += chunk - 1
    return urls


def route_points(route, depot: tuple[float, float]) -> list[tuple[float, float]]:
    stops = [] if route.schedule.empty else list(zip(route.schedule["lat"], route.schedule["lon"]))
    return [depot] + stops + [depot]


def schedule_table(route) -> pd.DataFrame:
    """Human-readable stop table for one route."""
    if route.schedule.empty:
        return pd.DataFrame()
    s = route.schedule.copy()
    out = pd.DataFrame(
        {
            "ลำดับ": s["seq"],
            "Order": s["order_id"],
            "ลูกค้า": s["customer"],
            "ที่อยู่": s["address"],
            "ถึง": s["arrival"].map(fmt_time),
            "เริ่มลงของ": s["service_start"].map(fmt_time),
            "ออก": s["depart"].map(fmt_time),
            "ช่วงเวลารับ": s["tw_start"].map(fmt_time) + "–" + s["tw_end"].map(fmt_time),
            "รอ (นาที)": s["wait_min"],
            "ระยะจากจุดก่อน (กม.)": s["leg_km"].round(2),
            "จำนวน": s["demand"],
            "โหลดสะสม": s["load_after"],
            "สาย": s["late"].map({True: "⚠️", False: ""}),
            "หมายเหตุ": s["note"],
            "Google Maps": [gmaps_point_url(a, b) for a, b in zip(s["lat"], s["lon"])],
        }
    )
    return out


def summary_table(solution: Solution) -> pd.DataFrame:
    rows = []
    for r in solution.routes:
        rows.append(
            {
                "รถ": r.vehicle.name,
                "จุดส่ง": len(r.order_idx),
                "ระยะทาง (กม.)": round(r.distance_km, 1),
                "เวลาขับ (นาที)": r.drive_min,
                "เวลาลงของ (นาที)": r.service_min,
                "เวลารอ (นาที)": r.wait_min,
                "ออกคลัง": fmt_time(r.start) if r.order_idx else "-",
                "กลับคลัง": fmt_time(r.end) if r.order_idx else "-",
                "โหลด / ความจุ": f"{r.load} / {r.vehicle.capacity}",
                "ใช้ความจุ (%)": round(100 * r.load / r.vehicle.capacity, 0)
                if r.vehicle.capacity
                else 0,
                "ค่าใช้จ่ายประมาณ (บาท)": round(r.cost, 0),
            }
        )
    return pd.DataFrame(rows)


def _safe_sheet(name: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]\*\?/\\:]", "-", str(name))[:28] or "Route"
    out, k = base, 2
    while out in used:
        out = f"{base[:25]}_{k}"
        k += 1
    used.add(out)
    return out


def build_excel(
    solution: Solution,
    orders: pd.DataFrame,
    depot: tuple[float, float],
    baseline: Baseline | None = None,
) -> bytes:
    buf = io.BytesIO()
    used: set[str] = set()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        summary = summary_table(solution)
        summary.to_excel(xw, sheet_name=_safe_sheet("Summary", used), index=False)

        for r in solution.used_routes:
            sheet = _safe_sheet(r.vehicle.name, used)
            tbl = schedule_table(r)
            tbl.to_excel(xw, sheet_name=sheet, index=False, startrow=0)
            ws = xw.sheets[sheet]
            links = gmaps_route_urls(route_points(r, depot))
            row = len(tbl) + 3
            ws.cell(row=row, column=1, value="นำทาง Google Maps (เปิดทีละลิงก์ตามลำดับ)")
            for k, url in enumerate(links, 1):
                ws.cell(row=row + k, column=1, value=f"ช่วงที่ {k}")
                c2 = ws.cell(row=row + k, column=2, value=url)
                c2.hyperlink = url
                c2.style = "Hyperlink"
            for col_cells in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells[:60])
                ws.column_dimensions[col_cells[0].column_letter].width = min(max(10, width + 2), 45)

        if solution.unassigned:
            un = orders.iloc[solution.unassigned][
                ["order_id", "customer", "address", "demand", "tw_start", "tw_end", "priority"]
            ].copy()
            un["tw_start"] = un["tw_start"].map(fmt_time)
            un["tw_end"] = un["tw_end"].map(fmt_time)
            un.to_excel(xw, sheet_name=_safe_sheet("Unassigned", used), index=False)

        if baseline is not None:
            b = pd.DataFrame(
                [
                    {
                        "เส้นทางเดิม": r.vehicle.name,
                        "จุดส่ง": len(r.order_idx),
                        "ระยะทาง (กม.)": round(r.distance_km, 1),
                        "เวลาขับ (นาที)": r.drive_min,
                        "ส่งสาย (จุด)": r.late_stops,
                    }
                    for r in baseline.routes
                ]
            )
            b.to_excel(xw, sheet_name=_safe_sheet("Current plan", used), index=False)
    return buf.getvalue()


def build_flat_csv(solution: Solution) -> bytes:
    frames = []
    for r in solution.used_routes:
        s = r.schedule.copy()
        s.insert(0, "vehicle", r.vehicle.name)
        for col in ("arrival", "service_start", "depart", "tw_start", "tw_end"):
            s[col] = s[col].map(fmt_time)
        frames.append(s)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return df.to_csv(index=False).encode("utf-8-sig")
