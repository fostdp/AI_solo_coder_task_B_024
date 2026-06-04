import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any
from health_evaluator import is_maintenance_window, _ph, _now_val, _parse_time
from managers.inventory_manager import InventoryManager
from managers.shift_allocator import ShiftAllocator

logger = logging.getLogger(__name__)

ENGINEER_MAX_LOAD = 5
PURCHASE_QUANTITY_FACTOR = 3


def check_work_order(device_id, health_score, conn, db_type, alert_callback):
    if is_maintenance_window():
        return
    if health_score >= 60:
        return

    p = _ph(db_type)
    now = _now_val(db_type)
    cur = conn.cursor()

    if db_type == 'postgres':
        cur.execute(f"""
            SELECT time FROM sensor_data
            WHERE device_id = {p} AND health_score < 60 AND time >= NOW() - INTERVAL '1 hour 5 minutes'
            ORDER BY time ASC LIMIT 1;
        """, (device_id,))
    else:
        cur.execute(f"""
            SELECT time FROM sensor_data
            WHERE device_id = {p} AND health_score < 60 AND time >= datetime('now', '-65 minutes')
            ORDER BY time ASC LIMIT 1;
        """, (device_id,))
    first_low = cur.fetchone()

    if not first_low:
        cur.close()
        return

    first_dt = _parse_time(first_low[0], db_type)
    elapsed_low = (datetime.utcnow() - first_dt).total_seconds()
    if elapsed_low < 3600:
        cur.close()
        return

    if db_type == 'postgres':
        cur.execute(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN health_score >= 60 THEN 1 ELSE 0 END) as recovered
            FROM sensor_data
            WHERE device_id = {p} AND time >= {p} AND time <= NOW();
        """, (device_id, first_low[0]))
        row = cur.fetchone()
        total_in_range = row[0]
        recovered_count = row[1] or 0
    else:
        cur.execute(f"""
            SELECT COUNT(*) as total FROM sensor_data
            WHERE device_id = {p} AND time >= ?;
        """, (device_id, first_low[0]))
        total_in_range = cur.fetchone()[0]

        cur.execute(f"""
            SELECT COUNT(*) as recovered FROM sensor_data
            WHERE device_id = {p} AND health_score >= 60 AND time >= ?;
        """, (device_id, first_low[0]))
        recovered_count = cur.fetchone()[0]

    if total_in_range > 0 and recovered_count > total_in_range * 0.1:
        cur.close()
        return

    cur.execute(f"""
        SELECT id FROM work_orders
        WHERE device_id = {p} AND status IN ('pending', 'accepted')
        LIMIT 1;
    """, (device_id,))
    if cur.fetchone():
        cur.close()
        return

    if db_type == 'postgres':
        cur.execute(f"""
            SELECT id FROM work_orders
            WHERE device_id = {p}
              AND created_at >= NOW() - INTERVAL '24 hours'
              AND reason LIKE '%健康评分%'
            LIMIT 1;
        """, (device_id,))
    else:
        cur.execute(f"""
            SELECT id FROM work_orders
            WHERE device_id = {p}
              AND created_at >= datetime('now', '-24 hours')
              AND reason LIKE '%健康评分%'
            LIMIT 1;
        """, (device_id,))
    if cur.fetchone():
        cur.close()
        return

    cur.execute(f"""
        SELECT engineer_id, factory_id, equipment_type
        FROM devices WHERE device_id = {p};
    """, (device_id,))
    dev_row = cur.fetchone()
    engineer_id = dev_row[0] if dev_row else None
    factory_id = dev_row[1] if dev_row and len(dev_row) > 1 else None
    equipment_type = dev_row[2] if dev_row and len(dev_row) > 2 else None

    recommended_eng, assign_reason = recommend_engineer(
        equipment_type, factory_id, conn, db_type
    ) if equipment_type else (None, None)

    spare_part, stock_warning = check_spare_parts(
        equipment_type, conn, db_type
    ) if equipment_type else (None, None)

    if spare_part and stock_warning:
        create_purchase_order(
            spare_part["part_id"], factory_id,
            spare_part["safe_stock_level"] * PURCHASE_QUANTITY_FACTOR - spare_part["stock_quantity"],
            f"设备 {device_id} 生成维保工单，{spare_part['part_name']} 库存不足（当前：{spare_part['stock_quantity']}，安全库存：{spare_part['safe_stock_level']}）",
            conn, db_type, alert_callback,
            expected_version=spare_part.get("version")
        )

    reason = f"设备健康评分连续1小时低于60分（当前：{health_score}），自动生成维保工单"
    cur.execute(f"""
        INSERT INTO work_orders (device_id, factory_id, engineer_id, recommended_engineer_id,
            spare_part_id, reason, assignment_reason, status, created_at)
        VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p});
    """, (device_id, factory_id, engineer_id, recommended_eng,
          spare_part["part_id"] if spare_part else None,
          reason, assign_reason, 'pending', now))
    cur.close()
    conn.commit()

    if alert_callback:
        alert_data = {
            "type": "work_order",
            "device_id": device_id,
            "engineer_id": engineer_id,
            "recommended_engineer_id": recommended_eng,
            "health_score": health_score,
            "message": f"设备 {device_id} 健康评分 {health_score} 连续低于60超1小时，已自动创建维保工单"
        }
        if stock_warning:
            alert_data["spare_part_warning"] = stock_warning
        if recommended_eng:
            alert_data["assignment_reason"] = assign_reason
        alert_callback(alert_data)


def get_engineer_workload(engineer_id: str, conn, db_type: str) -> Dict[str, Any]:
    allocator = ShiftAllocator(conn, db_type)
    return allocator.get_workload(engineer_id)


def recommend_engineer(equipment_type: str, factory_id: Optional[str],
                        conn, db_type: str) -> Tuple[Optional[str], Optional[str]]:
    allocator = ShiftAllocator(conn, db_type)
    return allocator.recommend(equipment_type, factory_id)


def check_spare_parts(equipment_type: str, conn, db_type: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    manager = InventoryManager(conn, db_type)
    return manager.check_stock(equipment_type)


def create_purchase_order(part_id: str, factory_id: Optional[str],
                           quantity: int, reason: str, conn, db_type: str,
                           alert_callback=None, expected_version: Optional[int] = None) -> Optional[int]:
    manager = InventoryManager(conn, db_type)
    return manager.create_purchase_order(
        part_id, factory_id, quantity, reason, alert_callback, expected_version
    )
