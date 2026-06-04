import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


def _ph(db_type: str) -> str:
    return "%s" if db_type == "postgres" else "?"


class MultiSiteView:
    def __init__(self, conn, db_type: str):
        self.conn = conn
        self.db_type = db_type
        self.p = _ph(db_type)

    def list_factories(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM factories ORDER BY factory_id;")
        factories = cur.fetchall()
        result = []
        for f in factories:
            fd = dict(f)
            cur.execute(
                "SELECT COUNT(*) FROM devices WHERE factory_id = ?;",
                (fd["factory_id"],)
            )
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
        return result

    def get_aggregated(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        try:
            if self.db_type == "postgres":
                cur.execute("""
                    SELECT f.*,
                           COUNT(d.device_id) as device_count,
                           ROUND(AVG(s.health_score), 1) as avg_health,
                           CASE
                               WHEN f.lat >= 30 THEN '华北地区'
                               WHEN f.lat >= 25 THEN '华东地区'
                               ELSE '华南地区'
                           END as region
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
                    ORDER BY region, f.factory_id;
                """)
            else:
                cur.execute("""
                    SELECT f.factory_id, f.factory_name, f.address, f.lat, f.lng, f.status,
                           COUNT(d.device_id) as device_count,
                           ROUND(AVG(s.health_score), 1) as avg_health,
                           CASE
                               WHEN f.lat >= 30 THEN '华北地区'
                               WHEN f.lat >= 25 THEN '华东地区'
                               ELSE '华南地区'
                           END as region
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
                    GROUP BY f.factory_id
                    ORDER BY region, f.factory_id;
                """)

            factories = cur.fetchall()
            regions = {}
            for f in factories:
                fd = dict(f)
                region = fd.pop('region')
                if region not in regions:
                    regions[region] = {
                        'region': region,
                        'factory_count': 0,
                        'total_devices': 0,
                        'avg_health': 0,
                        'factories': [],
                        'center_lat': 0,
                        'center_lng': 0
                    }
                regions[region]['factories'].append(fd)
                regions[region]['factory_count'] += 1
                regions[region]['total_devices'] += fd.get('device_count', 0) or 0
                regions[region]['avg_health'] += (fd.get('avg_health', 0) or 0) * (fd.get('device_count', 0) or 0)
                regions[region]['center_lat'] += fd.get('lat', 0) or 0
                regions[region]['center_lng'] += fd.get('lng', 0) or 0

            for region, data in regions.items():
                if data['total_devices'] > 0:
                    data['avg_health'] = round(data['avg_health'] / data['total_devices'], 1)
                else:
                    data['avg_health'] = 0
                if data['factory_count'] > 0:
                    data['center_lat'] = round(data['center_lat'] / data['factory_count'], 4)
                    data['center_lng'] = round(data['center_lng'] / data['factory_count'], 4)

            cur.close()
            return list(regions.values())
        except Exception as e:
            logger.exception(f"Failed to get aggregated data: {e}")
            cur.close()
            return []

    def get_factory_summary(self, factory_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM factories WHERE factory_id = ?;", (factory_id,))
        factory = cur.fetchone()
        if not factory:
            cur.close()
            return None

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

        cur.execute(
            "SELECT COUNT(*) FROM work_orders WHERE factory_id = ? AND status = 'pending';",
            (factory_id,)
        )
        pending_orders = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(DISTINCT d.device_id) FROM devices d
            JOIN sensor_data s ON d.device_id = s.device_id
            WHERE d.factory_id = ? AND s.health_score < 60;
        """, (factory_id,))
        low_health_devices = cur.fetchone()[0]

        cur.close()
        return {
            "factory": dict(factory),
            "total_devices": total_devices,
            "online_devices": online_devices,
            "avg_health": round(avg_health, 1),
            "pending_work_orders": pending_orders,
            "low_health_devices": low_health_devices
        }

    def get_factory_devices(self, factory_id: str) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT d.device_id, d.device_name, d.equipment_type, d.area, d.factory_id,
                   d.status, s.health_score
            FROM devices d
            LEFT JOIN (
                SELECT device_id, health_score, time FROM sensor_data
                GROUP BY device_id HAVING time = MAX(time)
            ) s ON d.device_id = s.device_id
            WHERE d.factory_id = ?
            ORDER BY d.device_id;
        """, (factory_id,))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    def get_alerts_summary(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM alerts WHERE status = 'active';")
        active_alerts = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM alerts WHERE status = 'active' AND alert_type = 'temperature';")
        temp_alerts = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM alerts WHERE status = 'active' AND alert_type = 'vibration';")
        vib_alerts = cur.fetchone()[0]
        cur.close()
        return {
            "active_alerts": active_alerts,
            "temperature_alerts": temp_alerts,
            "vibration_alerts": vib_alerts
        }

    def refresh_materialized_view(self) -> bool:
        if self.db_type != "postgres":
            return False
        cur = self.conn.cursor()
        try:
            cur.execute("REFRESH MATERIALIZED VIEW mv_factory_health_summary;")
            self.conn.commit()
            cur.close()
            return True
        except Exception as e:
            logger.warning(f"Materialized view refresh failed: {e}")
            cur.close()
            return False
