import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)

ENGINEER_MAX_LOAD = 5
FACTORY_PENALTY = 0.3


def _ph(db_type: str) -> str:
    return "%s" if db_type == "postgres" else "?"


class ShiftAllocator:
    def __init__(self, conn, db_type: str):
        self.conn = conn
        self.db_type = db_type
        self.p = _ph(db_type)

    def get_workload(self, engineer_id: str) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT COUNT(*) FROM work_orders
            WHERE engineer_id = {self.p} AND status IN ('pending', 'accepted');
        """, (engineer_id,))
        pending_count = cur.fetchone()[0] or 0

        if self.db_type == "postgres":
            cur.execute(f"""
                SELECT COUNT(*) FROM work_orders
                WHERE engineer_id = {self.p} AND status = 'completed'
                  AND completed_at >= NOW() - INTERVAL '7 days';
            """, (engineer_id,))
        else:
            seven_days_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
            cur.execute(f"""
                SELECT COUNT(*) FROM work_orders
                WHERE engineer_id = {self.p} AND status = 'completed'
                  AND completed_at >= {self.p};
            """, (engineer_id, seven_days_ago))
        completed_last_7d = cur.fetchone()[0] or 0
        cur.close()

        return {
            "engineer_id": engineer_id,
            "pending": pending_count,
            "completed_last_7d": completed_last_7d,
            "load_ratio": min(1.0, pending_count / ENGINEER_MAX_LOAD)
        }

    def recommend(
        self, equipment_type: str, factory_id: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT e.engineer_id, e.name, e.specialty, e.factory_id, f.factory_name
            FROM engineers e
            LEFT JOIN factories f ON e.factory_id = f.factory_id
            WHERE e.specialty = {self.p}
            ORDER BY CASE WHEN e.factory_id = {self.p} THEN 0 ELSE 1 END;
        """, (equipment_type, factory_id if factory_id else ""))

        rows = cur.fetchall()
        if not rows:
            cur.close()
            return None, None

        candidates = []
        for row in rows:
            eng_id = row[0]
            eng_name = row[1]
            eng_factory = row[3]
            factory_name = row[4] if row[4] else eng_factory
            is_same_factory = (eng_factory == factory_id) if factory_id else True

            workload = self.get_workload(eng_id)
            if workload["pending"] < ENGINEER_MAX_LOAD:
                completion_rate = min(1.0, workload["completed_last_7d"] / 20.0)
                base_score = (1.0 - workload["load_ratio"]) * 0.7 + completion_rate * 0.3
                if is_same_factory:
                    adjusted_score = base_score
                    factory_note = "本厂区"
                else:
                    adjusted_score = base_score * FACTORY_PENALTY
                    factory_note = f"跨厂区({factory_name})"

                candidates.append((eng_id, eng_name, adjusted_score, workload, factory_note))

        cur.close()
        if not candidates:
            return None, "该专业所有工程师均已达负载上限"

        candidates.sort(key=lambda x: x[2], reverse=True)
        best = candidates[0]
        reason = (
            f"推荐 {best[1]}({best[0]}) - {best[4]}，专业匹配：{equipment_type}，"
            f"当前待处理工单：{best[3]['pending']}/{ENGINEER_MAX_LOAD}，"
            f"近7天完成：{best[3]['completed_last_7d']} 单，"
            f"综合得分：{best[2]:.2f}"
        )
        return best[0], reason

    def get_engineer_info(self, engineer_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT e.engineer_id, e.name, e.specialty, e.factory_id, f.factory_name
            FROM engineers e
            LEFT JOIN factories f ON e.factory_id = f.factory_id
            WHERE e.engineer_id = {self.p};
        """, (engineer_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None

        workload = self.get_workload(engineer_id)
        return {
            "engineer_id": row[0],
            "name": row[1],
            "specialty": row[2],
            "factory_id": row[3],
            "factory_name": row[4],
            "workload": workload
        }

    def list_engineers(
        self, specialty: Optional[str] = None, factory_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        query = """
            SELECT e.engineer_id, e.name, e.specialty, e.factory_id, f.factory_name
            FROM engineers e
            LEFT JOIN factories f ON e.factory_id = f.factory_id
            WHERE 1=1
        """
        params = []
        if specialty:
            query += f" AND e.specialty = {self.p}"
            params.append(specialty)
        if factory_id:
            query += f" AND e.factory_id = {self.p}"
            params.append(factory_id)
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()

        result = []
        for row in rows:
            workload = self.get_workload(row[0])
            result.append({
                "engineer_id": row[0],
                "name": row[1],
                "specialty": row[2],
                "factory_id": row[3],
                "factory_name": row[4],
                "workload": workload
            })
        return result

    def assign_work_order(
        self, work_order_id: int, engineer_id: str
    ) -> bool:
        cur = self.conn.cursor()
        if self.db_type == "postgres":
            cur.execute(f"""
                UPDATE work_orders SET engineer_id = {self.p}, status = 'accepted',
                    accepted_at = NOW()
                WHERE id = {self.p} AND status = 'pending'
                RETURNING id;
            """, (engineer_id, work_order_id))
        else:
            now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
            cur.execute(f"""
                UPDATE work_orders SET engineer_id = {self.p}, status = 'accepted',
                    accepted_at = {self.p}
                WHERE id = {self.p} AND status = 'pending';
            """, (engineer_id, now, work_order_id))
        success = cur.rowcount > 0
        self.conn.commit()
        cur.close()
        return success
