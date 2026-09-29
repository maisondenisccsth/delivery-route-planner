"""Loading, normalising and validating order files (CSV / Excel)."""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, time

import pandas as pd

from .geo import decode_plus_code, haversine_km, is_valid_plus_code

# Canonical column -> accepted aliases (compared lower-case, spaces/dashes -> "_")
COLUMN_ALIASES: dict[str, list[str]] = {
    "order_id": ["order_id", "orderno", "order_no", "order", "id", "เลขที่ออเดอร์", "เลขออเดอร์"],
    "customer": ["customer", "customer_name", "name", "shipto", "ship_to", "ชื่อลูกค้า", "ลูกค้า"],
    "address": ["address", "addr", "ที่อยู่"],
    "lat": ["lat", "latitude", "y", "ละติจูด"],
    "lon": ["lon", "lng", "long", "longitude", "x", "ลองจิจูด"],
    "plus_code": ["plus_code", "pluscode", "plus", "olc", "พลัสโค้ด"],
    "demand": ["demand", "qty", "quantity", "load", "crates", "volume", "จำนวน"],
    "tw_start": ["tw_start", "time_from", "window_start", "open", "earliest", "เวลาเริ่ม"],
    "tw_end": ["tw_end", "time_to", "window_end", "close", "latest", "เวลาสิ้นสุด"],
    "service_min": ["service_min", "service", "service_time", "unload_min", "เวลาลงของ"],
    "priority": ["priority", "prio", "ความสำคัญ"],
    "current_route": ["current_route", "route", "manual_route", "เส้นทางเดิม"],
    "current_seq": ["current_seq", "seq", "sequence", "stop_no", "ลำดับเดิม"],
    "note": ["note", "notes", "remark", "หมายเหตุ"],
}

TEMPLATE_COLUMNS = [
    "order_id", "customer", "address", "lat", "lon", "plus_code", "demand",
    "tw_start", "tw_end", "service_min", "priority", "current_route", "current_seq", "note",
]

MINUTES_PER_DAY = 24 * 60


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def parse_time(value, default: int | None = None) -> int | None:
    """Convert many time representations to minutes after midnight.

    Accepts ``"08:30"``, ``"8.30"``, ``"0830"``, ``datetime.time``, pandas
    timestamps, Excel day fractions (``0.354``) and plain minute counts (``510``).
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default
    if isinstance(value, str) and not value.strip():
        return default
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.hour * 60 + value.minute
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    if isinstance(value, (int, float)):
        v = float(value)
        if 0 <= v < 1:  # Excel fraction of a day
            return int(round(v * MINUTES_PER_DAY))
        if 1 <= v <= 24 and v == int(v):  # "8" meaning 08:00
            return int(v) * 60
        if 0 <= v <= MINUTES_PER_DAY:
            return int(round(v))
        raise ValueError(f"time out of range: {value}")
    s = str(value).strip()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?::\d{2})?\s*(am|pm)?", s, re.IGNORECASE)
    if not m:
        m2 = re.fullmatch(r"(\d{1,2})(\d{2})", s)
        if m2:
            m = m2
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        ampm = m.group(3) if m.re.groups >= 3 else None
        if ampm:
            if ampm.lower() == "pm" and h < 12:
                h += 12
            if ampm.lower() == "am" and h == 12:
                h = 0
        if 0 <= h <= 24 and 0 <= mi < 60 and h * 60 + mi <= MINUTES_PER_DAY:
            return h * 60 + mi
    if re.fullmatch(r"\d{1,2}", s):
        return int(s) * 60
    raise ValueError(f"cannot read time {value!r}")


def fmt_time(minutes: float | int | None) -> str:
    if minutes is None or (isinstance(minutes, float) and math.isnan(minutes)):
        return ""
    minutes = int(round(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --------------------------------------------------------------------------- #
# File reading
# --------------------------------------------------------------------------- #
def _norm(col: str) -> str:
    return re.sub(r"[\s\-]+", "_", str(col).strip().lower())


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename recognised alias columns to canonical names; keep the rest."""
    lookup = {alias: canon for canon, aliases in COLUMN_ALIASES.items() for alias in aliases}
    rename, used = {}, set()
    for col in df.columns:
        canon = lookup.get(_norm(col))
        if canon and canon not in used:
            rename[col] = canon
            used.add(canon)
    return df.rename(columns=rename)


def read_orders_file(file_name: str, data: bytes) -> pd.DataFrame:
    name = file_name.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        df = pd.read_excel(io.BytesIO(data))
    else:
        text = None
        for enc in ("utf-8-sig", "cp874", "tis-620", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    df = df.dropna(how="all")
    return normalise_columns(df)


def template_dataframe() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["SO-0001", "Hotel Example Silom", "Silom Rd, Bang Rak", 13.7262, 100.5290, "",
             12, "08:00", "11:00", 15, 2, "R3", 1, "ส่งทางเข้าหลังร้าน"],
            ["SO-0002", "Cafe Example Ari", "Phahonyothin 7", "", "", "7P52QGFV+X5",
             4, "09:00", "16:00", 10, 1, "R5", 1, "ใช้ Plus code แทนพิกัดได้"],
        ],
        columns=TEMPLATE_COLUMNS,
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@dataclass
class ValidationResult:
    orders: pd.DataFrame  # clean, solver-ready rows
    rejected: pd.DataFrame  # rows that cannot be routed, with a reason
    warnings: list[str] = field(default_factory=list)


def validate_orders(
    raw: pd.DataFrame,
    depot_lat: float,
    depot_lon: float,
    default_service_min: int = 15,
    default_tw_start: int = 7 * 60,
    default_tw_end: int = 18 * 60,
    max_km_from_depot: float = 150.0,
) -> ValidationResult:
    df = normalise_columns(raw.copy()).reset_index(drop=True)
    warnings: list[str] = []

    if "order_id" not in df.columns:
        df["order_id"] = [f"ORD-{i + 1:04d}" for i in range(len(df))]
        warnings.append("ไม่พบคอลัมน์ order_id — สร้างรหัสอัตโนมัติให้แล้ว")
    for col in TEMPLATE_COLUMNS:
        if col not in df.columns:
            df[col] = None

    df["order_id"] = df["order_id"].astype(str).str.strip()
    dup = df["order_id"].duplicated(keep=False) & df["order_id"].ne("")
    if dup.any():
        warnings.append(
            f"order_id ซ้ำ {int(dup.sum())} แถว — เติมเลขต่อท้ายให้ไม่ซ้ำกัน"
        )
        counts: dict[str, int] = {}
        new_ids = []
        for oid, is_dup in zip(df["order_id"], dup):
            if is_dup:
                counts[oid] = counts.get(oid, 0) + 1
                new_ids.append(f"{oid}-{counts[oid]}")
            else:
                new_ids.append(oid)
        df["order_id"] = new_ids

    good_rows, bad_rows = [], []
    decoded_pc = 0
    for idx, row in df.iterrows():
        reasons = []
        rec = row.to_dict()

        # --- coordinates ------------------------------------------------------
        lat = pd.to_numeric(rec.get("lat"), errors="coerce")
        lon = pd.to_numeric(rec.get("lon"), errors="coerce")
        if (pd.isna(lat) or pd.isna(lon)) and isinstance(rec.get("plus_code"), str):
            pc = rec["plus_code"].strip()
            if pc and is_valid_plus_code(pc):
                try:
                    lat, lon = decode_plus_code(pc, depot_lat, depot_lon)
                    decoded_pc += 1
                except ValueError as exc:
                    reasons.append(str(exc))
            elif pc:
                reasons.append(f"Plus code ไม่ถูกต้อง: {pc}")
        if pd.isna(lat) or pd.isna(lon):
            reasons.append("ไม่มีพิกัด (lat/lon หรือ plus_code)")
        elif not (-90 <= lat <= 90 and -180 <= lon <= 180):
            reasons.append(f"พิกัดผิดรูปแบบ ({lat}, {lon})")
        else:
            dist = haversine_km(depot_lat, depot_lon, lat, lon)
            if dist > max_km_from_depot:
                reasons.append(
                    f"อยู่ห่างคลัง {dist:,.0f} กม. (เกิน {max_km_from_depot:.0f}) — lat/lon สลับกันหรือเปล่า?"
                )
        rec["lat"], rec["lon"] = lat, lon

        # --- numbers ----------------------------------------------------------
        demand = pd.to_numeric(rec.get("demand"), errors="coerce")
        rec["demand"] = 1 if pd.isna(demand) else max(0, int(math.ceil(demand)))
        svc = pd.to_numeric(rec.get("service_min"), errors="coerce")
        rec["service_min"] = default_service_min if pd.isna(svc) else max(0, int(round(svc)))
        prio = pd.to_numeric(rec.get("priority"), errors="coerce")
        rec["priority"] = 1 if pd.isna(prio) else int(min(max(prio, 1), 3))

        # --- time window ------------------------------------------------------
        try:
            tws = parse_time(rec.get("tw_start"), default_tw_start)
            twe = parse_time(rec.get("tw_end"), default_tw_end)
            if twe < tws:
                reasons.append(f"เวลาสิ้นสุด {fmt_time(twe)} ก่อนเวลาเริ่ม {fmt_time(tws)}")
            rec["tw_start"], rec["tw_end"] = tws, twe
        except ValueError as exc:
            reasons.append(f"อ่านเวลาไม่ได้: {exc}")

        for text_col in ("customer", "address", "note", "current_route"):
            v = rec.get(text_col)
            rec[text_col] = "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)
        seq = pd.to_numeric(rec.get("current_seq"), errors="coerce")
        rec["current_seq"] = None if pd.isna(seq) else float(seq)
        rec["row"] = idx + 2  # spreadsheet-style row number (header = 1)

        if reasons:
            rec["reason"] = "; ".join(reasons)
            bad_rows.append(rec)
        else:
            good_rows.append(rec)

    if decoded_pc:
        warnings.append(f"แปลง Plus code เป็นพิกัดแล้ว {decoded_pc} แถว")

    cols = ["row"] + TEMPLATE_COLUMNS
    orders = pd.DataFrame(good_rows, columns=cols) if good_rows else pd.DataFrame(columns=cols)
    rejected = (
        pd.DataFrame(bad_rows, columns=cols + ["reason"])
        if bad_rows
        else pd.DataFrame(columns=cols + ["reason"])
    )
    if not orders.empty:
        orders["lat"] = orders["lat"].astype(float)
        orders["lon"] = orders["lon"].astype(float)
        orders["tw_start"] = orders["tw_start"].astype(int)
        orders["tw_end"] = orders["tw_end"].astype(int)
    return ValidationResult(orders=orders.reset_index(drop=True), rejected=rejected, warnings=warnings)
