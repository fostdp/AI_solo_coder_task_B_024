import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)

LIFETIME_PREDICTION_WINDOW_HOURS = 72
MIN_DATA_POINTS_FOR_PREDICTION = 24
FAIL_THRESHOLD = 40.0


def _ph(db_type: str) -> str:
    return "%s" if db_type == "postgres" else "?"


def _linear_regression(x: List[float], y: List[float]) -> Tuple[float, float]:
    n = len(x)
    if n < 2:
        return 0.0, 0.0

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)

    denom = n * sum_x2 - sum_x * sum_x
    if abs(denom) < 1e-10:
        return 0.0, sum_y / n if n > 0 else 0.0

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


class LifePredictor:
    def __init__(self, conn, db_type: str):
        self.conn = conn
        self.db_type = db_type
        self.p = _ph(db_type)

    def _detect_maintenance_event(
        self, device_id: str, hours: int = 72
    ) -> Optional[datetime]:
        cur = self.conn.cursor()
        since_dt = datetime.utcnow() - timedelta(hours=hours)

        if self.db_type == "postgres":
            cur.execute(f"""
                SELECT completed_at FROM work_orders
                WHERE device_id = {self.p} AND status = 'completed' AND completed_at >= {self.p}
                ORDER BY completed_at DESC LIMIT 1;
            """, (device_id, since_dt))
        else:
            since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
            cur.execute(f"""
                SELECT completed_at FROM work_orders
                WHERE device_id = {self.p} AND status = 'completed' AND completed_at >= {self.p}
                ORDER BY completed_at DESC LIMIT 1;
            """, (device_id, since_str))

        row = cur.fetchone()
        cur.close()
        if row and row[0]:
            if isinstance(row[0], str):
                return datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S")
            return row[0]
        return None

    def _get_health_history(
        self, device_id: str, hours: int
    ) -> Tuple[List[Tuple[datetime, float]], bool]:
        cur = self.conn.cursor()

        maintenance_time = self._detect_maintenance_event(device_id, hours)
        post_maintenance = maintenance_time is not None
        if post_maintenance:
            window_start = maintenance_time
        else:
            window_start = datetime.utcnow() - timedelta(hours=hours)

        if self.db_type == "postgres":
            cur.execute(f"""
                SELECT time, health_score FROM health_score_history
                WHERE device_id = {self.p} AND time >= {self.p}
                ORDER BY time ASC;
            """, (device_id, window_start))
        else:
            since_str = window_start.strftime("%Y-%m-%dT%H:%M:%S")
            cur.execute(f"""
                SELECT time, health_score FROM sensor_data
                WHERE device_id = {self.p} AND time >= {self.p}
                ORDER BY time ASC;
            """, (device_id, since_str))

        rows = cur.fetchall()
        cur.close()
        result = []
        for row in rows:
            t = row[0]
            if isinstance(t, str):
                t = datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")
            hs = row[1]
            if hs is not None:
                result.append((t, hs))
        return result, post_maintenance

    def predict(
        self, device_id: str, hours: int = LIFETIME_PREDICTION_WINDOW_HOURS,
        fail_threshold: float = FAIL_THRESHOLD
    ) -> Dict[str, Any]:
        history, post_maintenance = self._get_health_history(device_id, hours)

        if len(history) < MIN_DATA_POINTS_FOR_PREDICTION:
            return {
                "device_id": device_id,
                "remaining_days": None,
                "confidence": 0.0,
                "data_points": len(history),
                "current_health": history[-1][1] if history else None,
                "slope": None,
                "intercept": None,
                "status": "insufficient_data",
                "post_maintenance_reset": post_maintenance
            }

        start_time = history[0][0]
        x_vals = []
        y_vals = []
        for t, hs in history:
            delta_hours = (t - start_time).total_seconds() / 3600.0
            x_vals.append(delta_hours)
            y_vals.append(hs)

        slope, intercept = _linear_regression(x_vals, y_vals)
        current_health = y_vals[-1]

        if slope >= 0:
            return {
                "device_id": device_id,
                "remaining_days": 365.0,
                "confidence": 0.5,
                "data_points": len(history),
                "current_health": current_health,
                "slope": slope,
                "intercept": intercept,
                "status": "stable_or_improving",
                "post_maintenance_reset": post_maintenance
            }

        hours_to_fail = (fail_threshold - intercept) / slope - x_vals[-1]
        remaining_days = max(0, hours_to_fail / 24.0) if hours_to_fail > 0 else 0

        ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x_vals, y_vals))
        ss_tot = sum((yi - (sum(y_vals) / len(y_vals))) ** 2 for yi in y_vals)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        confidence = max(0.0, min(1.0, r_squared))

        return {
            "device_id": device_id,
            "remaining_days": round(remaining_days, 1),
            "confidence": round(confidence, 2),
            "data_points": len(history),
            "current_health": current_health,
            "slope": round(slope, 4),
            "intercept": round(intercept, 2),
            "fail_threshold": fail_threshold,
            "status": "predicted",
            "post_maintenance_reset": post_maintenance
        }

    def predict_batch(self, device_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        results = {}
        for device_id in device_ids:
            results[device_id] = self.predict(device_id)
        return results

    def save_prediction(self, prediction: Dict[str, Any]) -> bool:
        device_id = prediction["device_id"]
        remaining_days = prediction.get("remaining_days")
        confidence = prediction.get("confidence", 0.0)
        status = prediction.get("status", "unknown")
        slope = prediction.get("slope")
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") if self.db_type != "postgres" else None

        cur = self.conn.cursor()
        try:
            if self.db_type == "postgres":
                cur.execute(f"""
                    INSERT INTO life_predictions (
                        device_id, remaining_days, confidence, slope,
                        status, predicted_at
                    ) VALUES ({self.p}, {self.p}, {self.p}, {self.p}, {self.p}, NOW())
                    ON CONFLICT (device_id) DO UPDATE SET
                        remaining_days = EXCLUDED.remaining_days,
                        confidence = EXCLUDED.confidence,
                        slope = EXCLUDED.slope,
                        status = EXCLUDED.status,
                        predicted_at = NOW();
                """, (device_id, remaining_days, confidence, slope, status))
            else:
                cur.execute(f"""
                    SELECT device_id FROM life_predictions WHERE device_id = {self.p};
                """, (device_id,))
                exists = cur.fetchone()
                if exists:
                    cur.execute(f"""
                        UPDATE life_predictions SET
                            remaining_days = {self.p},
                            confidence = {self.p},
                            slope = {self.p},
                            status = {self.p},
                            predicted_at = {self.p}
                        WHERE device_id = {self.p};
                    """, (remaining_days, confidence, slope, status, now, device_id))
                else:
                    cur.execute(f"""
                        INSERT INTO life_predictions (
                            device_id, remaining_days, confidence, slope,
                            status, predicted_at
                        ) VALUES ({self.p}, {self.p}, {self.p}, {self.p}, {self.p}, {self.p});
                    """, (device_id, remaining_days, confidence, slope, status, now))
            self.conn.commit()
            cur.close()
            return True
        except Exception as e:
            logger.exception(f"Failed to save prediction for {device_id}: {e}")
            cur.close()
            return False

    def get_cached_prediction(self, device_id: str, max_age_hours: int = 1) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        since_dt = datetime.utcnow() - timedelta(hours=max_age_hours)

        try:
            if self.db_type == "postgres":
                cur.execute(f"""
                    SELECT device_id, remaining_days, confidence, status, predicted_at
                    FROM life_predictions
                    WHERE device_id = {self.p} AND predicted_at >= {self.p};
                """, (device_id, since_dt))
            else:
                since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
                cur.execute(f"""
                    SELECT device_id, remaining_days, confidence, status, predicted_at
                    FROM life_predictions
                    WHERE device_id = {self.p} AND predicted_at >= {self.p};
                """, (device_id, since_str))

            row = cur.fetchone()
            cur.close()
            if row:
                return {
                    "device_id": row[0],
                    "remaining_days": row[1],
                    "confidence": row[2],
                    "status": row[3],
                    "predicted_at": row[4],
                    "cached": True
                }
            return None
        except Exception:
            cur.close()
            return None
