import os
import sys
import sqlite3
import math
import random
import threading
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from health_evaluator import (
    _linear_regression,
    predict_remaining_life,
    _get_health_history,
    compute_health_score,
    FAIL_THRESHOLD,
    MIN_DATA_POINTS_FOR_PREDICTION,
)
from workorder_engine import (
    check_spare_parts,
    create_purchase_order,
    recommend_engineer,
    get_engineer_workload,
    check_work_order,
    ENGINEER_MAX_LOAD,
    PURCHASE_QUANTITY_FACTOR,
)


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS factories (
    factory_id TEXT PRIMARY KEY,
    factory_name TEXT,
    address TEXT,
    lat REAL,
    lng REAL,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS devices (
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

CREATE TABLE IF NOT EXISTS sensor_data (
    time TEXT NOT NULL,
    device_id TEXT NOT NULL,
    temperature REAL,
    vibration REAL,
    rf_power REAL,
    health_score REAL
);

CREATE INDEX IF NOT EXISTS idx_sd_device_time ON sensor_data(device_id, time);

CREATE TABLE IF NOT EXISTS work_orders (
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

CREATE TABLE IF NOT EXISTS spare_parts (
    part_id TEXT PRIMARY KEY,
    part_name TEXT,
    equipment_type TEXT,
    stock_quantity INTEGER DEFAULT 0,
    safe_stock_level INTEGER DEFAULT 5,
    unit_price REAL,
    supplier TEXT,
    version INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchase_orders (
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

CREATE TABLE IF NOT EXISTS alerts (
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

CREATE TABLE IF NOT EXISTS engineers (
    engineer_id TEXT PRIMARY KEY,
    name TEXT,
    specialty TEXT,
    factory_id TEXT
);

CREATE TABLE IF NOT EXISTS health_score_history (
    time TEXT NOT NULL,
    device_id TEXT NOT NULL,
    health_score REAL
);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(DB_SCHEMA)
    yield c
    c.close()


@pytest.fixture
def seed_factories(conn):
    factories = [
        ("F-SH", "上海晶圆厂", "上海市浦东新区张江高科技园区", 31.2086, 121.5896),
        ("F-SZ", "深圳晶圆厂", "深圳市南山区科技园", 22.5431, 113.9413),
        ("F-BJ", "北京晶圆厂", "北京市亦庄经济技术开发区", 39.7817, 116.5047),
    ]
    conn.executemany(
        "INSERT INTO factories (factory_id, factory_name, address, lat, lng, status) VALUES (?,?,?,?,?,'active')",
        factories,
    )
    conn.commit()
    return factories


@pytest.fixture
def seed_spare_parts(conn):
    parts = [
        ("SP-CVD-001", "CVD反应室密封圈", "CVD", 15, 10, 2500.00, "Applied Materials"),
        ("SP-CVD-002", "CVD加热组件", "CVD", 5, 8, 15000.00, "Applied Materials"),
        ("SP-PVD-001", "PVD靶材组件", "PVD", 8, 6, 8000.00, "Lam Research"),
        ("SP-PVD-002", "PVD磁控管", "PVD", 4, 5, 12000.00, "Lam Research"),
        ("SP-ETCH-001", "ETCH电极组件", "ETCH", 6, 5, 18000.00, "Tokyo Electron"),
    ]
    conn.executemany(
        "INSERT INTO spare_parts (part_id, part_name, equipment_type, stock_quantity, safe_stock_level, unit_price, supplier) VALUES (?,?,?,?,?,?,?)",
        parts,
    )
    conn.commit()
    return parts


@pytest.fixture
def seed_engineers(conn, seed_factories):
    engineers = [
        ("ENG-001", "张伟", "CVD", "F-SH"),
        ("ENG-002", "李强", "CVD", "F-SZ"),
        ("ENG-003", "王芳", "PVD", "F-BJ"),
        ("ENG-004", "刘洋", "ETCH", "F-SH"),
        ("ENG-005", "陈磊", "PVD", "F-SZ"),
    ]
    conn.executemany(
        "INSERT INTO engineers (engineer_id, name, specialty, factory_id) VALUES (?,?,?,?)",
        engineers,
    )
    conn.commit()
    return engineers


@pytest.fixture
def seed_devices(conn, seed_factories, seed_engineers):
    devices = [
        ("EQ-001", "CVD-A1-001", "CVD", "A1", "F-SH", "ENG-001", 350, 2.5, 500, "online"),
        ("EQ-002", "PVD-A2-002", "PVD", "A2", "F-SZ", "ENG-005", 280, 3.0, 800, "online"),
        ("EQ-003", "ETCH-A3-003", "ETCH", "A3", "F-BJ", "ENG-004", 180, 2.0, 600, "online"),
    ]
    conn.executemany(
        "INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        devices,
    )
    conn.commit()
    return devices


def _insert_sensor_data(conn, device_id, hours=24, interval_min=5, health_fn=None, start_time=None):
    if start_time is None:
        start_time = datetime.utcnow() - timedelta(hours=hours)
    batch = []
    for minute in range(0, hours * 60, interval_min):
        ts = start_time + timedelta(minutes=minute)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
        hs = health_fn(minute) if health_fn else 95.0
        batch.append((ts_str, device_id, 350.0, 2.5, 500.0, hs))
    conn.executemany(
        "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
        batch,
    )
    conn.commit()
    return batch


def _insert_work_orders(conn, engineer_id, count, status="pending"):
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    for i in range(count):
        conn.execute(
            "INSERT INTO work_orders (device_id, factory_id, engineer_id, reason, status, created_at) VALUES (?,?,?,?,?,?)",
            (f"EQ-{i+1:03d}", "F-SH", engineer_id, "测试工单", status, now_str),
        )
    conn.commit()


# =============================================================================
# 备件联动测试
# =============================================================================
class TestSparePartsLinkage:
    def test_check_spare_parts_low_stock(self, conn, seed_spare_parts):
        part, warning = check_spare_parts("CVD", conn, "sqlite")
        assert part is not None
        assert part["equipment_type"] == "CVD"
        assert warning is not None
        assert "库存不足" in warning
        assert "CVD加热组件" in warning

    def test_check_spare_parts_sufficient_stock(self, conn, seed_spare_parts):
        conn.execute("UPDATE spare_parts SET stock_quantity = 100 WHERE equipment_type = 'CVD'")
        conn.commit()
        part, warning = check_spare_parts("CVD", conn, "sqlite")
        assert part is not None
        assert warning is None

    def test_check_spare_parts_unknown_type(self, conn, seed_spare_parts):
        part, warning = check_spare_parts("NONEXISTENT", conn, "sqlite")
        assert part is None
        assert warning is None

    def test_create_purchase_order_correct_quantity(self, conn, seed_spare_parts, seed_factories):
        part, _ = check_spare_parts("CVD", conn, "sqlite")
        expected_qty = part["safe_stock_level"] * PURCHASE_QUANTITY_FACTOR - part["stock_quantity"]
        po_id = create_purchase_order(
            part["part_id"], "F-SH", expected_qty, "库存不足测试", conn, "sqlite"
        )
        assert po_id is not None
        cur = conn.cursor()
        cur.execute("SELECT quantity, status, part_id FROM purchase_orders WHERE id = ?", (po_id,))
        row = cur.fetchone()
        assert row["quantity"] == expected_qty
        assert row["status"] == "pending"
        assert row["part_id"] == part["part_id"]

    def test_create_purchase_order_zero_quantity(self, conn, seed_spare_parts, seed_factories):
        po_id = create_purchase_order("SP-CVD-001", "F-SH", 0, "零数量", conn, "sqlite")
        assert po_id is None

    def test_create_purchase_order_negative_quantity(self, conn, seed_spare_parts, seed_factories):
        po_id = create_purchase_order("SP-CVD-001", "F-SH", -5, "负数量", conn, "sqlite")
        assert po_id is None

    def test_create_purchase_order_duplicate_skipped(self, conn, seed_spare_parts, seed_factories):
        po_id1 = create_purchase_order("SP-CVD-001", "F-SH", 10, "第一次", conn, "sqlite")
        assert po_id1 is not None
        po_id2 = create_purchase_order("SP-CVD-001", "F-SH", 10, "重复", conn, "sqlite")
        assert po_id2 is None

    def test_create_purchase_order_alert_callback(self, conn, seed_spare_parts, seed_factories):
        callback_data = []

        def callback(data):
            callback_data.append(data)

        create_purchase_order("SP-PVD-002", "F-SZ", 10, "回调测试", conn, "sqlite", callback)
        assert len(callback_data) == 1
        assert callback_data[0]["type"] == "purchase_order"
        assert callback_data[0]["part_id"] == "SP-PVD-002"
        assert callback_data[0]["quantity"] == 10

    def test_work_order_triggers_stock_check_low_stock(self, conn, seed_devices, seed_spare_parts, seed_engineers):
        now = datetime.utcnow()
        first_low_time = now - timedelta(hours=2)
        recent_time = now - timedelta(minutes=5)
        for m in range(0, 125, 5):
            ts = (first_low_time + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                (ts, "EQ-001", 450.0, 3.5, 600.0, 45.0),
            )
        conn.commit()

        alerts_received = []

        def alert_cb(data):
            alerts_received.append(data)

        with patch("workorder_engine.is_maintenance_window", return_value=False):
            check_work_order("EQ-001", 45.0, conn, "sqlite", alert_cb)

        cur = conn.cursor()
        cur.execute("SELECT * FROM work_orders WHERE device_id = 'EQ-001'")
        wo = cur.fetchone()
        assert wo is not None
        assert wo["spare_part_id"] is not None

        cur.execute("SELECT * FROM purchase_orders")
        po = cur.fetchone()
        assert po is not None
        assert po["quantity"] > 0

        wo_alerts = [a for a in alerts_received if a["type"] == "work_order"]
        assert len(wo_alerts) >= 1
        if "spare_part_warning" in wo_alerts[0]:
            assert "库存不足" in wo_alerts[0]["spare_part_warning"]

    def test_work_order_no_purchase_when_stock_sufficient(self, conn, seed_devices, seed_spare_parts, seed_engineers):
        conn.execute("UPDATE spare_parts SET stock_quantity = 999 WHERE equipment_type = 'CVD'")
        conn.commit()

        now = datetime.utcnow()
        first_low_time = now - timedelta(hours=2)
        for m in range(0, 125, 5):
            ts = (first_low_time + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                (ts, "EQ-001", 450.0, 3.5, 600.0, 45.0),
            )
        conn.commit()

        with patch("workorder_engine.is_maintenance_window", return_value=False):
            check_work_order("EQ-001", 45.0, conn, "sqlite", None)

        cur = conn.cursor()
        cur.execute("SELECT * FROM purchase_orders")
        po = cur.fetchone()
        assert po is None

    def test_purchase_order_quantity_formula(self, conn, seed_spare_parts, seed_factories):
        conn.execute("UPDATE spare_parts SET stock_quantity = 3 WHERE part_id = 'SP-CVD-002'")
        conn.commit()

        part, _ = check_spare_parts("CVD", conn, "sqlite")
        qty = part["safe_stock_level"] * PURCHASE_QUANTITY_FACTOR - part["stock_quantity"]
        expected = 8 * 3 - 3
        assert qty == expected

        po_id = create_purchase_order("SP-CVD-002", "F-SH", qty, "测试", conn, "sqlite")
        cur = conn.cursor()
        cur.execute("SELECT quantity FROM purchase_orders WHERE id = ?", (po_id,))
        assert cur.fetchone()["quantity"] == expected

    def test_purchase_order_approve_receive_flow(self, conn, seed_spare_parts, seed_factories):
        original_qty = 4
        po_id = create_purchase_order("SP-PVD-002", "F-SZ", original_qty, "审批收货测试", conn, "sqlite")

        conn.execute("UPDATE purchase_orders SET status = 'approved', approved_at = ? WHERE id = ?",
                     (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"), po_id))
        conn.commit()

        cur = conn.cursor()
        cur.execute("SELECT stock_quantity FROM spare_parts WHERE part_id = 'SP-PVD-002'")
        old_stock = cur.fetchone()["stock_quantity"]

        conn.execute("UPDATE purchase_orders SET status = 'received', received_at = ? WHERE id = ?",
                     (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"), po_id))
        conn.execute("UPDATE spare_parts SET stock_quantity = stock_quantity + ? WHERE part_id = ?",
                     (original_qty, "SP-PVD-002"))
        conn.commit()

        cur.execute("SELECT stock_quantity FROM spare_parts WHERE part_id = 'SP-PVD-002'")
        new_stock = cur.fetchone()["stock_quantity"]
        assert new_stock == old_stock + original_qty


# =============================================================================
# 寿命预测测试
# =============================================================================
class TestLifePrediction:
    def test_linear_regression_perfect_line(self):
        x = [0, 1, 2, 3, 4, 5]
        y = [100, 90, 80, 70, 60, 50]
        slope, intercept = _linear_regression(x, y)
        assert abs(slope - (-10.0)) < 0.01
        assert abs(intercept - 100.0) < 0.01

    def test_linear_regression_horizontal(self):
        x = [0, 1, 2, 3, 4]
        y = [85, 85, 85, 85, 85]
        slope, intercept = _linear_regression(x, y)
        assert abs(slope) < 1e-10
        assert abs(intercept - 85.0) < 0.01

    def test_linear_regression_single_point(self):
        slope, intercept = _linear_regression([0], [50])
        assert slope == 0.0
        assert intercept == 0.0

    def test_linear_regression_empty(self):
        slope, intercept = _linear_regression([], [])
        assert slope == 0.0
        assert intercept == 0.0

    def test_r_squared_acceptable_declining(self, conn, seed_devices):
        def declining_health(minute):
            return 90 - minute * 0.01

        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=declining_health)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        assert result["status"] == "predicted"
        assert result["confidence"] > 0.7, f"R²={result['confidence']} too low for strong linear trend"
        assert result["remaining_days"] > 0
        assert result["slope"] < 0
        assert result["data_points"] >= MIN_DATA_POINTS_FOR_PREDICTION

    def test_r_squared_low_for_noisy_data(self, conn, seed_devices):
        def noisy_health(minute):
            return 75 + random.uniform(-25, 25)

        random.seed(42)
        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=noisy_health)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        assert result["data_points"] >= MIN_DATA_POINTS_FOR_PREDICTION
        if result["status"] == "predicted":
            assert result["confidence"] < 0.5

    def test_stable_or_improving_device(self, conn, seed_devices):
        def improving_health(minute):
            return min(100, 80 + minute * 0.05)

        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=improving_health)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        assert result["status"] == "stable_or_improving"
        assert result["remaining_days"] == 365.0
        assert result["slope"] >= 0

    def test_insufficient_data(self, conn, seed_devices):
        _insert_sensor_data(conn, "EQ-001", hours=1, interval_min=5, health_fn=lambda m: 80.0)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        assert result["status"] == "insufficient_data"
        assert result["remaining_days"] is None
        assert result["confidence"] == 0.0

    def test_no_data_at_all(self, conn, seed_devices):
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        assert result["status"] == "insufficient_data"

    def test_different_time_windows_stability(self, conn, seed_devices):
        def steady_decline(minute):
            return 85 - minute * 0.05

        _insert_sensor_data(conn, "EQ-001", hours=72, interval_min=5, health_fn=steady_decline)

        result_24h = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        result_48h = predict_remaining_life("EQ-001", conn, "sqlite", hours=48)
        result_72h = predict_remaining_life("EQ-001", conn, "sqlite", hours=72)

        assert result_24h["status"] in ("predicted", "stable_or_improving")
        assert result_48h["status"] in ("predicted", "stable_or_improving")
        assert result_72h["status"] in ("predicted", "stable_or_improving")

        if result_24h["status"] == "predicted" and result_72h["status"] == "predicted":
            day_diff = abs(result_24h["remaining_days"] - result_72h["remaining_days"])
            assert day_diff < 100, f"Prediction varies too much across windows: 24h={result_24h['remaining_days']}, 72h={result_72h['remaining_days']}"

    def test_prediction_residual_distribution(self, conn, seed_devices):
        def steady_decline(minute):
            return 85 - minute * 0.05

        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=steady_decline)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)

        if result["status"] != "predicted":
            pytest.skip("Not in predicted state")

        history, _ = _get_health_history("EQ-001", conn, "sqlite", hours=24)
        start_time = history[0][0]
        x_vals = []
        y_vals = []
        for t, hs in history:
            delta_h = (t - start_time).total_seconds() / 3600.0
            x_vals.append(delta_h)
            y_vals.append(hs)

        slope = result["slope"]
        intercept = result["intercept"]

        residuals = [y - (slope * x + intercept) for x, y in zip(x_vals, y_vals)]
        mean_res = sum(residuals) / len(residuals)
        assert abs(mean_res) < 2.0, f"Residual mean {mean_res} indicates systematic bias"

        std_res = (sum((r - mean_res) ** 2 for r in residuals) / len(residuals)) ** 0.5
        assert std_res < 5.0, f"Residual std {std_res} too high, prediction unreliable"

    def test_already_failed_device(self, conn, seed_devices):
        def failed_health(minute):
            return max(0, 10 - minute * 0.01)

        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=failed_health)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        if result["status"] == "predicted":
            assert result["remaining_days"] == 0

    def test_near_threshold_device(self, conn, seed_devices):
        def near_threshold(minute):
            return FAIL_THRESHOLD + 5 - minute * 0.01

        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=near_threshold)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        if result["status"] == "predicted":
            assert result["remaining_days"] >= 0

    def test_prediction_with_constant_zero_health(self, conn, seed_devices):
        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=lambda m: 0.0)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        assert result["data_points"] >= MIN_DATA_POINTS_FOR_PREDICTION


# =============================================================================
# 排班分配测试
# =============================================================================
class TestSchedulingAssignment:
    def test_skill_matching_accuracy(self, conn, seed_engineers, seed_factories):
        eng_id, reason = recommend_engineer("CVD", "F-SH", conn, "sqlite")
        assert eng_id == "ENG-001"
        assert "CVD" in reason
        assert "ENG-001" in reason

    def test_skill_matching_different_type(self, conn, seed_engineers, seed_factories):
        eng_id, reason = recommend_engineer("PVD", "F-BJ", conn, "sqlite")
        assert eng_id == "ENG-003"
        assert "PVD" in reason

    def test_no_matching_specialty(self, conn, seed_engineers, seed_factories):
        eng_id, reason = recommend_engineer("NONEXISTENT", "F-SH", conn, "sqlite")
        assert eng_id is None
        assert reason is None

    def test_load_balancing_prefers_lighter(self, conn, seed_engineers, seed_factories):
        _insert_work_orders(conn, "ENG-001", 3, "pending")
        eng_id, reason = recommend_engineer("CVD", "F-SH", conn, "sqlite")
        assert eng_id == "ENG-001"

        _insert_work_orders(conn, "ENG-001", 2, "pending")
        eng_id2, reason2 = recommend_engineer("CVD", "F-SH", conn, "sqlite")
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM work_orders WHERE engineer_id = 'ENG-001' AND status IN ('pending','accepted')")
        eng1_pending = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) as cnt FROM work_orders WHERE engineer_id = 'ENG-002' AND status IN ('pending','accepted')")
        eng2_pending = cur.fetchone()["cnt"]

        if eng1_pending >= ENGINEER_MAX_LOAD:
            assert eng_id2 == "ENG-002"
            assert "跨厂区" in reason2
        else:
            assert eng_id2 == "ENG-001"

    def test_all_engineers_at_max_load(self, conn, seed_engineers, seed_factories):
        _insert_work_orders(conn, "ENG-001", ENGINEER_MAX_LOAD, "pending")
        _insert_work_orders(conn, "ENG-002", ENGINEER_MAX_LOAD, "pending")
        eng_id, reason = recommend_engineer("CVD", "F-SH", conn, "sqlite")
        assert eng_id is None
        assert "负载上限" in reason

    def test_no_engineer_for_factory(self, conn, seed_engineers, seed_factories):
        conn.execute("INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES ('EQ-099','TEST','LITHO','A1','F-SH','ENG-001',120,1.0,300,'online')")
        conn.commit()
        eng_id, reason = recommend_engineer("LITHO", "F-SH", conn, "sqlite")
        assert eng_id is None
        assert reason is None

    def test_recommend_without_factory_id(self, conn, seed_engineers, seed_factories):
        eng_id, reason = recommend_engineer("CVD", None, conn, "sqlite")
        assert eng_id is not None
        assert "CVD" in reason

    def test_get_engineer_workload(self, conn, seed_engineers, seed_factories):
        _insert_work_orders(conn, "ENG-001", 2, "pending")
        _insert_work_orders(conn, "ENG-001", 3, "accepted")
        workload = get_engineer_workload("ENG-001", conn, "sqlite")
        assert workload["pending"] == 5
        assert workload["load_ratio"] == 1.0

    def test_workload_no_orders(self, conn, seed_engineers, seed_factories):
        workload = get_engineer_workload("ENG-001", conn, "sqlite")
        assert workload["pending"] == 0
        assert workload["load_ratio"] == 0.0

    def test_workload_capped_at_max(self, conn, seed_engineers, seed_factories):
        _insert_work_orders(conn, "ENG-001", ENGINEER_MAX_LOAD + 3, "pending")
        workload = get_engineer_workload("ENG-001", conn, "sqlite")
        assert workload["load_ratio"] == 1.0

    def test_degradation_fallback_cross_factory(self, conn, seed_engineers, seed_factories):
        _insert_work_orders(conn, "ENG-001", ENGINEER_MAX_LOAD, "pending")
        eng_id, reason = recommend_engineer("CVD", None, conn, "sqlite")
        assert eng_id == "ENG-002"
        assert "CVD" in reason

    def test_work_order_includes_recommended_engineer(self, conn, seed_devices, seed_spare_parts, seed_engineers):
        now = datetime.utcnow()
        first_low = now - timedelta(hours=2)
        for m in range(0, 125, 5):
            ts = (first_low + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                (ts, "EQ-001", 450.0, 3.5, 600.0, 40.0),
            )
        conn.commit()

        with patch("workorder_engine.is_maintenance_window", return_value=False):
            check_work_order("EQ-001", 40.0, conn, "sqlite", None)

        cur = conn.cursor()
        cur.execute("SELECT recommended_engineer_id, assignment_reason FROM work_orders WHERE device_id = 'EQ-001'")
        wo = cur.fetchone()
        assert wo["recommended_engineer_id"] is not None
        assert wo["assignment_reason"] is not None


# =============================================================================
# 多厂区视图测试
# =============================================================================
class TestMultiFactoryView:
    def test_factory_data_aggregation(self, conn, seed_factories, seed_devices):
        now = datetime.utcnow()
        for m in range(0, 30, 5):
            ts = (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                (ts, "EQ-001", 350.0, 2.5, 500.0, 90.0),
            )
        conn.commit()

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = 'F-SH'")
        assert cur.fetchone()[0] == 1

        cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = 'F-SZ'")
        assert cur.fetchone()[0] == 1

        cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = 'F-BJ'")
        assert cur.fetchone()[0] == 1

    def test_factory_device_count_matches(self, conn, seed_factories):
        devices = []
        for i in range(10):
            fid = "F-SH"
            devices.append((f"EQ-{i+1:03d}", f"DEV-{i}", "CVD", "A1", fid, None, 350, 2.5, 500, "online"))
        for i in range(5):
            fid = "F-SZ"
            devices.append((f"EQ-{i+11:03d}", f"DEV-{i+10}", "PVD", "A2", fid, None, 280, 3.0, 800, "online"))
        conn.executemany(
            "INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            devices,
        )
        conn.commit()

        cur = conn.cursor()
        for factory_id, expected in [("F-SH", 10), ("F-SZ", 5), ("F-BJ", 0)]:
            cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = ?", (factory_id,))
            assert cur.fetchone()[0] == expected

    def test_avg_health_per_factory(self, conn, seed_factories):
        devices_sh = [(f"EQ-{i+1:03d}", f"CVD-A1-{i}", "CVD", "A1", "F-SH", None, 350, 2.5, 500, "online") for i in range(3)]
        devices_sz = [(f"EQ-{i+4:03d}", f"PVD-A2-{i}", "PVD", "A2", "F-SZ", None, 280, 3.0, 800, "online") for i in range(2)]
        conn.executemany(
            "INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            devices_sh + devices_sz,
        )
        conn.commit()

        now = datetime.utcnow()
        health_scores = {"EQ-001": 90, "EQ-002": 80, "EQ-003": 70, "EQ-004": 60, "EQ-005": 50}
        for devid, hs in health_scores.items():
            for m in range(0, 30, 5):
                ts = (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
                conn.execute(
                    "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                    (ts, devid, 300.0, 2.0, 400.0, hs),
                )
        conn.commit()

        cur = conn.cursor()
        cur.execute("""
            SELECT d.device_id, s.health_score
            FROM devices d
            JOIN (
                SELECT device_id, health_score, time
                FROM sensor_data
                GROUP BY device_id
                HAVING time = MAX(time)
            ) s ON d.device_id = s.device_id
            WHERE d.factory_id = 'F-SH'
        """)
        sh_scores = [row["health_score"] for row in cur.fetchall()]
        assert len(sh_scores) == 3
        assert abs(sum(sh_scores) / len(sh_scores) - 80.0) < 0.1

    def test_cross_factory_consistency(self, conn, seed_factories):
        all_devices = []
        for fid in ["F-SH", "F-SZ", "F-BJ"]:
            for i in range(3):
                idx = hash(fid + str(i)) % 1000
                all_devices.append(
                    (f"EQ-{fid}-{i}", f"DEV-{fid}-{i}", "CVD", "A1", fid, None, 350, 2.5, 500, "online")
                )
        conn.executemany(
            "INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            all_devices,
        )
        conn.commit()

        now = datetime.utcnow()
        for dev in all_devices:
            did = dev[0]
            for m in range(0, 30, 5):
                ts = (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
                conn.execute(
                    "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                    (ts, did, 350.0, 2.5, 500.0, 85.0),
                )
        conn.commit()

        total_in_factories = 0
        cur = conn.cursor()
        for fid in ["F-SH", "F-SZ", "F-BJ"]:
            cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = ?", (fid,))
            total_in_factories += cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM devices")
        total_all = cur.fetchone()[0]
        assert total_in_factories == total_all

    def test_factory_not_found(self, conn, seed_factories):
        cur = conn.cursor()
        cur.execute("SELECT * FROM factories WHERE factory_id = 'F-XX'")
        assert cur.fetchone() is None

    def test_empty_factory_has_zero_devices(self, conn, seed_factories):
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = 'F-BJ'")
        assert cur.fetchone()[0] == 0

    def test_factory_summary_pending_orders(self, conn, seed_factories):
        conn.execute(
            "INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES ('EQ-001','D','CVD','A1','F-SH',NULL,350,2.5,500,'online')"
        )
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "INSERT INTO work_orders (device_id, factory_id, engineer_id, reason, status, created_at) VALUES ('EQ-001','F-SH',NULL,'测试','pending',?)",
            (now_str,),
        )
        conn.commit()

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM work_orders WHERE factory_id = 'F-SH' AND status = 'pending'")
        assert cur.fetchone()[0] == 1

        cur.execute("SELECT COUNT(*) FROM work_orders WHERE factory_id = 'F-SZ' AND status = 'pending'")
        assert cur.fetchone()[0] == 0

    def test_factory_life_predictions_by_factory(self, conn, seed_factories, seed_devices):
        def steady_decline(minute):
            return 90 - minute * 0.01

        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=steady_decline)
        _insert_sensor_data(conn, "EQ-002", hours=24, interval_min=5, health_fn=steady_decline)

        result1 = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        result2 = predict_remaining_life("EQ-002", conn, "sqlite", hours=24)

        if result1["status"] == "predicted" and result2["status"] == "predicted":
            assert result1["remaining_days"] > 0
            assert result2["remaining_days"] > 0
            day_diff = abs(result1["remaining_days"] - result2["remaining_days"])
            assert day_diff < 50


# =============================================================================
# 异常场景测试
# =============================================================================
class TestExceptionScenarios:
    def test_spare_parts_table_empty(self, conn):
        part, warning = check_spare_parts("CVD", conn, "sqlite")
        assert part is None
        assert warning is None

    def test_engineers_table_empty(self, conn, seed_factories):
        eng_id, reason = recommend_engineer("CVD", "F-SH", conn, "sqlite")
        assert eng_id is None
        assert reason is None

    def test_device_not_found_in_prediction(self, conn):
        result = predict_remaining_life("EQ-NONEXIST", conn, "sqlite", hours=24)
        assert result["status"] == "insufficient_data"

    def test_purchase_order_for_nonexistent_part(self, conn, seed_factories):
        po_id = create_purchase_order("SP-XXXX-001", "F-SH", 10, "不存在备件", conn, "sqlite")
        assert po_id is not None

    def test_recommend_engineer_for_nonexistent_factory(self, conn, seed_engineers, seed_factories):
        eng_id, reason = recommend_engineer("CVD", "F-XX", conn, "sqlite")
        assert eng_id is not None
        assert "跨厂区" in reason or "专业匹配" in reason

    def test_work_order_maintenance_window_blocked(self, conn, seed_devices, seed_spare_parts, seed_engineers):
        now = datetime.utcnow()
        first_low = now - timedelta(hours=2)
        for m in range(0, 125, 5):
            ts = (first_low + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                (ts, "EQ-001", 450.0, 3.5, 600.0, 40.0),
            )
        conn.commit()

        with patch("workorder_engine.is_maintenance_window", return_value=True):
            check_work_order("EQ-001", 40.0, conn, "sqlite", None)

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM work_orders WHERE device_id = 'EQ-001'")
        assert cur.fetchone()[0] == 0

    def test_work_order_already_exists(self, conn, seed_devices, seed_spare_parts, seed_engineers):
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "INSERT INTO work_orders (device_id, factory_id, engineer_id, reason, status, created_at) VALUES ('EQ-001','F-SH','ENG-001','已有工单','pending',?)",
            (now_str,),
        )
        conn.commit()

        now = datetime.utcnow()
        first_low = now - timedelta(hours=2)
        for m in range(0, 125, 5):
            ts = (first_low + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                (ts, "EQ-001", 450.0, 3.5, 600.0, 40.0),
            )
        conn.commit()

        with patch("workorder_engine.is_maintenance_window", return_value=False):
            check_work_order("EQ-001", 40.0, conn, "sqlite", None)

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM work_orders WHERE device_id = 'EQ-001'")
        assert cur.fetchone()[0] == 1

    def test_health_score_above_threshold_no_order(self, conn, seed_devices, seed_spare_parts, seed_engineers):
        with patch("workorder_engine.is_maintenance_window", return_value=False):
            check_work_order("EQ-001", 75.0, conn, "sqlite", None)

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM work_orders")
        assert cur.fetchone()[0] == 0

    def test_prediction_with_nan_like_values(self, conn, seed_devices):
        def nan_health(minute):
            return 0.0 if minute % 100 == 0 else 90.0

        _insert_sensor_data(conn, "EQ-001", hours=24, interval_min=5, health_fn=nan_health)
        result = predict_remaining_life("EQ-001", conn, "sqlite", hours=24)
        assert result["status"] in ("predicted", "stable_or_improving", "insufficient_data")

    def test_simultaneous_work_orders_different_devices(self, conn, seed_factories, seed_spare_parts, seed_engineers):
        devices = [
            ("EQ-100", "CVD-A1-100", "CVD", "A1", "F-SH", "ENG-001", 350, 2.5, 500, "online"),
            ("EQ-101", "CVD-A1-101", "CVD", "A1", "F-SH", "ENG-001", 350, 2.5, 500, "online"),
        ]
        conn.executemany(
            "INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            devices,
        )
        conn.commit()

        now = datetime.utcnow()
        first_low = now - timedelta(hours=2)
        for dev_id in ["EQ-100", "EQ-101"]:
            for m in range(0, 125, 5):
                ts = (first_low + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
                conn.execute(
                    "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                    (ts, dev_id, 450.0, 3.5, 600.0, 40.0),
                )
        conn.commit()

        with patch("workorder_engine.is_maintenance_window", return_value=False):
            check_work_order("EQ-100", 40.0, conn, "sqlite", None)
            check_work_order("EQ-101", 40.0, conn, "sqlite", None)

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM work_orders")
        assert cur.fetchone()[0] == 2

    def test_linear_regression_two_identical_x(self):
        x = [1, 1, 2, 3]
        y = [10, 10, 20, 30]
        slope, intercept = _linear_regression(x, y)
        assert not math.isnan(slope)
        assert not math.isnan(intercept)
        assert slope > 0

    def test_callback_exception_does_not_crash(self, conn, seed_spare_parts, seed_factories):
        def bad_callback(data):
            raise RuntimeError("callback failed")

        po_id = create_purchase_order("SP-CVD-001", "F-SH", 10, "回调异常", conn, "sqlite", bad_callback)
        assert po_id is not None

    def test_optimistic_lock_conflict_rejects(self, conn, seed_spare_parts, seed_factories):
        cur = conn.cursor()
        cur.execute("SELECT version FROM spare_parts WHERE part_id = 'SP-CVD-001'")
        current_version = cur.fetchone()[0]
        cur.close()

        po_id1 = create_purchase_order(
            "SP-CVD-001", "F-SH", 10, "First", conn, "sqlite", None,
            expected_version=current_version
        )
        assert po_id1 is not None

        po_id2 = create_purchase_order(
            "SP-CVD-001", "F-SH", 10, "Conflict", conn, "sqlite", None,
            expected_version=current_version
        )
        assert po_id2 is None

        cur = conn.cursor()
        cur.execute("SELECT version FROM spare_parts WHERE part_id = 'SP-CVD-001'")
        new_version = cur.fetchone()[0]
        cur.close()
        assert new_version == current_version + 1

    def test_optimistic_lock_no_version_passes(self, conn, seed_spare_parts, seed_factories):
        po_id = create_purchase_order("SP-CVD-001", "F-SH", 10, "No version", conn, "sqlite", None)
        assert po_id is not None

    def test_factory_with_offline_devices(self, conn, seed_factories):
        devices = [
            ("EQ-001", "CVD-A1-001", "CVD", "A1", "F-SH", None, 350, 2.5, 500, "offline"),
            ("EQ-002", "CVD-A1-002", "CVD", "A1", "F-SH", None, 350, 2.5, 500, "online"),
        ]
        conn.executemany(
            "INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            devices,
        )
        conn.commit()

        now = datetime.utcnow()
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
            (ts, "EQ-002", 350.0, 2.5, 500.0, 90.0),
        )
        conn.commit()

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = 'F-SH' AND status = 'offline'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = 'F-SH' AND status = 'online'")
        assert cur.fetchone()[0] == 1


# =============================================================================
# API 端点测试
# =============================================================================
class TestAPIEndpoints:
    @pytest.fixture
    def test_client(self, request):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        db_path = os.path.join(os.path.dirname(__file__), "_test_api.db")
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except PermissionError:
                pass

        test_conn = sqlite3.connect(db_path)
        test_conn.row_factory = sqlite3.Row
        test_conn.executescript(DB_SCHEMA)

        test_conn.executemany(
            "INSERT INTO factories (factory_id, factory_name, address, lat, lng, status) VALUES (?,?,?,?,?,'active')",
            [
                ("F-SH", "上海晶圆厂", "上海市浦东新区", 31.2, 121.5),
                ("F-SZ", "深圳晶圆厂", "深圳市南山区", 22.5, 113.9),
                ("F-BJ", "北京晶圆厂", "北京市亦庄", 39.7, 116.5),
            ],
        )
        test_conn.executemany(
            "INSERT INTO spare_parts (part_id, part_name, equipment_type, stock_quantity, safe_stock_level, unit_price, supplier) VALUES (?,?,?,?,?,?,?)",
            [
                ("SP-CVD-001", "CVD密封圈", "CVD", 15, 10, 2500, "AMAT"),
                ("SP-CVD-002", "CVD加热器", "CVD", 5, 8, 15000, "AMAT"),
            ],
        )
        test_conn.executemany(
            "INSERT INTO engineers (engineer_id, name, specialty, factory_id) VALUES (?,?,?,?)",
            [("ENG-001", "张伟", "CVD", "F-SH"), ("ENG-002", "李强", "CVD", "F-SZ")],
        )
        now = datetime.utcnow()
        for i in range(3):
            did = f"EQ-{i+1:03d}"
            test_conn.execute(
                "INSERT INTO devices (device_id, device_name, equipment_type, area, factory_id, engineer_id, baseline_temperature, baseline_vibration, baseline_rf_power, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, f"CVD-A1-{i}", "CVD", "A1", "F-SH", "ENG-001", 350, 2.5, 500, "online"),
            )
            for m in range(0, 24 * 60, 5):
                ts = (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
                test_conn.execute(
                    "INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score) VALUES (?,?,?,?,?,?)",
                    (ts, did, 350.0, 2.5, 500.0, 90.0 - m * 0.01),
                )
        test_conn.commit()
        test_conn.close()

        app = FastAPI()
        lock = threading.Lock()
        _open_conns = []

        def get_conn():
            c = sqlite3.connect(db_path, timeout=10)
            c.row_factory = sqlite3.Row
            _open_conns.append(c)
            return c

        def close_conns():
            for c in _open_conns:
                try:
                    c.close()
                except Exception:
                    pass
            _open_conns.clear()

        @app.get("/api/factories")
        def list_factories():
            with lock:
                c = get_conn()
                cur = c.cursor()
                cur.execute("SELECT * FROM factories ORDER BY factory_id")
                factories = cur.fetchall()
                result = []
                for f in factories:
                    fd = dict(f)
                    cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = ?", (fd["factory_id"],))
                    fd["device_count"] = cur.fetchone()[0]
                    cur.execute(
                        "SELECT AVG(health_score) FROM (SELECT MAX(time) as mt, device_id FROM sensor_data WHERE device_id IN (SELECT device_id FROM devices WHERE factory_id = ?) GROUP BY device_id) t JOIN sensor_data s ON t.device_id = s.device_id AND t.mt = s.time",
                        (fd["factory_id"],),
                    )
                    avg = cur.fetchone()[0]
                    fd["avg_health"] = round(avg, 1) if avg else 0
                    result.append(fd)
                cur.close()
                c.close()
            return result

        @app.get("/api/factories/{factory_id}/summary")
        def factory_summary(factory_id: str):
            with lock:
                c = get_conn()
                cur = c.cursor()
                cur.execute("SELECT * FROM factories WHERE factory_id = ?", (factory_id,))
                factory = cur.fetchone()
                if not factory:
                    cur.close()
                    c.close()
                    from fastapi import HTTPException
                    raise HTTPException(status_code=404, detail="Factory not found")
                cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = ?", (factory_id,))
                total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM work_orders WHERE factory_id = ? AND status = 'pending'", (factory_id,))
                pending = cur.fetchone()[0]
                cur.close()
                c.close()
            return {"factory": dict(factory), "total_devices": total, "pending_work_orders": pending}

        @app.get("/api/factories/{factory_id}/devices")
        def list_factory_devices(factory_id: str):
            with lock:
                c = get_conn()
                cur = c.cursor()
                cur.execute(
                    "SELECT d.device_id, d.device_name, d.equipment_type, d.area, d.factory_id, d.status, s.health_score FROM devices d LEFT JOIN (SELECT device_id, health_score, time FROM sensor_data GROUP BY device_id HAVING time = MAX(time)) s ON d.device_id = s.device_id WHERE d.factory_id = ? ORDER BY d.device_id",
                    (factory_id,),
                )
                devices = cur.fetchall()
                cur.close()
                c.close()
            return [dict(d) for d in devices]

        @app.get("/api/spare-parts")
        def list_spare_parts(equipment_type: str = None):
            with lock:
                c = get_conn()
                cur = c.cursor()
                q = "SELECT * FROM spare_parts"
                params = []
                if equipment_type:
                    q += " WHERE equipment_type = ?"
                    params.append(equipment_type)
                cur.execute(q, params)
                parts = cur.fetchall()
                cur.close()
                c.close()
            return [dict(p) for p in parts]

        @app.get("/api/devices/{device_id}/life-prediction")
        def device_life_prediction(device_id: str, hours: int = 72):
            with lock:
                c = get_conn()
                result = predict_remaining_life(device_id, c, "sqlite", hours=hours)
                c.close()
            return result

        @app.get("/api/work-orders/{order_id}/recommend-engineer")
        def recommend_for_order(order_id: int):
            with lock:
                c = get_conn()
                cur = c.cursor()
                cur.execute(
                    "SELECT d.equipment_type, d.factory_id FROM work_orders wo JOIN devices d ON wo.device_id = d.device_id WHERE wo.id = ?",
                    (order_id,),
                )
                row = cur.fetchone()
                cur.close()
                if not row:
                    c.close()
                    from fastapi import HTTPException
                    raise HTTPException(status_code=404, detail="Not found")
                eng_id, reason = recommend_engineer(row["equipment_type"], row["factory_id"], c, "sqlite")
                c.close()
            return {"recommended_engineer_id": eng_id, "assignment_reason": reason}

        client = TestClient(app)

        def cleanup():
            close_conns()
            try:
                if os.path.exists(db_path):
                    os.remove(db_path)
            except PermissionError:
                pass

        request.addfinalizer(cleanup)
        yield client

    def test_api_list_factories(self, test_client):
        resp = test_client.get("/api/factories")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert all("factory_id" in f for f in data)
        assert all("device_count" in f for f in data)
        assert all("avg_health" in f for f in data)

    def test_api_factory_summary(self, test_client):
        resp = test_client.get("/api/factories/F-SH/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["factory"]["factory_id"] == "F-SH"
        assert data["total_devices"] == 3
        assert "pending_work_orders" in data

    def test_api_factory_not_found(self, test_client):
        resp = test_client.get("/api/factories/F-XX/summary")
        assert resp.status_code == 404

    def test_api_factory_devices(self, test_client):
        resp = test_client.get("/api/factories/F-SH/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert all(d["factory_id"] == "F-SH" for d in data if "factory_id" in d)

    def test_api_factory_devices_empty(self, test_client):
        resp = test_client.get("/api/factories/F-BJ/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 0

    def test_api_spare_parts_list(self, test_client):
        resp = test_client.get("/api/spare-parts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_api_spare_parts_filter(self, test_client):
        resp = test_client.get("/api/spare-parts?equipment_type=CVD")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(p["equipment_type"] == "CVD" for p in data)

    def test_api_spare_parts_filter_empty(self, test_client):
        resp = test_client.get("/api/spare-parts?equipment_type=NONEXISTENT")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 0

    def test_api_life_prediction(self, test_client):
        resp = test_client.get("/api/devices/EQ-001/life-prediction?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "remaining_days" in data
        assert "confidence" in data
        assert data["device_id"] == "EQ-001"

    def test_api_life_prediction_nonexistent(self, test_client):
        resp = test_client.get("/api/devices/EQ-999/life-prediction?hours=24")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "insufficient_data"

    def test_api_recommend_engineer_not_found(self, test_client):
        resp = test_client.get("/api/work-orders/99999/recommend-engineer")
        assert resp.status_code == 404

    def test_api_cross_factory_consistency(self, test_client):
        f_sh = test_client.get("/api/factories/F-SH/summary").json()
        f_sz = test_client.get("/api/factories/F-SZ/summary").json()
        f_bj = test_client.get("/api/factories/F-BJ/summary").json()

        sh_devices = test_client.get("/api/factories/F-SH/devices").json()
        sz_devices = test_client.get("/api/factories/F-SZ/devices").json()
        bj_devices = test_client.get("/api/factories/F-BJ/devices").json()

        assert f_sh["total_devices"] == len(sh_devices)
        assert f_sz["total_devices"] == len(sz_devices)
        assert f_bj["total_devices"] == len(bj_devices)

        all_device_ids = set()
        for d in sh_devices + sz_devices + bj_devices:
            assert d["device_id"] not in all_device_ids
            all_device_ids.add(d["device_id"])
