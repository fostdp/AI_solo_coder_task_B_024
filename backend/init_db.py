import psycopg2
from psycopg2 import sql
import random
import os

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "fab_maintenance")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")

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


def get_connection(dbname=None):
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=dbname or DB_NAME,
        user=DB_USER,
        password=DB_PASS,
    )


def create_database():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname="postgres",
        user=DB_USER, password=DB_PASS,
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
    if not cur.fetchone():
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
        print(f"Database '{DB_NAME}' created.")
    else:
        print(f"Database '{DB_NAME}' already exists.")
    cur.close()
    conn.close()


def create_tables():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS factories (
            factory_id VARCHAR(32) PRIMARY KEY,
            factory_name VARCHAR(128),
            address VARCHAR(256),
            lat DOUBLE PRECISION,
            lng DOUBLE PRECISION,
            status VARCHAR(32) DEFAULT 'active'
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id VARCHAR(32) PRIMARY KEY,
            device_name VARCHAR(128),
            equipment_type VARCHAR(32),
            area VARCHAR(16),
            factory_id VARCHAR(32) REFERENCES factories(factory_id),
            engineer_id VARCHAR(32),
            baseline_temperature DOUBLE PRECISION,
            baseline_vibration DOUBLE PRECISION,
            baseline_rf_power DOUBLE PRECISION,
            status VARCHAR(32) DEFAULT 'online'
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sensor_data (
            time TIMESTAMPTZ NOT NULL,
            device_id VARCHAR(32) NOT NULL,
            temperature DOUBLE PRECISION,
            vibration DOUBLE PRECISION,
            rf_power DOUBLE PRECISION,
            health_score DOUBLE PRECISION
        );
    """)

    try:
        cur.execute("""
            SELECT create_hypertable('sensor_data', 'time',
                chunk_time_interval => INTERVAL '1 day',
                migrate_data => true
            );
        """)
    except psycopg2.errors.DuplicateTable:
        print("sensor_data already a hypertable, skipping.")

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_sensor_data_device_time
        ON sensor_data (device_id, time DESC);
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS work_orders (
            id SERIAL PRIMARY KEY,
            device_id VARCHAR(32) NOT NULL,
            factory_id VARCHAR(32) REFERENCES factories(factory_id),
            engineer_id VARCHAR(32),
            recommended_engineer_id VARCHAR(32),
            spare_part_id VARCHAR(32),
            reason TEXT,
            assignment_reason TEXT,
            status VARCHAR(32) DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            accepted_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            verified_at TIMESTAMPTZ
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS spare_parts (
            part_id VARCHAR(32) PRIMARY KEY,
            part_name VARCHAR(128),
            equipment_type VARCHAR(32),
            stock_quantity INTEGER DEFAULT 0,
            safe_stock_level INTEGER DEFAULT 5,
            unit_price DOUBLE PRECISION,
            supplier VARCHAR(128),
            version INTEGER DEFAULT 0
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id SERIAL PRIMARY KEY,
            part_id VARCHAR(32) REFERENCES spare_parts(part_id),
            factory_id VARCHAR(32) REFERENCES factories(factory_id),
            quantity INTEGER,
            reason TEXT,
            status VARCHAR(32) DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            approved_at TIMESTAMPTZ,
            received_at TIMESTAMPTZ
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id SERIAL PRIMARY KEY,
            device_id VARCHAR(32) NOT NULL,
            alert_type VARCHAR(32) NOT NULL,
            metric_value DOUBLE PRECISION,
            baseline_value DOUBLE PRECISION,
            deviation_pct DOUBLE PRECISION,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            status VARCHAR(32) DEFAULT 'active'
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS engineers (
            engineer_id VARCHAR(32) PRIMARY KEY,
            name VARCHAR(64),
            specialty VARCHAR(32),
            factory_id VARCHAR(32) REFERENCES factories(factory_id)
        );
    """)

    cur.execute("ALTER TABLE engineers ADD COLUMN IF NOT EXISTS factory_id VARCHAR(32);")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS life_predictions (
            device_id VARCHAR(32) PRIMARY KEY,
            remaining_days DOUBLE PRECISION,
            confidence DOUBLE PRECISION,
            slope DOUBLE PRECISION,
            status VARCHAR(32),
            predicted_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS health_score_history (
            time TIMESTAMPTZ NOT NULL,
            device_id VARCHAR(32) NOT NULL,
            health_score DOUBLE PRECISION
        );
    """)

    try:
        cur.execute("""
            SELECT create_hypertable('health_score_history', 'time',
                chunk_time_interval => INTERVAL '1 day',
                migrate_data => true
            );
        """)
    except psycopg2.errors.DuplicateTable:
        print("health_score_history already a hypertable, skipping.")

    try:
        cur.execute("""
            CREATE MATERIALIZED VIEW mv_factory_health_summary AS
            SELECT
                f.factory_id,
                f.factory_name,
                f.lat,
                f.lng,
                COUNT(d.device_id) as device_count,
                ROUND(AVG(s.health_score), 1) as avg_health,
                CASE
                    WHEN f.lat >= 30 THEN '华北地区'
                    WHEN f.lat >= 25 THEN '华东地区'
                    ELSE '华南地区'
                END as region,
                NOW() as last_updated
            FROM factories f
            LEFT JOIN devices d ON f.factory_id = d.factory_id
            LEFT JOIN (
                SELECT s1.device_id, s1.health_score
                FROM sensor_data s1
                INNER JOIN (
                    SELECT device_id, MAX(time) as max_time
                    FROM sensor_data
                    GROUP BY device_id
                ) s2 ON s1.device_id = s2.device_id AND s1.time = s2.max_time
            ) s ON d.device_id = s.device_id
            GROUP BY f.factory_id, f.lat, f.lng
            WITH DATA;
        """)
        cur.execute("CREATE UNIQUE INDEX idx_mv_factory ON mv_factory_health_summary(factory_id);")
        print("Materialized view mv_factory_health_summary created.")
    except Exception as e:
        print(f"Materialized view may already exist: {e}")

    print("All tables created (with TimescaleDB hypertables).")
    cur.close()
    conn.close()


def seed_factories():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM factories;")
    count = cur.fetchone()[0]
    if count >= 3:
        print("Factories already seeded.")
        cur.close()
        conn.close()
        return
    cur.executemany("""
        INSERT INTO factories (factory_id, factory_name, address, lat, lng, status)
        VALUES (%s, %s, %s, %s, %s, 'active')
        ON CONFLICT (factory_id) DO NOTHING;
    """, FACTORIES)
    print(f"Seeded {len(FACTORIES)} factories.")
    cur.close()
    conn.close()


def seed_spare_parts():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM spare_parts;")
    count = cur.fetchone()[0]
    if count >= 20:
        print("Spare parts already seeded.")
        cur.close()
        conn.close()
        return
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
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (part_id) DO NOTHING;
    """, parts)
    print(f"Seeded {len(parts)} spare parts.")
    cur.close()
    conn.close()


def seed_devices():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM devices;")
    count = cur.fetchone()[0]
    if count >= 200:
        print("Devices already seeded.")
        cur.close()
        conn.close()
        return

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
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (device_id) DO NOTHING;
    """, devices)

    print(f"Seeded {len(devices)} devices.")

    for eng in ENGINEERS:
        factory_idx = ENGINEERS.index(eng) % len(FACTORIES)
        cur.execute("""
            INSERT INTO engineers (engineer_id, name, specialty, factory_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (engineer_id) DO NOTHING;
        """, (eng["id"], eng["name"], eng["specialty"], FACTORIES[factory_idx][0]))

    print(f"Seeded {len(ENGINEERS)} engineers.")
    cur.close()
    conn.close()


def seed_dummy_sensor_data():
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM sensor_data;")
    count = cur.fetchone()[0]
    if count > 0:
        print(f"Sensor data already exists ({count} rows). Skipping.")
        cur.close()
        conn.close()
        return

    cur.execute("SELECT device_id, baseline_temperature, baseline_vibration, baseline_rf_power FROM devices;")
    devices = cur.fetchall()

    from datetime import datetime, timedelta
    now = datetime.utcnow()
    rows = []
    batch_size = 0

    for device_id, bt, bv, bp in devices:
        for minute_offset in range(0, 24 * 60, 5):
            ts = now - timedelta(minutes=minute_offset)
            temp = bt + random.gauss(0, bt * 0.05)
            vib = bv + random.gauss(0, bv * 0.08)
            pwr = bp + random.gauss(0, bp * 0.04)

            temp_dev = abs(temp - bt) / bt
            vib_dev = abs(vib - bv) / bv
            pwr_dev = abs(pwr - bp) / bp

            temp_score = max(0, 100 - temp_dev * 100)
            vib_score = max(0, 100 - vib_dev * 100)
            pwr_score = max(0, 100 - pwr_dev * 100)
            health = 0.3 * temp_score + 0.4 * vib_score + 0.3 * pwr_score

            rows.append((ts, device_id, temp, vib, pwr, round(health, 2)))
            batch_size += 1

            if batch_size >= 5000:
                cur.executemany("""
                    INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """, rows)
                rows = []
                batch_size = 0

    if rows:
        cur.executemany("""
            INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score)
            VALUES (%s, %s, %s, %s, %s, %s);
        """, rows)

    print(f"Seeded dummy sensor data for {len(devices)} devices (24h, 5min interval).")
    cur.close()
    conn.close()


def main():
    print("=== Fab Maintenance DB Initialization ===")
    create_database()
    create_tables()
    seed_factories()
    seed_spare_parts()
    seed_devices()
    seed_dummy_sensor_data()
    print("=== Initialization Complete ===")


if __name__ == "__main__":
    main()
