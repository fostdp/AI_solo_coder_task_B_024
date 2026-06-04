import os
import sys
import asyncio
import logging
import random
import time
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from health_evaluator import (
    compute_health_score, update_baselines_ema, should_update_baseline,
    check_and_fire_alerts, is_maintenance_window
)
from workorder_engine import check_work_order
from managers import MultiSiteView, LifePredictor

DB_PATH = os.path.join(os.path.dirname(__file__), "demo.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DemoAPI] %(message)s")
logger = logging.getLogger(__name__)

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

EQUIPMENT_TYPES = list(BASELINES.keys())
AREAS = ["A1", "A2", "A3", "B1", "B2", "B3"]

ENGINEERS = [
    {"id": f"ENG-{i+1:03d}", "name": name}
    for i, name in enumerate([
        "张伟", "李强", "王芳", "刘洋", "陈磊",
        "赵敏", "周涛", "吴鹏", "郑华", "孙丽",
        "马超", "朱军", "胡明", "林峰", "何勇",
        "高健", "罗斌", "谢刚", "韩雪", "唐杰",
    ])
]

ws_clients: List[WebSocket] = []
_db_lock = threading.Lock()


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_type_baseline_sqlite(equipment_type):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT AVG(baseline_temperature), AVG(baseline_vibration), AVG(baseline_rf_power)
        FROM devices WHERE equipment_type = ?;
    """, (equipment_type,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and row[0]:
        return row[0], row[1], row[2]
    return None, None, None


def _update_baseline_sqlite(device_id, bt, bv, bp):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE devices SET
            baseline_temperature = ?,
            baseline_vibration = ?,
            baseline_rf_power = ?
        WHERE device_id = ?;
    """, (bt, bv, bp, device_id))
    conn.commit()
    cur.close()
    conn.close()


async def broadcast_alert(alert_data: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(alert_data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


def _sync_broadcast(alert_data: dict):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast_alert(alert_data))
    except RuntimeError:
        pass


simulator_running = False
degradation_state = {}
device_baselines_cache = {}


def simulator_loop():
    global simulator_running
    logger.info("Background simulator started (200 devices, 30s interval)")

    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        for i in range(200):
            device_id = f"EQ-{i+1:03d}"
            eq_type = EQUIPMENT_TYPES[i % len(EQUIPMENT_TYPES)]
            cur.execute("""
                SELECT baseline_temperature, baseline_vibration, baseline_rf_power
                FROM devices WHERE device_id = ?;
            """, (device_id,))
            row = cur.fetchone()
            if row and row[0]:
                device_baselines_cache[device_id] = (row[0], row[1], row[2])
            else:
                bl = BASELINES[eq_type]
                device_baselines_cache[device_id] = (bl["temperature"], bl["vibration"], bl["rf_power"])
        cur.close()
        conn.close()

    for i in range(200):
        device_id = f"EQ-{i+1:03d}"
        degradation_state[device_id] = {
            "degrading": random.random() < 0.08,
            "factor": 1.0,
            "rate": random.uniform(0.0005, 0.003),
            "anomaly_mode": False,
            "anomaly_countdown": 0,
        }

    cycle = 0
    while simulator_running:
        cycle += 1
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        batch = []

        with _db_lock:
            conn = get_connection()

            for i in range(200):
                device_id = f"EQ-{i+1:03d}"
                eq_type = EQUIPMENT_TYPES[i % len(EQUIPMENT_TYPES)]
                bt, bv, bp = device_baselines_cache.get(
                    device_id, (BASELINES[eq_type]["temperature"],
                                BASELINES[eq_type]["vibration"],
                                BASELINES[eq_type]["rf_power"])
                )
                state = degradation_state[device_id]

                if state["degrading"]:
                    state["factor"] += state["rate"]

                if random.random() < 0.01:
                    state["anomaly_mode"] = True
                    state["anomaly_countdown"] = random.randint(2, 10)

                if state["anomaly_mode"] and state["anomaly_countdown"] > 0:
                    temp = bt * (1 + random.uniform(0.2, 0.5) * state["factor"])
                    vib = bv * (1 + random.uniform(0.3, 0.8) * state["factor"])
                    pwr = bp * (1 + random.uniform(-0.3, 0.3))
                    state["anomaly_countdown"] -= 1
                    if state["anomaly_countdown"] <= 0:
                        state["anomaly_mode"] = False
                else:
                    temp = bt + random.gauss(0, bt * 0.05) * state["factor"]
                    vib = bv + random.gauss(0, bv * 0.08) * state["factor"]
                    pwr = bp + random.gauss(0, bp * 0.04)

                temp = max(bt * 0.5, min(bt * 2.0, temp))
                vib = max(bv * 0.2, min(bv * 3.0, vib))
                pwr = max(bp * 0.3, min(bp * 1.8, pwr))

                health = compute_health_score(
                    temp, vib, pwr, bt, bv, bp,
                    device_id, eq_type,
                    _get_type_baseline_sqlite, _update_baseline_sqlite
                )

                new_bt, new_bv, new_bp = update_baselines_ema(
                    device_id, temp, vib, pwr,
                    bt, bv, bp, conn, 'sqlite', _update_baseline_sqlite
                )
                device_baselines_cache[device_id] = (new_bt, new_bv, new_bp)

                batch.append((now_str, device_id, round(temp, 2), round(vib, 4), round(pwr, 2), health))

                check_and_fire_alerts(
                    device_id, temp, vib, pwr,
                    new_bt, new_bv, new_bp,
                    conn, 'sqlite', _sync_broadcast
                )
                check_work_order(device_id, health, conn, 'sqlite', _sync_broadcast)

            cur = conn.cursor()
            cur.executemany("""
                INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score)
                VALUES (?, ?, ?, ?, ?, ?);
            """, batch)
            conn.commit()
            cur.close()
            conn.close()

        logger.info(f"Sim cycle {cycle}: inserted {len(batch)} readings")
        time.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global simulator_running
    simulator_running = True
    t = threading.Thread(target=simulator_loop, daemon=True)
    t.start()
    yield
    simulator_running = False


app = FastAPI(title="半导体晶圆厂设备预防性维护平台", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/devices")
async def list_devices():
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT d.device_id, d.device_name, d.equipment_type, d.area,
                   d.engineer_id, d.baseline_temperature, d.baseline_vibration,
                   d.baseline_rf_power, d.status,
                   s.health_score, s.temperature, s.vibration, s.rf_power, s.time
            FROM devices d
            LEFT JOIN (
                SELECT device_id, health_score, temperature, vibration, rf_power, time
                FROM sensor_data
                GROUP BY device_id
                HAVING time = MAX(time)
            ) s ON d.device_id = s.device_id
            ORDER BY d.device_id;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return [dict(r) for r in rows]


@app.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT d.device_id, d.device_name, d.equipment_type, d.area,
                   d.engineer_id, d.baseline_temperature, d.baseline_vibration,
                   d.baseline_rf_power, d.status
            FROM devices d WHERE d.device_id = ?;
        """, (device_id,))
        device = cur.fetchone()
        if not device:
            cur.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Device not found")

        cur.execute("""
            SELECT health_score, temperature, vibration, rf_power, time
            FROM sensor_data WHERE device_id = ?
            ORDER BY time DESC LIMIT 1;
        """, (device_id,))
        latest = cur.fetchone()

        result = dict(device)
        if latest:
            result.update(dict(latest))
        cur.close()
        conn.close()
    return result


@app.get("/api/devices/{device_id}/trend")
async def get_device_trend(device_id: str, hours: int = Query(default=24, ge=1, le=72)):
    since = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT time, temperature, vibration, rf_power, health_score
            FROM sensor_data
            WHERE device_id = ? AND time >= ?
            ORDER BY time ASC;
        """, (device_id, since))
        rows = cur.fetchall()

        cur.execute("""
            SELECT baseline_temperature, baseline_vibration, baseline_rf_power
            FROM devices WHERE device_id = ?;
        """, (device_id,))
        baseline = cur.fetchone()
        cur.close()
        conn.close()

    return {
        "device_id": device_id,
        "baselines": dict(baseline) if baseline else {},
        "data": [dict(r) for r in rows],
    }


@app.get("/api/devices/{device_id}/health-history")
async def get_health_history(device_id: str, hours: int = Query(default=24, ge=1, le=72)):
    since = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT time, health_score FROM sensor_data
            WHERE device_id = ? AND time >= ?
            ORDER BY time ASC;
        """, (device_id, since))
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return {"device_id": device_id, "data": [dict(r) for r in rows]}


@app.get("/api/work-orders")
async def list_work_orders(
    status: Optional[str] = Query(default=None),
    device_id: Optional[str] = Query(default=None),
):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        query = """
            SELECT wo.*, d.device_name, e.name as engineer_name
            FROM work_orders wo
            JOIN devices d ON wo.device_id = d.device_id
            LEFT JOIN engineers e ON wo.engineer_id = e.engineer_id
            WHERE 1=1
        """
        params = []
        if status:
            query += " AND wo.status = ?"
            params.append(status)
        if device_id:
            query += " AND wo.device_id = ?"
            params.append(device_id)
        query += " ORDER BY wo.created_at DESC;"
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return [dict(r) for r in rows]


@app.post("/api/work-orders/{order_id}/accept")
async def accept_work_order(order_id: int):
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE work_orders SET status = 'accepted', accepted_at = ?
            WHERE id = ? AND status = 'pending' RETURNING id;
        """, (now_str, order_id))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
    if not result:
        raise HTTPException(status_code=400, detail="工单不存在或状态不允许接单")
    return {"message": "工单已接单", "order_id": order_id}


@app.post("/api/work-orders/{order_id}/complete")
async def complete_work_order(order_id: int):
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE work_orders SET status = 'completed', completed_at = ?
            WHERE id = ? AND status = 'accepted' RETURNING id;
        """, (now_str, order_id))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
    if not result:
        raise HTTPException(status_code=400, detail="工单不存在或状态不允许完成")
    return {"message": "工单已完成", "order_id": order_id}


@app.post("/api/work-orders/{order_id}/verify")
async def verify_work_order(order_id: int):
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE work_orders SET status = 'verified', verified_at = ?
            WHERE id = ? AND status = 'completed' RETURNING id;
        """, (now_str, order_id))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
    if not result:
        raise HTTPException(status_code=400, detail="工单不存在或状态不允许验收")
    return {"message": "工单已验收", "order_id": order_id}


@app.get("/api/alerts")
async def list_alerts(
    status: Optional[str] = Query(default=None),
    device_id: Optional[str] = Query(default=None),
):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        query = """
            SELECT a.*, d.device_name
            FROM alerts a
            JOIN devices d ON a.device_id = d.device_id
            WHERE 1=1
        """
        params = []
        if status:
            query += " AND a.status = ?"
            params.append(status)
        if device_id:
            query += " AND a.device_id = ?"
            params.append(device_id)
        query += " ORDER BY a.started_at DESC LIMIT 200;"
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return [dict(r) for r in rows]


@app.get("/api/dashboard/summary")
async def dashboard_summary():
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) as total FROM devices;")
        total_devices = cur.fetchone()["total"]

        five_min_ago = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        cur.execute("""
            SELECT COUNT(DISTINCT device_id) as cnt FROM sensor_data WHERE time >= ?;
        """, (five_min_ago,))
        online_devices = cur.fetchone()["cnt"]

        cur.execute("""
            SELECT AVG(health_score) as avg_health FROM (
                SELECT health_score FROM sensor_data
                GROUP BY device_id HAVING time = MAX(time)
            );
        """)
        row = cur.fetchone()
        avg_health = row["avg_health"] if row and row["avg_health"] else 0

        cur.execute("SELECT COUNT(*) as cnt FROM work_orders WHERE status = 'pending';")
        pending_orders = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM alerts WHERE status = 'active';")
        active_alerts = cur.fetchone()["cnt"]

        cur.execute("""
            SELECT COUNT(*) as cnt FROM (
                SELECT device_id FROM sensor_data
                GROUP BY device_id HAVING time = MAX(time) AND health_score < 60
            );
        """)
        low_health = cur.fetchone()["cnt"]

        cur.close()
        conn.close()

    return {
        "total_devices": total_devices,
        "online_devices": online_devices,
        "avg_health_score": round(avg_health, 1),
        "pending_work_orders": pending_orders,
        "active_alerts": active_alerts,
        "low_health_devices": low_health,
    }


@app.websocket("/ws/alerts")
async def websocket_alerts(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    logger.info(f"WebSocket client connected. Total: {len(ws_clients)}")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.remove(websocket)
        logger.info(f"WebSocket client disconnected. Total: {len(ws_clients)}")


@app.get("/api/factories")
async def list_factories():
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM factories ORDER BY factory_id;")
        factories = cur.fetchall()
        result = []
        for f in factories:
            fd = dict(f)
            cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = ?;", (fd["factory_id"],))
            fd["device_count"] = cur.fetchone()[0]
            cur.execute("""
                SELECT AVG(health_score) FROM (
                    SELECT MAX(time) as mt, device_id FROM sensor_data
                    WHERE device_id IN (SELECT device_id FROM devices WHERE factory_id = ?)
                    GROUP BY device_id
                ) t
                JOIN sensor_data s ON t.device_id = s.device_id AND t.mt = s.time;
            """, (fd["factory_id"],))
            avg = cur.fetchone()[0]
            fd["avg_health"] = round(avg, 1) if avg else 0
            result.append(fd)
        cur.close()
        conn.close()
    return result


@app.get("/api/factories/aggregated")
async def list_factories_aggregated():
    with _db_lock:
        conn = get_connection()
        view = MultiSiteView(conn, 'sqlite')
        result = view.get_aggregated()
        conn.close()
    return result


@app.get("/api/factories/{factory_id}/summary")
async def factory_summary(factory_id: str):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM factories WHERE factory_id = ?;", (factory_id,))
        factory = cur.fetchone()
        if not factory:
            raise HTTPException(status_code=404, detail="Factory not found")

        cur.execute("SELECT COUNT(*) FROM devices WHERE factory_id = ?;", (factory_id,))
        total_devices = cur.fetchone()[0]

        five_min_ago = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        cur.execute("""
            SELECT COUNT(DISTINCT d.device_id) FROM devices d
            JOIN sensor_data s ON d.device_id = s.device_id
            WHERE d.factory_id = ? AND s.time >= ?;
        """, (factory_id, five_min_ago))
        online_devices = cur.fetchone()[0]

        cur.execute("""
            SELECT AVG(s.health_score) FROM (
                SELECT d.device_id, MAX(s.time) as mt FROM devices d
                LEFT JOIN sensor_data s ON d.device_id = s.device_id
                WHERE d.factory_id = ?
                GROUP BY d.device_id
            ) t
            LEFT JOIN sensor_data s ON t.device_id = s.device_id AND t.mt = s.time;
        """, (factory_id,))
        avg_health = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM work_orders WHERE factory_id = ? AND status = 'pending';", (factory_id,))
        pending_orders = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(DISTINCT d.device_id) FROM devices d
            JOIN (
                SELECT device_id, MAX(time) as mt FROM sensor_data GROUP BY device_id
            ) t ON d.device_id = t.device_id
            JOIN sensor_data s ON t.device_id = s.device_id AND t.mt = s.time
            WHERE d.factory_id = ? AND s.health_score < 60;
        """, (factory_id,))
        low_health = cur.fetchone()[0]

        cur.close()
        conn.close()
    return {
        "factory": dict(factory),
        "total_devices": total_devices,
        "online_devices": online_devices,
        "avg_health_score": round(avg_health, 1),
        "pending_work_orders": pending_orders,
        "low_health_devices": low_health,
    }


@app.get("/api/factories/{factory_id}/devices")
async def list_factory_devices(factory_id: str):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT d.device_id, d.device_name, d.equipment_type, d.area,
                   d.engineer_id, d.baseline_temperature, d.baseline_vibration,
                   d.baseline_rf_power, d.status,
                   s.health_score, s.temperature, s.vibration, s.rf_power, s.time
            FROM devices d
            LEFT JOIN (
                SELECT device_id, health_score, temperature, vibration, rf_power, time
                FROM sensor_data
                GROUP BY device_id
                HAVING time = MAX(time)
            ) s ON d.device_id = s.device_id
            WHERE d.factory_id = ?
            ORDER BY d.device_id;
        """, (factory_id,))
        devices = cur.fetchall()
        cur.close()
        conn.close()
    return [dict(d) for d in devices]


@app.get("/api/spare-parts")
async def list_spare_parts(equipment_type: Optional[str] = None):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        query = "SELECT * FROM spare_parts"
        params = []
        if equipment_type:
            query += " WHERE equipment_type = ?"
            params.append(equipment_type)
        query += " ORDER BY equipment_type, part_id;"
        cur.execute(query, params)
        parts = cur.fetchall()
        cur.close()
        conn.close()
    return [dict(p) for p in parts]


@app.get("/api/spare-parts/{part_id}")
async def get_spare_part(part_id: str):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM spare_parts WHERE part_id = ?;", (part_id,))
        part = cur.fetchone()
        cur.close()
        conn.close()
    if not part:
        raise HTTPException(status_code=404, detail="Spare part not found")
    return dict(part)


@app.get("/api/purchase-orders")
async def list_purchase_orders(status: Optional[str] = None):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        query = """
            SELECT po.*, sp.part_name, sp.equipment_type, f.factory_name
            FROM purchase_orders po
            JOIN spare_parts sp ON po.part_id = sp.part_id
            LEFT JOIN factories f ON po.factory_id = f.factory_id
            WHERE 1=1
        """
        params = []
        if status:
            query += " AND po.status = ?"
            params.append(status)
        query += " ORDER BY po.created_at DESC LIMIT 200;"
        cur.execute(query, params)
        pos = cur.fetchall()
        cur.close()
        conn.close()
    return [dict(po) for po in pos]


@app.post("/api/purchase-orders/{po_id}/approve")
async def approve_purchase_order(po_id: int):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE purchase_orders SET status = 'approved', approved_at = ?
            WHERE id = ? AND status = 'pending';
        """, (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"), po_id))
        conn.commit()
        updated = cur.rowcount
        cur.close()
        conn.close()
    if not updated:
        raise HTTPException(status_code=400, detail="采购单不存在或状态不允许审批")
    return {"message": "采购单已审批", "po_id": po_id}


@app.post("/api/purchase-orders/{po_id}/receive")
async def receive_purchase_order(po_id: int):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT part_id, quantity FROM purchase_orders
            WHERE id = ? AND status = 'approved';
        """, (po_id,))
        po = cur.fetchone()
        if not po:
            cur.close()
            conn.close()
            raise HTTPException(status_code=400, detail="采购单不存在或状态不允许收货")

        cur.execute("""
            UPDATE purchase_orders SET status = 'received', received_at = ?
            WHERE id = ?;
        """, (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"), po_id))

        cur.execute("""
            UPDATE spare_parts SET stock_quantity = stock_quantity + ?
            WHERE part_id = ?;
        """, (po["quantity"], po["part_id"]))
        conn.commit()
        cur.close()
        conn.close()
    return {"message": "采购单已收货，库存已更新", "po_id": po_id}


@app.get("/api/devices/{device_id}/life-prediction")
async def get_device_life_prediction(device_id: str, hours: int = Query(default=72, ge=24, le=168)):
    from health_evaluator import predict_remaining_life
    with _db_lock:
        conn = get_connection()
        result = predict_remaining_life(device_id, conn, 'sqlite', hours=hours)
        conn.close()
    return result


@app.get("/api/factories/{factory_id}/life-predictions")
async def get_factory_life_predictions(factory_id: str, hours: int = Query(default=72, ge=24, le=168)):
    from health_evaluator import predict_remaining_life
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT device_id FROM devices WHERE factory_id = ?;", (factory_id,))
        devices = [row[0] for row in cur.fetchall()]
        cur.close()

        results = {}
        for device_id in devices:
            results[device_id] = predict_remaining_life(device_id, conn, 'sqlite', hours=hours)
        conn.close()
    return {"factory_id": factory_id, "predictions": results}


@app.get("/api/engineers")
async def list_engineers(factory_id: Optional[str] = None):
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        query = """
            SELECT e.*,
                   (SELECT COUNT(*) FROM work_orders wo
                    WHERE wo.engineer_id = e.engineer_id
                      AND wo.status IN ('pending', 'accepted')) as pending_work_orders
            FROM engineers e
        """
        params = []
        if factory_id:
            query += " WHERE e.factory_id = ?"
            params.append(factory_id)
        query += " ORDER BY e.engineer_id;"
        cur.execute(query, params)
        engineers = cur.fetchall()
        cur.close()
        conn.close()
    return [dict(e) for e in engineers]


@app.get("/api/engineers/{engineer_id}/workload")
async def get_engineer_workload(engineer_id: str):
    from workorder_engine import get_engineer_workload
    with _db_lock:
        conn = get_connection()
        result = get_engineer_workload(engineer_id, conn, 'sqlite')
        conn.close()
    return result


@app.get("/api/work-orders/{order_id}/recommend-engineer")
async def recommend_engineer_for_order(order_id: int):
    from workorder_engine import recommend_engineer
    with _db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT d.equipment_type, d.factory_id FROM work_orders wo
            JOIN devices d ON wo.device_id = d.device_id
            WHERE wo.id = ?;
        """, (order_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Work order not found")
        recommended_id, reason = recommend_engineer(row["equipment_type"], row["factory_id"], conn, 'sqlite')
        conn.close()
    return {
        "recommended_engineer_id": recommended_id,
        "assignment_reason": reason,
        "equipment_type": row["equipment_type"],
        "factory_id": row["factory_id"]
    }


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
