import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "generated"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "fab_maintenance")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [API] %(message)s")
logger = logging.getLogger(__name__)


def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
    )


ws_clients: List[WebSocket] = []


async def broadcast_alert(alert_data: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(alert_data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


def sync_broadcast_alert(alert_data: dict):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast_alert(alert_data))
    except RuntimeError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    import grpc_server
    grpc_server.set_alert_callback(sync_broadcast_alert)
    yield


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
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT d.device_id, d.device_name, d.equipment_type, d.area,
               d.engineer_id, d.baseline_temperature, d.baseline_vibration,
               d.baseline_rf_power, d.status,
               s.health_score, s.temperature, s.vibration, s.rf_power, s.time
        FROM devices d
        LEFT JOIN LATERAL (
            SELECT health_score, temperature, vibration, rf_power, time
            FROM sensor_data
            WHERE device_id = d.device_id
            ORDER BY time DESC LIMIT 1
        ) s ON true
        ORDER BY d.device_id;
    """)
    devices = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(d) for d in devices]


@app.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT d.device_id, d.device_name, d.equipment_type, d.area,
               d.engineer_id, d.baseline_temperature, d.baseline_vibration,
               d.baseline_rf_power, d.status,
               s.health_score, s.temperature, s.vibration, s.rf_power, s.time
        FROM devices d
        LEFT JOIN LATERAL (
            SELECT health_score, temperature, vibration, rf_power, time
            FROM sensor_data
            WHERE device_id = d.device_id
            ORDER BY time DESC LIMIT 1
        ) s ON true
        WHERE d.device_id = %s;
    """, (device_id,))
    device = cur.fetchone()
    cur.close()
    conn.close()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return dict(device)


@app.get("/api/devices/{device_id}/trend")
async def get_device_trend(
    device_id: str,
    hours: int = Query(default=24, ge=1, le=72),
):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    since = datetime.utcnow() - timedelta(hours=hours)
    cur.execute("""
        SELECT time, temperature, vibration, rf_power, health_score
        FROM sensor_data
        WHERE device_id = %s AND time >= %s
        ORDER BY time ASC;
    """, (device_id, since))
    rows = cur.fetchall()

    cur.execute("""
        SELECT baseline_temperature, baseline_vibration, baseline_rf_power
        FROM devices WHERE device_id = %s;
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
async def get_health_history(
    device_id: str,
    hours: int = Query(default=24, ge=1, le=72),
):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    since = datetime.utcnow() - timedelta(hours=hours)
    cur.execute("""
        SELECT time, health_score
        FROM health_score_history
        WHERE device_id = %s AND time >= %s
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
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT wo.*, d.device_name, e.name as engineer_name
        FROM work_orders wo
        JOIN devices d ON wo.device_id = d.device_id
        LEFT JOIN engineers e ON wo.engineer_id = e.engineer_id
        WHERE 1=1
    """
    params = []
    if status:
        query += " AND wo.status = %s"
        params.append(status)
    if device_id:
        query += " AND wo.device_id = %s"
        params.append(device_id)
    query += " ORDER BY wo.created_at DESC;"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/work-orders/{order_id}/accept")
async def accept_work_order(order_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE work_orders SET status = 'accepted', accepted_at = %s
        WHERE id = %s AND status = 'pending' RETURNING id;
    """, (datetime.utcnow(), order_id))
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not result:
        raise HTTPException(status_code=400, detail="工单不存在或状态不允许接单")
    return {"message": "工单已接单", "order_id": order_id}


@app.post("/api/work-orders/{order_id}/complete")
async def complete_work_order(order_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE work_orders SET status = 'completed', completed_at = %s
        WHERE id = %s AND status = 'accepted' RETURNING id;
    """, (datetime.utcnow(), order_id))
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not result:
        raise HTTPException(status_code=400, detail="工单不存在或状态不允许完成")
    return {"message": "工单已完成", "order_id": order_id}


@app.post("/api/work-orders/{order_id}/verify")
async def verify_work_order(order_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE work_orders SET status = 'verified', verified_at = %s
        WHERE id = %s AND status = 'completed' RETURNING id;
    """, (datetime.utcnow(), order_id))
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
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT a.*, d.device_name
        FROM alerts a
        JOIN devices d ON a.device_id = d.device_id
        WHERE 1=1
    """
    params = []
    if status:
        query += " AND a.status = %s"
        params.append(status)
    if device_id:
        query += " AND a.device_id = %s"
        params.append(device_id)
    query += " ORDER BY a.started_at DESC LIMIT 200;"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/dashboard/summary")
async def dashboard_summary():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT COUNT(*) as total FROM devices;")
    total_devices = cur.fetchone()["total"]

    cur.execute("""
        SELECT COUNT(DISTINCT device_id) as cnt
        FROM sensor_data
        WHERE time >= NOW() - INTERVAL '5 minutes';
    """)
    online_devices = cur.fetchone()["cnt"]

    cur.execute("""
        SELECT AVG(health_score) as avg_health
        FROM (
            SELECT DISTINCT ON (device_id) health_score
            FROM sensor_data ORDER BY device_id, time DESC
        ) sub;
    """)
    avg_health = cur.fetchone()["avg_health"] or 0

    cur.execute("SELECT COUNT(*) as cnt FROM work_orders WHERE status = 'pending';")
    pending_orders = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) as cnt FROM alerts WHERE status = 'active';")
    active_alerts = cur.fetchone()["cnt"]

    cur.execute("""
        SELECT COUNT(DISTINCT device_id) as cnt FROM (
            SELECT device_id, health_score
            FROM (
                SELECT DISTINCT ON (device_id) device_id, health_score
                FROM sensor_data ORDER BY device_id, time DESC
            ) sub
            WHERE health_score < 60
        ) low;
    """)
    low_health_devices = cur.fetchone()["cnt"]

    cur.close()
    conn.close()

    return {
        "total_devices": total_devices,
        "online_devices": online_devices,
        "avg_health_score": round(avg_health, 1),
        "pending_work_orders": pending_orders,
        "active_alerts": active_alerts,
        "low_health_devices": low_health_devices,
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
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT f.*,
               (SELECT COUNT(*) FROM devices d WHERE d.factory_id = f.factory_id) as device_count,
               (SELECT AVG(s.health_score) FROM devices d
                LEFT JOIN LATERAL (
                    SELECT health_score FROM sensor_data
                    WHERE device_id = d.device_id ORDER BY time DESC LIMIT 1
                ) s ON true
                WHERE d.factory_id = f.factory_id) as avg_health
        FROM factories f
        ORDER BY f.factory_id;
    """)
    factories = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(f) for f in factories]


@app.get("/api/factories/{factory_id}/summary")
async def factory_summary(factory_id: str):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM factories WHERE factory_id = %s;", (factory_id,))
    factory = cur.fetchone()
    if not factory:
        raise HTTPException(status_code=404, detail="Factory not found")

    cur.execute("""
        SELECT COUNT(*) as total FROM devices WHERE factory_id = %s;
    """, (factory_id,))
    total_devices = cur.fetchone()["total"]

    cur.execute("""
        SELECT COUNT(DISTINCT device_id) as cnt
        FROM sensor_data s
        JOIN devices d ON s.device_id = d.device_id
        WHERE d.factory_id = %s AND s.time >= NOW() - INTERVAL '5 minutes';
    """, (factory_id,))
    online_devices = cur.fetchone()["cnt"]

    cur.execute("""
        SELECT AVG(health_score) as avg_health
        FROM (
            SELECT DISTINCT ON (d.device_id) s.health_score
            FROM devices d
            LEFT JOIN sensor_data s ON d.device_id = s.device_id
            WHERE d.factory_id = %s
            ORDER BY d.device_id, s.time DESC
        ) sub;
    """, (factory_id,))
    avg_health = cur.fetchone()["avg_health"] or 0

    cur.execute("""
        SELECT COUNT(*) as cnt FROM work_orders
        WHERE factory_id = %s AND status = 'pending';
    """, (factory_id,))
    pending_orders = cur.fetchone()["cnt"]

    cur.execute("""
        SELECT COUNT(DISTINCT d.device_id) as cnt
        FROM devices d
        LEFT JOIN LATERAL (
            SELECT health_score FROM sensor_data
            WHERE device_id = d.device_id ORDER BY time DESC LIMIT 1
        ) s ON true
        WHERE d.factory_id = %s AND s.health_score < 60;
    """, (factory_id,))
    low_health = cur.fetchone()["cnt"]

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
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT d.device_id, d.device_name, d.equipment_type, d.area,
               d.engineer_id, d.baseline_temperature, d.baseline_vibration,
               d.baseline_rf_power, d.status,
               s.health_score, s.temperature, s.vibration, s.rf_power, s.time
        FROM devices d
        LEFT JOIN LATERAL (
            SELECT health_score, temperature, vibration, rf_power, time
            FROM sensor_data
            WHERE device_id = d.device_id
            ORDER BY time DESC LIMIT 1
        ) s ON true
        WHERE d.factory_id = %s
        ORDER BY d.device_id;
    """, (factory_id,))
    devices = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(d) for d in devices]


@app.get("/api/spare-parts")
async def list_spare_parts(equipment_type: Optional[str] = None):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = "SELECT * FROM spare_parts"
    params = []
    if equipment_type:
        query += " WHERE equipment_type = %s"
        params.append(equipment_type)
    query += " ORDER BY equipment_type, part_id;"
    cur.execute(query, params)
    parts = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(p) for p in parts]


@app.get("/api/spare-parts/{part_id}")
async def get_spare_part(part_id: str):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM spare_parts WHERE part_id = %s;", (part_id,))
    part = cur.fetchone()
    cur.close()
    conn.close()
    if not part:
        raise HTTPException(status_code=404, detail="Spare part not found")
    return dict(part)


@app.get("/api/purchase-orders")
async def list_purchase_orders(status: Optional[str] = None):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT po.*, sp.part_name, sp.equipment_type, f.factory_name
        FROM purchase_orders po
        JOIN spare_parts sp ON po.part_id = sp.part_id
        LEFT JOIN factories f ON po.factory_id = f.factory_id
        WHERE 1=1
    """
    params = []
    if status:
        query += " AND po.status = %s"
        params.append(status)
    query += " ORDER BY po.created_at DESC LIMIT 200;"
    cur.execute(query, params)
    pos = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(po) for po in pos]


@app.post("/api/purchase-orders/{po_id}/approve")
async def approve_purchase_order(po_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE purchase_orders SET status = 'approved', approved_at = %s
        WHERE id = %s AND status = 'pending' RETURNING id;
    """, (datetime.utcnow(), po_id))
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not result:
        raise HTTPException(status_code=400, detail="采购单不存在或状态不允许审批")
    return {"message": "采购单已审批", "po_id": po_id}


@app.post("/api/purchase-orders/{po_id}/receive")
async def receive_purchase_order(po_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT part_id, quantity FROM purchase_orders
        WHERE id = %s AND status = 'approved';
    """, (po_id,))
    po = cur.fetchone()
    if not po:
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="采购单不存在或状态不允许收货")

    cur.execute("""
        UPDATE purchase_orders SET status = 'received', received_at = %s
        WHERE id = %s RETURNING id;
    """, (datetime.utcnow(), po_id))

    cur.execute("""
        UPDATE spare_parts SET stock_quantity = stock_quantity + %s
        WHERE part_id = %s;
    """, (po[1], po[0]))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "采购单已收货，库存已更新", "po_id": po_id}


@app.get("/api/devices/{device_id}/life-prediction")
async def get_device_life_prediction(device_id: str, hours: int = Query(default=72, ge=24, le=168)):
    from health_evaluator import predict_remaining_life
    conn = get_connection()
    result = predict_remaining_life(device_id, conn, 'postgres', hours=hours)
    conn.close()
    return result


@app.get("/api/factories/{factory_id}/life-predictions")
async def get_factory_life_predictions(factory_id: str, hours: int = Query(default=72, ge=24, le=168)):
    from health_evaluator import predict_remaining_life
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT device_id FROM devices WHERE factory_id = %s;", (factory_id,))
    devices = [row[0] for row in cur.fetchall()]
    cur.close()

    results = {}
    for device_id in devices:
        results[device_id] = predict_remaining_life(device_id, conn, 'postgres', hours=hours)
    conn.close()
    return {"factory_id": factory_id, "predictions": results}


@app.get("/api/engineers")
async def list_engineers(factory_id: Optional[str] = None):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT e.*,
               (SELECT COUNT(*) FROM work_orders wo
                WHERE wo.engineer_id = e.engineer_id
                  AND wo.status IN ('pending', 'accepted')) as pending_work_orders
        FROM engineers e
    """
    params = []
    if factory_id:
        query += " WHERE e.factory_id = %s"
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
    conn = get_connection()
    result = get_engineer_workload(engineer_id, conn, 'postgres')
    conn.close()
    return result


@app.get("/api/work-orders/{order_id}/recommend-engineer")
async def recommend_engineer_for_order(order_id: int):
    from workorder_engine import recommend_engineer
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT d.equipment_type, d.factory_id FROM work_orders wo
        JOIN devices d ON wo.device_id = d.device_id
        WHERE wo.id = %s;
    """, (order_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Work order not found")
    recommended_id, reason = recommend_engineer(row["equipment_type"], row["factory_id"], conn, 'postgres')
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
