import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)

PURCHASE_QUANTITY_FACTOR = 3


def _ph(db_type: str) -> str:
    return "%s" if db_type == "postgres" else "?"


class InventoryManager:
    def __init__(self, conn, db_type: str):
        self.conn = conn
        self.db_type = db_type
        self.p = _ph(db_type)

    def check_stock(self, equipment_type: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT part_id, part_name, equipment_type, stock_quantity,
                   safe_stock_level, unit_price, supplier, version
            FROM spare_parts
            WHERE equipment_type = {self.p}
            ORDER BY stock_quantity ASC;
        """, (equipment_type,))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return None, None

        parts = []
        low_stock = []
        for row in rows:
            part = {
                "part_id": row[0],
                "part_name": row[1],
                "equipment_type": row[2],
                "stock_quantity": row[3],
                "safe_stock_level": row[4],
                "unit_price": row[5],
                "supplier": row[6],
                "version": row[7] if len(row) > 7 else 0,
            }
            parts.append(part)
            if row[3] < row[4]:
                low_stock.append(
                    f"{part['part_name']}(库存:{part['stock_quantity']}, 安全:{part['safe_stock_level']})"
                )

        if low_stock:
            return parts[0], f"{equipment_type} 备件库存不足：{', '.join(low_stock)}"
        return parts[0], None

    def create_purchase_order(
        self,
        part_id: str,
        factory_id: Optional[str],
        quantity: int,
        reason: str,
        alert_callback=None,
        expected_version: Optional[int] = None,
    ) -> Optional[int]:
        if quantity <= 0:
            return None

        cur = self.conn.cursor()

        if expected_version is not None:
            cur.execute(f"""
                SELECT version FROM spare_parts WHERE part_id = {self.p};
            """, (part_id,))
            row = cur.fetchone()
            if not row or row[0] != expected_version:
                cur.close()
                logger.warning(
                    f"Optimistic lock conflict for part {part_id}: "
                    f"expected version {expected_version}, current {row[0] if row else 'N/A'}"
                )
                return None

        cur.execute(f"""
            SELECT id FROM purchase_orders
            WHERE part_id = {self.p} AND status IN ('pending', 'approved', 'ordered')
            LIMIT 1;
        """, (part_id,))
        if cur.fetchone():
            cur.close()
            return None

        now = datetime.utcnow()
        if self.db_type == "postgres":
            if expected_version is not None:
                cur.execute(f"""
                    UPDATE spare_parts SET version = version + 1 
                    WHERE part_id = {self.p} AND version = {self.p};
                """, (part_id, expected_version))
                if cur.rowcount == 0:
                    cur.close()
                    return None
            cur.execute(f"""
                INSERT INTO purchase_orders (part_id, factory_id, quantity, reason, status, created_at)
                VALUES ({self.p}, {self.p}, {self.p}, {self.p}, {self.p}, NOW())
                RETURNING id;
            """, (part_id, factory_id, max(quantity, 1), reason, "pending"))
            po_id = cur.fetchone()[0]
            self.conn.commit()
            cur.close()
        else:
            if expected_version is not None:
                cur.execute(f"""
                    UPDATE spare_parts SET version = version + 1 
                    WHERE part_id = {self.p} AND version = {self.p};
                """, (part_id, expected_version))
                if cur.rowcount == 0:
                    cur.close()
                    return None
            cur.execute(f"""
                INSERT INTO purchase_orders (part_id, factory_id, quantity, reason, status, created_at)
                VALUES ({self.p}, {self.p}, {self.p}, {self.p}, {self.p}, {self.p});
            """, (
                part_id, factory_id, max(quantity, 1), reason, "pending",
                now.strftime("%Y-%m-%dT%H:%M:%S")
            ))
            po_id = cur.lastrowid
            cur.close()
            self.conn.commit()

        if alert_callback:
            try:
                alert_callback({
                    "type": "purchase_order",
                    "purchase_order_id": po_id,
                    "part_id": part_id,
                    "quantity": quantity,
                    "reason": reason,
                    "message": f"已自动生成采购申请 #{po_id}：{reason}"
                })
            except Exception:
                logger.exception("Purchase order alert callback failed")
        return po_id

    def list_parts(self, equipment_type: Optional[str] = None) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        query = "SELECT * FROM spare_parts"
        params = []
        if equipment_type:
            query += f" WHERE equipment_type = {self.p}"
            params.append(equipment_type)
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    def get_part(self, part_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT * FROM spare_parts WHERE part_id = {self.p};", (part_id,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None

    def update_stock(self, part_id: str, delta: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(f"""
            UPDATE spare_parts 
            SET stock_quantity = stock_quantity + {self.p}, version = version + 1
            WHERE part_id = {self.p};
        """, (delta, part_id))
        success = cur.rowcount > 0
        self.conn.commit()
        cur.close()
        return success

    def approve_po(self, po_id: int) -> bool:
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") if self.db_type != "postgres" else None
        cur = self.conn.cursor()
        if self.db_type == "postgres":
            cur.execute(f"""
                UPDATE purchase_orders SET status = 'approved', approved_at = NOW()
                WHERE id = {self.p} AND status = 'pending'
                RETURNING id;
            """, (po_id,))
        else:
            cur.execute(f"""
                UPDATE purchase_orders SET status = 'approved', approved_at = {self.p}
                WHERE id = {self.p} AND status = 'pending';
            """, (now, po_id))
        success = cur.rowcount > 0
        self.conn.commit()
        cur.close()
        return success

    def receive_po(self, po_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(f"SELECT part_id, quantity FROM purchase_orders WHERE id = {self.p};", (po_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return False

        part_id, quantity = row
        self.update_stock(part_id, quantity)

        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") if self.db_type != "postgres" else None
        if self.db_type == "postgres":
            cur.execute(f"""
                UPDATE purchase_orders SET status = 'received', received_at = NOW()
                WHERE id = {self.p};
            """, (po_id,))
        else:
            cur.execute(f"""
                UPDATE purchase_orders SET status = 'received', received_at = {self.p}
                WHERE id = {self.p};
            """, (now, po_id))
        self.conn.commit()
        cur.close()
        return True

    def list_purchase_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        query = "SELECT * FROM purchase_orders"
        params = []
        if status:
            query += f" WHERE status = {self.p}"
            params.append(status)
        query += " ORDER BY created_at DESC;"
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
