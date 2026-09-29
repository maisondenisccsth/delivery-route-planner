"""Folium map of the optimised routes."""

from __future__ import annotations

import html

import folium
import pandas as pd

from .data import fmt_time
from .solver import Solution

# Distinct, colour-blind-friendly-ish palette; cycles for large fleets
ROUTE_COLORS = [
    "#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c",
    "#0891b2", "#be185d", "#65a30d", "#4f46e5", "#b45309",
]


def route_color(i: int) -> str:
    return ROUTE_COLORS[i % len(ROUTE_COLORS)]


def _numbered_icon(n: int, color: str, late: bool = False) -> folium.DivIcon:
    border = "3px solid #facc15" if late else "2px solid white"
    return folium.DivIcon(
        icon_size=(26, 26),
        icon_anchor=(13, 13),
        html=(
            f'<div style="background:{color};color:white;border:{border};'
            "border-radius:50%;width:26px;height:26px;display:flex;align-items:center;"
            "justify-content:center;font:600 12px/1 system-ui,sans-serif;"
            f'box-shadow:0 1px 3px rgba(0,0,0,.4)">{n}</div>'
        ),
    )


def build_route_map(
    solution: Solution | None,
    orders: pd.DataFrame,
    depot: tuple[float, float],
    depot_name: str = "Depot",
    geometries: dict[int, list[tuple[float, float]]] | None = None,
) -> folium.Map:
    """Map with depot, numbered stops per route and route lines.

    ``geometries`` maps route index -> road polyline (from OSRM); routes
    without one are drawn as straight segments between stops.
    """
    fmap = folium.Map(location=depot, zoom_start=12, tiles="OpenStreetMap", control_scale=True)

    folium.Marker(
        depot,
        tooltip=f"🏭 {depot_name}",
        icon=folium.Icon(color="black", icon="home", prefix="fa"),
    ).add_to(fmap)

    bounds = [depot]
    if solution is None:
        grp = folium.FeatureGroup(name="Orders")
        for _, o in orders.iterrows():
            folium.CircleMarker(
                (o["lat"], o["lon"]),
                radius=6,
                color="#475569",
                fill=True,
                fill_opacity=0.8,
                tooltip=f"{o['order_id']} · {o.get('customer', '')}",
            ).add_to(grp)
            bounds.append((o["lat"], o["lon"]))
        grp.add_to(fmap)
    else:
        for i, r in enumerate(solution.routes):
            if not r.order_idx:
                continue
            color = route_color(i)
            grp = folium.FeatureGroup(
                name=f"{r.vehicle.name} · {len(r.order_idx)} จุด · {r.distance_km:.1f} กม."
            )
            pts = [depot] + list(zip(r.schedule["lat"], r.schedule["lon"])) + [depot]
            line = (geometries or {}).get(i) or pts
            folium.PolyLine(line, color=color, weight=4, opacity=0.8).add_to(grp)
            for _, s in r.schedule.iterrows():
                popup = (
                    f"<b>{html.escape(str(s['order_id']))}</b><br>"
                    f"{html.escape(str(s['customer']))}<br>"
                    f"ถึง {fmt_time(s['arrival'])} · ลงของ {fmt_time(s['service_start'])}<br>"
                    f"ช่วงรับ {fmt_time(s['tw_start'])}–{fmt_time(s['tw_end'])}<br>"
                    f"จำนวน {s['demand']}"
                    + ("<br><b style='color:#b45309'>⚠️ ส่งสาย</b>" if s["late"] else "")
                )
                folium.Marker(
                    (s["lat"], s["lon"]),
                    icon=_numbered_icon(int(s["seq"]), color, bool(s["late"])),
                    tooltip=f"{r.vehicle.name} #{int(s['seq'])} · {s['customer'] or s['order_id']}",
                    popup=folium.Popup(popup, max_width=260),
                ).add_to(grp)
                bounds.append((s["lat"], s["lon"]))
            grp.add_to(fmap)

        if solution.unassigned:
            grp = folium.FeatureGroup(name=f"ไม่ได้จัดส่ง ({len(solution.unassigned)})")
            for oi in solution.unassigned:
                o = orders.iloc[oi]
                folium.CircleMarker(
                    (o["lat"], o["lon"]),
                    radius=7,
                    color="#6b7280",
                    fill=True,
                    fill_color="#e5e7eb",
                    fill_opacity=1,
                    dash_array="3",
                    tooltip=f"ไม่ได้จัดส่ง: {o['order_id']} · {o.get('customer', '')}",
                ).add_to(grp)
                bounds.append((o["lat"], o["lon"]))
            grp.add_to(fmap)

    folium.LayerControl(collapsed=True).add_to(fmap)
    if len(bounds) > 1:
        fmap.fit_bounds(bounds, padding=(20, 20))
    return fmap
