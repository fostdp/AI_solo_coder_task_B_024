import os
import sqlite3
import random
import json
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "demo.db")

EQUIPMENT_TYPES = [
    "CVD", "PVD", "ETCH", "LITHO", "IMPLANT",
    "CMP", "DIFF", "CLEAN", "METRO", "DEPO"
]

AREAS = ["A1", "A2", "A3", "B1", "B2", "B3"]

FACTORIES = [
    ("F-SH", "上海晶圆厂", "上海市浦东新区张江高科技园区", 31.2086, 121.5896),
    ("F-SZ", "深圳晶圆厂", "深圳市南山区科技园", 22.5431, 113.9413),
    ("F-BJ", "北京晶圆厂", "北京市亦庄经济技术开发区", 39.7817, 116.5047),
]

BASELINES = {
    "CVD":     {"temperature": 350, "vibration": 2.5, "rf_power": 500},
    "PVD":     {"temperature": 280, "vibration": 3.0, "rf_power": 800},
    "ETCH":    {"temperature": 180, "vibration": 2.0, "rf_power": 600},
    "LITHO":   {"temperature": 120, "vibration": 1.0, "rf_power": 300},
    "IMPLANT": {"temperature": 250, "vibration": 3.5, "rf_power": 700},
    "CMP":     {"temperature": 200, "vibration": 4.0, "rf_power": 200},
    "DIFF":    {"temperature": 400, "vibration": 1.5, "rf_power": 450},
    "CLEAN":   {"temperature": 150, "vibration": 1.8, "rf_power": 150},
    "METRO":   {"temperature": 100, "vibration": 0.5, "rf_power": 100},
    "DEPO":    {"temperature": 320, "vibration": 2.8, "rf_power": 550},
}

ENGINEERS = [
    {"id": f"ENG-{i+1:03d}", "name": name, "specialty": EQUIPMENT_TYPES[i % len(EQUIPMENT_TYPES)]}
    for i, name in enumerate([
        "张伟", "李强", "王芳", "刘洋", "陈磊",
        "赵敏", "周涛", "吴鹏", "郑华", "孙丽",
        "马超", "朱军", "胡明", "林峰", "何勇",
        "高健", "罗斌", "谢刚", "韩雪", "唐杰",
    ])
]


def compute_health_score(temperature, vibration, rf_power, bt, bv, bp):
    temp_dev = abs(temperature - bt) / bt if bt else 0
    vib_dev = abs(vibration - bv) / bv if bv else 0
    pwr_dev = abs(rf_power - bp) / bp if bp else 0
    temp_score = max(0, 100 - temp_dev * 100)
    vib_score = max(0, 100 - vib_dev * 100)
    pwr_score = max(0, 100 - pwr_dev * 100)
    return round(0.3 * temp_score + 0.4 * vib_score + 0.3 * pwr_score, 2)


def init():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("Removed old demo.db")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE factories (
            factory_id TEXT PRIMARY KEY,
            factory_name TEXT,
            address TEXT,
            lat REAL,
            lng REAL,
            status TEXT DEFAULT 'active'
        );
    """)

    cur.execute("""
        CREATE TABLE devices (
            device_id TEXT PRIMARY KEY,
            device_name TEXT,
            equipment_type TEXT,
            area TEXT,
            factory_id TEXT,
            engineer_id TEXT,
            baseline_temperature REAL,
            baseline_vibration REAL,
            baseline_rf_power REAL,
            status TEXT DEFAULT 'online'
        );
    """)

    cur.execute("""
        CREATE TABLE sensor_data (
            time TEXT NOT NULL,
            device_id TEXT NOT NULL,
            temperature REAL,
            vibration REAL,
            rf_power REAL,
            health_score REAL
        );
    """)

    cur.execute("CREATE INDEX idx_sd_device_time ON sensor_data(device_id, time);")

    cur.execute("""
        CREATE TABLE work_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            factory_id TEXT,
            engineer_id TEXT,
            recommended_engineer_id TEXT,
            spare_part_id TEXT,
            reason TEXT,
            assignment_reason TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            accepted_at TEXT,
            completed_at TEXT,
            verified_at TEXT
        );
    """)

    cur.execute("""
        CREATE TABLE spare_parts (
            part_id TEXT PRIMARY KEY,
            part_name TEXT,
            equipment_type TEXT,
            stock_quantity INTEGER DEFAULT 0,
            safe_stock_level INTEGER DEFAULT 5,
            unit_price REAL,
            supplier TEXT,
            version INTEGER DEFAULT 0
        );
    """)

    cur.execute("""
        CREATE TABLE purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_id TEXT,
            factory_id TEXT,
            quantity INTEGER,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            approved_at TEXT,
            received_at TEXT
        );
    """)

    cur.execute("""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            metric_value REAL,
            baseline_value REAL,
            deviation_pct REAL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT DEFAULT 'active'
        );
    """)

    cur.execute("""
        CREATE TABLE engineers (
            engineer_id TEXT PRIMARY KEY,
            name TEXT,
            specialty TEXT,
            factory_id TEXT
        );
    """)

    cur.execute("""
        CREATE TABLE health_score_history (
            time TEXT NOT NULL,
            device_id TEXT NOT NULL,
            health_score REAL
        );
    """)

    cur.execute("""
        CREATE TABLE life_predictions (
            device_id TEXT PRIMARY KEY,
            remaining_days REAL,
            confidence REAL,
            slope REAL,
            status TEXT,
            predicted_at TEXT DEFAULT (datetime('now'))
        );
    """)

    print("Tables created.")

    cur.executemany("""
        INSERT INTO factories (factory_id, factory_name, address, lat, lng, status)
        VALUES (?, ?, ?, ?, ?, 'active');
    """, FACTORIES)
    print(f"Seeded {len(FACTORIES)} factories.")

    parts = [
        ("SP-CVD-001", "CVD反应室密封圈", "CVD", 15, 10, 2500.00, "Applied Materials"),
        ("SP-CVD-002", "CVD加热组件", "CVD", 5, 8, 15000.00, "Applied Materials"),
        ("SP-PVD-001", "PVD靶材组件", "PVD", 8, 6, 8000.00, "Lam Research"),
        ("SP-PVD-002", "PVD磁控管", "PVD", 4, 5, 12000.00, "Lam Research"),
        ("SP-ETCH-001", "ETCH电极组件", "ETCH", 6, 5, 18000.00, "Tokyo Electron"),
        ("SP-ETCH-002", "ETCH气体喷头", "ETCH", 10, 8, 3500.00, "Tokyo Electron"),
        ("SP-LITHO-001", "LITHO投影镜头", "LITHO", 2, 3, 250000.00, "ASML"),
        ("SP-LITHO-002", "LITHO光源模块", "LITHO", 3, 2, 80000.00, "ASML"),
        ("SP-IMPLANT-001", "IMPLANT离子源", "IMPLANT", 4, 4, 35000.00, "Varian"),
        ("SP-IMPLANT-002", "IMPLANT晶圆夹具", "IMPLANT", 8, 6, 5000.00, "Varian"),
        ("SP-CMP-001", "CMP抛光垫", "CMP", 20, 15, 1200.00, "Cabot Micro"),
        ("SP-CMP-002", "CMP修整器", "CMP", 10, 8, 6000.00, "Cabot Micro"),
        ("SP-DIFF-001", "DIFF石英管", "DIFF", 3, 4, 12000.00, "Tokyo Electron"),
        ("SP-DIFF-002", "DIFF热电偶组", "DIFF", 8, 6, 800.00, "Tokyo Electron"),
        ("SP-CLEAN-001", "CLEAN超声波振子", "CLEAN", 12, 10, 2000.00, "Screen Holdings"),
        ("SP-CLEAN-002", "CLEAN化学喷嘴", "CLEAN", 15, 10, 500.00, "Screen Holdings"),
        ("SP-METRO-001", "METRO光学传感器", "METRO", 8, 6, 15000.00, "KLA-Tencor"),
        ("SP-METRO-002", "METRO探针卡", "METRO", 10, 8, 3000.00, "KLA-Tencor"),
        ("SP-DEPO-001", "DEPO前驱体气瓶", "DEPO", 6, 5, 8000.00, "Applied Materials"),
        ("SP-DEPO-002", "DEPO衬底加热器", "DEPO", 4, 4, 22000.00, "Applied Materials"),
    ]
    cur.executemany("""
        INSERT INTO spare_parts (part_id, part_name, equipment_type, stock_quantity, safe_stock_level, unit_price, supplier)
        VALUES (?, ?, ?, ?, ?, ?, ?);
    """, parts)
    print(f"Seeded {len(parts)} spare parts.")

    devices = []
    for i in range(200):
        eq_type = EQUIPMENT_TYPES[i % len(EQUIPMENT_TYPES)]
        area = AREAS[i % len(AREAS)]
        baseline = BASELINES[eq_type]
        device_id = f"EQ-{i+1:03d}"
        device_name = f"{eq_type}-{area}-{i+1:03d}"
        eng_idx = i % len(ENGINEERS)
        factory_idx = i % len(FACTORIES)
        devices.append((
            device_id, device_name, eq_type, area, FACTORIES[factory_idx][0],
            ENGINEERS[eng_idx]["id"],
            baseline["temperature"],
            baseline["vibration"],
            baseline["rf_power"],
            "online"
        ))

    cur.executemany("""
        INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id,
            engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, devices)
    print(f"Seeded {len(devices)} devices.")

    for eng in ENGINEERS:
        factory_idx = ENGINEERS.index(eng) % len(FACTORIES)
        cur.execute("""
            INSERT INTO engineers (engineer_id, name, specialty, factory_id)
            VALUES (?, ?, ?, ?);
        """, (eng["id"], eng["name"], eng["specialty"], FACTORIES[factory_idx][0]))
    print(f"Seeded {len(ENGINEERS)} engineers.")

    now = datetime.utcnow()
    rows = []
    batch = []

    for i in range(200):
        eq_type = EQUIPMENT_TYPES[i % len(EQUIPMENT_TYPES)]
        baseline = BASELINES[eq_type]
        bt, bv, bp = baseline["temperature"], baseline["vibration"], baseline["rf_power"]
        device_id = f"EQ-{i+1:03d}"

        degrading = random.random() < 0.08
        degradation = 1.0

        for minute_offset in range(0, 24 * 60, 5):
            ts = now - timedelta(minutes=minute_offset)
            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S")

            if degrading:
                degradation += 0.001

            anomaly = random.random() < 0.008
            if anomaly:
                temp = bt * (1 + random.uniform(0.2, 0.5) * degradation)
                vib = bv * (1 + random.uniform(0.3, 0.8) * degradation)
                pwr = bp * (1 + random.uniform(-0.3, 0.3))
            else:
                temp = bt + random.gauss(0, bt * 0.05) * degradation
                vib = bv + random.gauss(0, bv * 0.08) * degradation
                pwr = bp + random.gauss(0, bp * 0.04)

            temp = max(bt * 0.5, min(bt * 2.0, temp))
            vib = max(bv * 0.2, min(bv * 3.0, vib))
            pwr = max(bp * 0.3, min(bp * 1.8, pwr))

            health = compute_health_score(temp, vib, pwr, bt, bv, bp)

            batch.append((ts_str, device_id, round(temp, 2), round(vib, 4), round(pwr, 2), health))

            if len(batch) >= 5000:
                cur.executemany("""
                    INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, batch)
                batch = []

    if batch:
        cur.executemany("""
            INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score)
            VALUES (?, ?, ?, ?, ?, ?);
        """, batch)

    print("Seeded sensor data (200 devices, 24h, 5min interval).")

    low_devices = random.sample(range(200), 5)
    for dev_idx in low_devices:
        device_id = f"EQ-{dev_idx+1:03d}"
        eq_type = EQUIPMENT_TYPES[dev_idx % len(EQUIPMENT_TYPES)]
        eng = ENGINEERS[dev_idx % len(ENGINEERS)]
        factory_idx = dev_idx % len(FACTORIES)
        cur.execute("""
            INSERT INTO work_orders (device_id, factory_id, engineer_id, reason, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?);
        """, (device_id, FACTORIES[factory_idx][0], eng["id"],
              f"设备健康评分连续1小时低于60分，自动生成维保工单",
              (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")))

        cur.execute("""
            INSERT INTO alerts (device_id, alert_type, metric_value, baseline_value, deviation_pct, started_at, status)
            VALUES (?, 'overheat', ?, ?, ?, ?, 'active');
        """, (device_id,
              BASELINES[eq_type]["temperature"] * 1.4,
              BASELINES[eq_type]["temperature"],
              40.0,
              (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")))

    print("Seeded sample work orders and alerts.")

    conn.commit()
    conn.close()
    print(f"\nDemo database created: {DB_PATH}")
    print("Run: python demo_server.py")


if __name__ == "__main__":
    init()
