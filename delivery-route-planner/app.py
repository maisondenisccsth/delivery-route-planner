"""Delivery Route Planner — Streamlit app.

Run locally:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import time
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from planner import __version__
from planner.data import (
    fmt_time,
    parse_time,
    read_orders_file,
    template_dataframe,
    validate_orders,
)
from planner.export import (
    build_excel,
    build_flat_csv,
    gmaps_route_urls,
    route_points,
    schedule_table,
    summary_table,
)
from planner.geo import (
    DEFAULT_OSRM_URL,
    build_haversine_matrix,
    build_osrm_matrix,
    decode_plus_code,
    fetch_osrm_route_geometry,
    is_valid_plus_code,
)
from planner.solver import SolverSettings, Vehicle, evaluate_current_plan, solve_routes
from planner.viz import build_route_map, route_color

SAMPLE_FILE = Path(__file__).parent / "sample_data" / "orders_sample.csv"
DEFAULT_DEPOT = (13.7247, 100.5452)  # Sathorn, Bangkok — change in the sidebar

st.set_page_config(
    page_title="Delivery Route Planner",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --------------------------------------------------------------------------- #
# Cached helpers
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False, max_entries=20)
def get_matrix(coords: tuple, method: str, circuity: float, speed: float, osrm_url: str, traffic: float):
    coords = list(coords)
    if method == "osrm":
        return build_osrm_matrix(coords, osrm_url, traffic, circuity, speed)
    return build_haversine_matrix(coords, circuity, speed)


@st.cache_data(show_spinner=False, max_entries=100)
def get_geometry(points: tuple, osrm_url: str):
    return fetch_osrm_route_geometry(list(points), osrm_url)


def df_signature(*objs) -> str:
    h = hashlib.sha1()
    for o in objs:
        if isinstance(o, pd.DataFrame):
            h.update(pd.util.hash_pandas_object(o.astype(str), index=True).values.tobytes())
        else:
            h.update(json.dumps(o, sort_keys=True, default=str).encode())
    return h.hexdigest()


def default_fleet() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"name": "รถ 1", "capacity": 180, "shift_start": "07:00", "shift_end": "18:00",
             "cost_per_km": 7.0, "fixed_cost": 0.0},
            {"name": "รถ 2", "capacity": 180, "shift_start": "07:00", "shift_end": "18:00",
             "cost_per_km": 7.0, "fixed_cost": 0.0},
        ]
    )


def parse_fleet(df: pd.DataFrame) -> tuple[list[Vehicle], list[str]]:
    vehicles, errors = [], []
    for i, r in df.reset_index(drop=True).iterrows():
        name = str(r.get("name") or "").strip()
        if not name or name.lower() == "nan":
            if all(pd.isna(r.get(c)) or str(r.get(c)).strip() == "" for c in df.columns):
                continue  # blank row from the editor
            name = f"รถ {i + 1}"
        try:
            cap = int(float(r.get("capacity")))
            s = parse_time(r.get("shift_start"))
            e = parse_time(r.get("shift_end"))
            if cap <= 0:
                raise ValueError("ความจุต้องมากกว่า 0")
            if s is None or e is None or e <= s:
                raise ValueError("เวลาเริ่ม/เลิกกะไม่ถูกต้อง")
            cpk = float(r.get("cost_per_km") or 0)
            fix = float(r.get("fixed_cost") or 0)
        except (TypeError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        vehicles.append(Vehicle(name, cap, s, e, cpk, fix))
    names = [v.name for v in vehicles]
    if len(set(names)) != len(names):
        errors.append("ชื่อรถซ้ำกัน — ตั้งชื่อให้ไม่ซ้ำ")
    return vehicles, errors


def fmt_hm(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes // 60} ชม. {minutes % 60:02d} น."


# --------------------------------------------------------------------------- #
# Sidebar — settings
# --------------------------------------------------------------------------- #
ss = st.session_state
ss.setdefault("fleet", default_fleet())

with st.sidebar:
    st.title("⚙️ ตั้งค่า")

    with st.expander("🏭 คลังสินค้า / จุดออกรถ", expanded=True):
        depot_name = st.text_input("ชื่อคลัง", value="Depot (Sathorn)")
        c1, c2 = st.columns(2)
        depot_lat = c1.number_input("Latitude", value=DEFAULT_DEPOT[0], format="%.6f", step=0.0001)
        depot_lon = c2.number_input("Longitude", value=DEFAULT_DEPOT[1], format="%.6f", step=0.0001)
        depot_pc = st.text_input("หรือใส่ Plus code (ถ้ามี จะใช้แทน lat/lon)", value="")
        if depot_pc.strip():
            if is_valid_plus_code(depot_pc):
                try:
                    depot_lat, depot_lon = decode_plus_code(depot_pc, depot_lat, depot_lon)
                    st.caption(f"→ {depot_lat:.6f}, {depot_lon:.6f}")
                except ValueError as exc:
                    st.error(str(exc))
            else:
                st.error("Plus code ไม่ถูกต้อง")
    depot = (float(depot_lat), float(depot_lon))

    with st.expander("🚚 รถและคนขับ", expanded=True):
        st.caption("ความจุใช้หน่วยเดียวกับคอลัมน์ `demand` (ลัง, กก., คิว ฯลฯ)")
        fleet_df = st.data_editor(
            ss.fleet,
            num_rows="dynamic",
            hide_index=True,
            key="fleet_editor",
            column_config={
                "name": st.column_config.TextColumn("ชื่อรถ", required=True),
                "capacity": st.column_config.NumberColumn("ความจุ", min_value=1, step=1),
                "shift_start": st.column_config.TextColumn("เริ่มกะ", help="HH:MM"),
                "shift_end": st.column_config.TextColumn("เลิกกะ", help="HH:MM"),
                "cost_per_km": st.column_config.NumberColumn("บาท/กม.", min_value=0.0, format="%.2f"),
                "fixed_cost": st.column_config.NumberColumn("ค่าคงที่/วัน", min_value=0.0, format="%.0f"),
            },
        )
        vehicles, fleet_errors = parse_fleet(fleet_df)
        for e in fleet_errors:
            st.error(e)

    with st.expander("🛣️ วิธีคำนวณระยะทาง", expanded=False):
        method_label = st.radio(
            "แหล่งข้อมูลระยะทาง",
            ["ประมาณการ (ออฟไลน์)", "ถนนจริง (OSRM)"],
            help="ประมาณการ = ระยะเส้นตรง × ตัวคูณความคดเคี้ยว, ไม่ต้องใช้อินเทอร์เน็ต/API key\n\n"
            "OSRM = ระยะทางตามถนนจริงจาก OpenStreetMap (ไม่รวมรถติด)",
        )
        method = "osrm" if method_label.startswith("ถนน") else "haversine"
        circuity = st.slider("ตัวคูณความคดเคี้ยวของถนน", 1.0, 2.0, 1.35, 0.05,
                             help="ระยะถนนจริง ÷ ระยะเส้นตรง — ในเมืองใหญ่มักอยู่ราว 1.3–1.5")
        speed = st.slider("ความเร็วเฉลี่ย (กม./ชม.)", 8, 60, 22,
                          help="ในกรุงเทพฯ ช่วงกลางวันมักอยู่ราว 15–25 กม./ชม.")
        osrm_url = DEFAULT_OSRM_URL
        traffic = 1.5
        if method == "osrm":
            osrm_url = st.text_input("OSRM server", value=DEFAULT_OSRM_URL)
            traffic = st.slider("ตัวคูณรถติด", 1.0, 3.0, 1.5, 0.1,
                                help="เวลาจาก OSRM ไม่รวมรถติด จึงคูณเพิ่ม")
            st.caption("เซิร์ฟเวอร์ demo สาธารณะเหมาะกับการทดลองเท่านั้น (≤100 จุด) "
                       "ถ้าติดต่อไม่ได้ ระบบจะใช้ค่าประมาณการแทนอัตโนมัติ")

    with st.expander("📦 ค่าเริ่มต้นของออเดอร์", expanded=False):
        default_service = st.number_input("เวลาลงของต่อจุด (นาที)", 0, 180, 15)
        c1, c2 = st.columns(2)
        d_tw_s = c1.time_input("รับของได้ตั้งแต่", value=time(7, 0), step=900)
        d_tw_e = c2.time_input("ถึง", value=time(18, 0), step=900)
        max_km = st.number_input("เตือนถ้าไกลจากคลังเกิน (กม.)", 10, 2000, 150)

    with st.expander("🧠 ตัว Optimize", expanded=False):
        time_limit = st.slider("เวลาคำนวณสูงสุด (วินาที)", 3, 120, 10,
                               help="ให้เวลามากขึ้น = ผลลัพธ์มักดีขึ้น (โดยเฉพาะเมื่อมีจุดส่งเยอะ)")
        balance_label = st.select_slider("เกลี่ยงานให้คนขับเท่า ๆ กัน",
                                         options=["ปิด", "ต่ำ", "กลาง", "สูง"], value="ปิด")
        balance = {"ปิด": 0, "ต่ำ": 20, "กลาง": 100, "สูง": 500}[balance_label]
        min_vehicles = st.checkbox("พยายามใช้รถให้น้อยคันที่สุด", value=False)
        max_wait = st.slider("รอหน้าร้านได้ไม่เกิน (นาที)", 0, 360, 240)

    st.caption(f"v{__version__} · OR-Tools + Streamlit")


# --------------------------------------------------------------------------- #
# Main — header
# --------------------------------------------------------------------------- #
st.title("🚚 Delivery Route Planner")
st.caption(
    "วางแผนเส้นทางจัดส่งหลายคันรถ โดยคำนึงถึงความจุรถ ช่วงเวลารับของ กะคนขับ และลำดับความสำคัญ"
)

with st.expander("📘 วิธีใช้ & รูปแบบไฟล์", expanded=False):
    st.markdown(
        """
1. ตั้งค่า **คลัง** และ **รถ** ที่แถบซ้าย
2. อัปโหลดไฟล์ออเดอร์ (CSV หรือ Excel) — หรือกด **ใช้ข้อมูลตัวอย่าง**
3. ตรวจ/แก้ข้อมูลในตาราง แล้วกด **🚀 คำนวณเส้นทาง**
4. ดูแผนที่ ตารางแต่ละคัน ลิงก์นำทาง Google Maps และดาวน์โหลด Excel ให้คนขับ

| คอลัมน์ | จำเป็น | ความหมาย |
|---|---|---|
| `order_id` | ✔ | รหัสออเดอร์ |
| `lat`, `lon` | ✔* | พิกัด (*หรือใช้ `plus_code` แทน) |
| `plus_code` | | Plus code เช่น `7P52QGFV+X5` หรือแบบสั้น `QGFV+X5` |
| `customer`, `address` | | ชื่อลูกค้า / ที่อยู่ (แสดงผลเท่านั้น) |
| `demand` | | ปริมาณสินค้า (ค่าเริ่มต้น 1) หน่วยเดียวกับความจุรถ |
| `tw_start`, `tw_end` | | ช่วงเวลารับของ เช่น `08:00`, `11:30` |
| `service_min` | | เวลาลงของ (นาที) |
| `priority` | | 1 = ปกติ, 2 = สำคัญ, 3 = ต้องส่ง — ถ้ารถไม่พอ จุดที่ priority ต่ำจะถูกตัดก่อน |
| `current_route`, `current_seq` | | แผนเดิมที่ใช้อยู่ (ชื่อเส้น + ลำดับ) เพื่อเทียบว่าประหยัดได้เท่าไร |
| `note` | | หมายเหตุถึงคนขับ |

ชื่อคอลัมน์ภาษาไทยหรือชื่อที่ใกล้เคียง (เช่น `latitude`, `lng`, `qty`, `OrderNo`) ระบบจะจับคู่ให้อัตโนมัติ
"""
    )

# --------------------------------------------------------------------------- #
# Step 1 — orders
# --------------------------------------------------------------------------- #
st.subheader("1 · ออเดอร์")
c_up, c_btn = st.columns([3, 1])
with c_up:
    upload = st.file_uploader(
        "อัปโหลดไฟล์ออเดอร์ (.csv / .xlsx)", type=["csv", "xlsx", "xls"], label_visibility="collapsed"
    )
with c_btn:
    if st.button("ใช้ข้อมูลตัวอย่าง", width="stretch"):
        ss.raw = pd.read_csv(SAMPLE_FILE)
        ss.raw_source = "sample"
        ss.pop("result", None)
    st.download_button(
        "ดาวน์โหลด template",
        template_dataframe().to_csv(index=False).encode("utf-8-sig"),
        file_name="orders_template.csv",
        mime="text/csv",
        width="stretch",
        on_click="ignore",
    )

if upload is not None:
    file_id = f"{upload.name}:{upload.size}"
    if ss.get("raw_source") != file_id:
        try:
            ss.raw = read_orders_file(upload.name, upload.getvalue())
            ss.raw_source = file_id
            ss.pop("result", None)
        except Exception as exc:
            st.error(f"อ่านไฟล์ไม่ได้: {exc}")

if "raw" not in ss:
    st.info("อัปโหลดไฟล์ออเดอร์ หรือกด **ใช้ข้อมูลตัวอย่าง** เพื่อเริ่มต้น")
    st.stop()

with st.expander(f"ตารางออเดอร์ ({len(ss.raw)} แถว) — แก้ไขได้โดยตรง", expanded=False):
    edited = st.data_editor(ss.raw, num_rows="dynamic", width="stretch", key=f"orders_{ss.raw_source}")

val = validate_orders(
    edited,
    depot_lat=depot[0],
    depot_lon=depot[1],
    default_service_min=int(default_service),
    default_tw_start=d_tw_s.hour * 60 + d_tw_s.minute,
    default_tw_end=d_tw_e.hour * 60 + d_tw_e.minute,
    max_km_from_depot=float(max_km),
)
orders = val.orders

total_cap = sum(v.capacity for v in vehicles)
m1, m2, m3, m4 = st.columns(4)
m1.metric("ออเดอร์พร้อมจัดเส้นทาง", f"{len(orders)}")
m2.metric("มีปัญหา", f"{len(val.rejected)}")
m3.metric("ปริมาณรวม", f"{int(orders['demand'].sum()) if len(orders) else 0:,}")
m4.metric("ความจุรถรวม", f"{total_cap:,}",
          delta=f"{total_cap - int(orders['demand'].sum()):,} เหลือ" if len(orders) else None)
for w in val.warnings:
    st.caption(f"ℹ️ {w}")
if len(val.rejected):
    with st.expander(f"⚠️ {len(val.rejected)} แถวที่จัดเส้นทางไม่ได้ (คลิกดูเหตุผล)", expanded=True):
        st.dataframe(val.rejected[["row", "order_id", "customer", "reason"]], hide_index=True, width="stretch")
if len(orders) and total_cap < orders["demand"].sum():
    st.warning("ความจุรถรวมน้อยกว่าปริมาณสินค้า — บางออเดอร์จะไม่ได้จัดส่ง (priority ต่ำจะถูกตัดก่อน)")

# --------------------------------------------------------------------------- #
# Step 2 — optimise
# --------------------------------------------------------------------------- #
st.subheader("2 · คำนวณเส้นทาง")
settings = SolverSettings(
    time_limit_s=int(time_limit),
    balance_coef=int(balance),
    minimize_vehicles=bool(min_vehicles),
    max_wait_min=int(max_wait),
)
sig = df_signature(
    orders, [vars(v) for v in vehicles], depot,
    [method, circuity, speed, osrm_url, traffic], vars(settings),
)

can_run = len(orders) > 0 and len(vehicles) > 0 and not fleet_errors
run = st.button("🚀 คำนวณเส้นทาง", type="primary", disabled=not can_run)
if not vehicles:
    st.error("เพิ่มรถอย่างน้อย 1 คันที่แถบซ้าย")

if run:
    coords = tuple([depot] + list(zip(orders["lat"].round(6), orders["lon"].round(6))))
    with st.status("กำลังคำนวณ…", expanded=False) as status:
        status.write("สร้างตารางระยะทาง/เวลา")
        tm = get_matrix(coords, method, float(circuity), float(speed), osrm_url, float(traffic))
        status.write(f"Optimize ({len(orders)} จุด, {len(vehicles)} คัน, ≤{time_limit} วินาที)")
        sol = solve_routes(orders, vehicles, tm.distance_m, tm.time_min, settings)
        baseline = evaluate_current_plan(orders, tm.distance_m, tm.time_min, vehicles[0])
        geoms = {}
        if tm.source == "osrm":
            status.write("ดึงเส้นทางถนนจริงสำหรับแผนที่")
            for i, r in enumerate(sol.routes):
                if r.order_idx:
                    g = get_geometry(tuple(route_points(r, depot)), osrm_url)
                    if g:
                        geoms[i] = g
        status.update(label=f"เสร็จแล้ว ({sol.solve_seconds:.1f} วินาที)", state="complete")
    ss.result = {
        "sig": sig, "solution": sol, "baseline": baseline, "orders": orders,
        "matrix_source": tm.source, "matrix_note": tm.note, "geoms": geoms,
        "depot": depot, "depot_name": depot_name,
    }

res = ss.get("result")
if not res:
    st.caption("กดปุ่มด้านบนเพื่อสร้างแผนเส้นทาง")
    st.subheader("ตำแหน่งออเดอร์")
    st_folium(build_route_map(None, orders, depot, depot_name), height=460,
              use_container_width=True, returned_objects=[], key="preview_map")
    st.stop()

if res["sig"] != sig:
    st.warning("ข้อมูลหรือการตั้งค่าเปลี่ยนไปหลังคำนวณครั้งล่าสุด — กด **คำนวณเส้นทาง** อีกครั้งเพื่ออัปเดต")

# --------------------------------------------------------------------------- #
# Step 3 — results
# --------------------------------------------------------------------------- #
sol = res["solution"]
baseline = res["baseline"]
r_orders = res["orders"]
r_depot = res["depot"]

st.subheader("3 · แผนเส้นทาง")
if res["matrix_note"]:
    st.warning(res["matrix_note"])
st.caption(
    "ระยะทาง: " + ("ถนนจริง (OSRM)" if res["matrix_source"] == "osrm" else "ประมาณการ (เส้นตรง × ตัวคูณ)")
    + f" · สถานะ solver: {sol.status}"
)

late_total = sum(r.late_stops for r in sol.routes)
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("จัดส่งได้", f"{sol.served} / {len(r_orders)}")
k2.metric("รถที่ใช้", f"{len(sol.used_routes)} / {len(sol.routes)}")
delta_km = None
if baseline and baseline.covered_orders == len(r_orders) and not sol.unassigned and baseline.total_km > 0:
    delta_km = f"{(sol.total_km - baseline.total_km) / baseline.total_km * 100:+.1f}% vs แผนเดิม"
k3.metric("ระยะทางรวม", f"{sol.total_km:,.1f} กม.", delta=delta_km, delta_color="inverse")
k4.metric("เวลาทำงานรวม", fmt_hm(sol.total_duration_min))
k5.metric("ค่าใช้จ่ายประมาณ", f"฿{sol.total_cost:,.0f}")

if baseline:
    b_note = (
        f"**เทียบกับแผนเดิม** ({len(baseline.routes)} เส้น, {baseline.covered_orders} จุด): "
        f"ระยะ {baseline.total_km:,.1f} → {sol.total_km:,.1f} กม. · "
        f"เวลาขับ {fmt_hm(baseline.total_drive_min)} → {fmt_hm(sol.total_drive_min)} · "
        f"ส่งสาย {baseline.late_stops} → {late_total} จุด"
    )
    if baseline.covered_orders != len(r_orders) or sol.unassigned:
        b_note += "  \n_หมายเหตุ: จำนวนจุดของสองแผนไม่เท่ากัน ตัวเลขจึงเทียบกันตรง ๆ ไม่ได้_"
    st.info(b_note)

for m in sol.messages:
    st.warning(m)

if sol.unassigned:
    with st.expander(f"❌ ไม่ได้จัดส่ง {len(sol.unassigned)} ออเดอร์", expanded=True):
        un = r_orders.iloc[sol.unassigned][
            ["order_id", "customer", "demand", "tw_start", "tw_end", "priority"]
        ].copy()
        un["tw_start"] = un["tw_start"].map(fmt_time)
        un["tw_end"] = un["tw_end"].map(fmt_time)
        st.dataframe(un, hide_index=True, width="stretch")
        st.caption("สาเหตุที่พบบ่อย: ความจุรถไม่พอ · ช่วงเวลารับของแคบ/อยู่นอกกะ · อยู่ไกลเกินจะกลับทันเลิกกะ "
                   "— ลองเพิ่มรถ ขยายกะ หรือเพิ่ม priority ของออเดอร์ที่ต้องส่ง")

st_folium(
    build_route_map(sol, r_orders, r_depot, res["depot_name"], res["geoms"]),
    height=560,
    use_container_width=True,
    returned_objects=[],
    key="route_map",
)

st.markdown("##### สรุปรายคัน")
st.dataframe(summary_table(sol), hide_index=True, width="stretch")

d1, d2 = st.columns(2)
d1.download_button(
    "⬇️ ดาวน์โหลด Excel (แยกชีตต่อคัน)",
    build_excel(sol, r_orders, r_depot, baseline),
    file_name="route_plan.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    type="primary",
    width="stretch",
    on_click="ignore",
)
d2.download_button(
    "⬇️ ดาวน์โหลด CSV",
    build_flat_csv(sol),
    file_name="route_plan.csv",
    mime="text/csv",
    width="stretch",
    on_click="ignore",
)

st.markdown("##### รายละเอียดแต่ละคัน")
for i, r in enumerate(sol.routes):
    if not r.order_idx:
        st.caption(f"{r.vehicle.name}: ไม่ได้ใช้งาน")
        continue
    dot = f":material/local_shipping: **{r.vehicle.name}**"
    title = (
        f"{dot} — {len(r.order_idx)} จุด · {r.distance_km:.1f} กม. · "
        f"{fmt_time(r.start)}–{fmt_time(r.end)} · โหลด {r.load}/{r.vehicle.capacity}"
        + (f" · ⚠️ สาย {r.late_stops}" if r.late_stops else "")
    )
    with st.expander(title):
        st.markdown(
            f"<span style='display:inline-block;width:14px;height:14px;border-radius:50%;"
            f"background:{route_color(i)};vertical-align:middle'></span> สีเส้นบนแผนที่",
            unsafe_allow_html=True,
        )
        st.dataframe(
            schedule_table(r),
            hide_index=True,
            width="stretch",
            column_config={"Google Maps": st.column_config.LinkColumn("Google Maps", display_text="เปิด")},
        )
        links = gmaps_route_urls(route_points(r, r_depot))
        st.markdown(
            "**นำทาง Google Maps:** "
            + " · ".join(f"[ช่วงที่ {k}]({u})" for k, u in enumerate(links, 1))
            + ("  \n_(Google Maps รับได้ครั้งละ 9 จุดแวะ จึงแบ่งเป็นช่วง)_" if len(links) > 1 else "")
        )
