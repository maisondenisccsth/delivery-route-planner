# 🚚 Delivery Route Planner

เว็บแอปวางแผนเส้นทางจัดส่งหลายคันรถ (Vehicle Routing) สร้างด้วย **Streamlit** + **Google OR-Tools**
อัปโหลดรายการออเดอร์ → กดคำนวณ → ได้เส้นทางของแต่ละคัน แผนที่ ตารางเวลา ลิงก์นำทาง Google Maps และไฟล์ Excel สำหรับคนขับ

> โปรเจกต์นี้เป็นเครื่องมือโอเพนซอร์สอิสระ ไม่มีส่วนเกี่ยวข้องกับ OptimoRoute หรือซอฟต์แวร์เชิงพาณิชย์รายใด

## ความสามารถ

- **Optimize หลายคัน** พร้อมเงื่อนไข: ความจุรถ, ช่วงเวลารับของของลูกค้า (time window), เวลาลงของต่อจุด, กะคนขับ (ออก–กลับคลังในกะ)
- **Priority 1–3** — ถ้ารถไม่พอ ระบบตัดออเดอร์ priority ต่ำก่อน แทนที่จะคำนวณไม่ออก
- **เกลี่ยงาน** ให้คนขับเท่า ๆ กัน หรือ **ใช้รถให้น้อยคันที่สุด**
- **ระยะทาง 2 แบบ**: ประมาณการแบบออฟไลน์ (ไม่ต้องใช้ API key) หรือระยะถนนจริงจาก OSRM (มี fallback อัตโนมัติ)
- **รองรับ Plus code** ทั้งแบบเต็ม (`7P52QGFV+X5`) และแบบสั้น (`QGFV+X5 Bangkok`) — ถอดรหัสในตัว ไม่ต้องติดตั้งเพิ่ม
- **เทียบกับแผนเดิม** — ใส่คอลัมน์ `current_route` + `current_seq` เพื่อดูว่าประหยัดระยะ/เวลาได้กี่ %
- **แผนที่ interactive** (ลำดับจุด, สีตามคัน, จุดที่ส่งสาย/ไม่ได้ส่ง)
- **Export**: Excel แยกชีตต่อคัน (มีลิงก์ Google Maps), CSV, ลิงก์นำทาง Google Maps ทีละช่วง (≤ 9 จุดแวะต่อลิงก์)
- ตรวจข้อมูลอัตโนมัติ: พิกัดหาย, lat/lon สลับกัน, เวลาผิด, order_id ซ้ำ
- จับคู่ชื่อคอลัมน์อัตโนมัติ (เช่น `OrderNo`, `Latitude`, `lng`, `Qty`, `ชื่อลูกค้า`)

## ลองรันบนเครื่อง

ต้องใช้ Python 3.10–3.12

```bash
git clone https://github.com/<your-username>/delivery-route-planner.git
cd delivery-route-planner
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

เปิด http://localhost:8501 แล้วกด **ใช้ข้อมูลตัวอย่าง**

รันเทสต์: `pip install pytest && pytest -q`

## อัปขึ้น GitHub

```bash
cd delivery-route-planner
git init
git add .
git commit -m "Initial commit: delivery route planner"
git branch -M main
git remote add origin https://github.com/<your-username>/delivery-route-planner.git
git push -u origin main
```

(สร้าง repo เปล่าชื่อ `delivery-route-planner` บน github.com ก่อน โดย **ไม่ต้อง** ติ๊กสร้าง README)

GitHub Actions จะรันเทสต์ให้อัตโนมัติทุกครั้งที่ push (ดูที่แท็บ **Actions**)

## Deploy บน Streamlit Community Cloud (ฟรี)

1. ไปที่ [share.streamlit.io](https://share.streamlit.io) แล้วล็อกอินด้วย GitHub
2. กด **Create app** → **Deploy a public app from GitHub**
3. เลือก Repository: `<your-username>/delivery-route-planner`, Branch: `main`, Main file path: `app.py`
4. เปิด **Advanced settings** → เลือก Python **3.12**
5. กด **Deploy** — รอติดตั้งแพ็กเกจราว 2–4 นาที จะได้ลิงก์ `https://<ชื่อแอป>.streamlit.app`

หลังจากนั้นทุกครั้งที่ `git push` แอปจะอัปเดตเองอัตโนมัติ

## รูปแบบไฟล์ออเดอร์

CSV (UTF-8 หรือ TIS-620) หรือ Excel หนึ่งแถวต่อหนึ่งจุดส่ง ดาวน์โหลด template ได้ในแอป

| คอลัมน์ | จำเป็น | ตัวอย่าง | ความหมาย |
|---|---|---|---|
| `order_id` | ✔ | `SO-1001` | รหัสออเดอร์ |
| `lat`, `lon` | ✔* | `13.7262`, `100.5290` | พิกัด (*หรือใช้ `plus_code` แทน) |
| `plus_code` | | `QGFV+X5` | Plus code (แบบสั้นจะอ้างอิงจากตำแหน่งคลัง) |
| `customer`, `address` | | | ใช้แสดงผล |
| `demand` | | `12` | ปริมาณสินค้า — หน่วยเดียวกับความจุรถ |
| `tw_start`, `tw_end` | | `08:00`, `11:00` | ช่วงเวลาที่ลูกค้ารับของ |
| `service_min` | | `15` | เวลาลงของ (นาที) |
| `priority` | | `1`–`3` | 3 = ต้องส่ง |
| `current_route`, `current_seq` | | `Truck 1`, `4` | แผนเดิม ใช้เปรียบเทียบ |
| `note` | | | หมายเหตุถึงคนขับ |

## ทำงานอย่างไร

- สร้างตารางระยะทาง/เวลาระหว่างทุกคู่จุด
  - *ประมาณการ*: ระยะเส้นตรง (haversine) × ตัวคูณความคดเคี้ยว (ค่าเริ่มต้น 1.35) ÷ ความเร็วเฉลี่ย (ค่าเริ่มต้น 22 กม./ชม.)
  - *OSRM*: ระยะถนนจริง × ตัวคูณรถติด (OSRM ไม่รู้สภาพจราจร)
- แก้ปัญหา **CVRPTW** (Capacitated VRP with Time Windows) ด้วย OR-Tools routing solver: หาคำตอบเริ่มต้นแบบ *path cheapest arc* แล้วปรับปรุงด้วย *guided local search* จนหมดเวลาที่กำหนด
- เป้าหมายคือระยะทางรวมต่ำที่สุด ออเดอร์ที่ใส่ไม่ลงจะถูก "ตัด" พร้อมค่าปรับ (ตาม priority) แทนการทำให้ทั้งปัญหาไม่มีคำตอบ

### ปรับค่าให้ตรงกับของจริง

ค่าประมาณการจะแม่นขึ้นมากถ้าปรับ **ตัวคูณความคดเคี้ยว** และ **ความเร็วเฉลี่ย** จากข้อมูลจริง เช่น เทียบระยะที่แอปคำนวณกับเลขไมล์/GPS ของเส้นทางจริงสัก 5–10 วัน แล้วปรับจนใกล้เคียง

## ข้อจำกัด

- เซิร์ฟเวอร์ OSRM สาธารณะ (`router.project-osrm.org`) เป็น demo — จำกัด ≤ 100 จุดต่อครั้ง และไม่เหมาะกับการใช้งานจริงจำนวนมาก ถ้าจะใช้จริงควรตั้ง OSRM ของตัวเอง (Docker + ไฟล์แผนที่ประเทศไทยจาก Geofabrik) แล้วใส่ URL ในแถบตั้งค่า
- คลังเดียว (single depot) และวางแผนวันต่อวัน
- ยังไม่มี live tracking / แอปคนขับ / proof of delivery
- ตัวอย่างใน `sample_data/` เป็นข้อมูลสมมติ และ "แผนเดิม" ในตัวอย่างจงใจทำแบบง่าย ๆ (แบ่งตะวันตก/ตะวันออก เรียงตามลำดับที่รับออเดอร์) ตัวเลขประหยัดในตัวอย่างจึงสูงกว่าที่มักเจอกับแผนจริง

## ความเป็นส่วนตัวของข้อมูล

- **อย่า commit ข้อมูลลูกค้าจริงเข้า repo สาธารณะ** — `.gitignore` กันไฟล์ `.xlsx` และโฟลเดอร์ `data/` ไว้แล้ว
- ไฟล์ที่อัปโหลดเข้าแอปจะอยู่ในหน่วยความจำของ session นั้นเท่านั้น ไม่ถูกบันทึกลงดิสก์
- ถ้าจะใช้กับข้อมูลบริษัท แนะนำตั้งแอปเป็น **private** (Streamlit Cloud → ⋮ → Settings → Sharing) แล้วเชิญเฉพาะอีเมลที่เกี่ยวข้อง
- เมื่อเลือกโหมด OSRM พิกัดจะถูกส่งไปยังเซิร์ฟเวอร์ OSRM ที่ตั้งไว้

## โครงสร้างโปรเจกต์

```
delivery-route-planner/
├── app.py                  # Streamlit UI
├── planner/
│   ├── data.py             # อ่านไฟล์ จับคู่คอลัมน์ ตรวจข้อมูล แปลงเวลา
│   ├── geo.py              # ระยะทาง/เวลา, OSRM, ถอดรหัส Plus code
│   ├── solver.py           # OR-Tools CVRPTW + เทียบแผนเดิม
│   ├── export.py           # Excel / CSV / ลิงก์ Google Maps
│   └── viz.py              # แผนที่ Folium
├── sample_data/orders_sample.csv
├── tests/test_planner.py
├── .streamlit/config.toml
├── .github/workflows/tests.yml
└── requirements.txt
```

## License

MIT — ดู [LICENSE](LICENSE)
