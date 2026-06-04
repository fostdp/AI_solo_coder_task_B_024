import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any

from managers.life_predictor import LifePredictor, _linear_regression as lp_linear_regression

logger = logging.getLogger(__name__)

EMA_ALPHA = 0.03
_baseline_cache = {}
_baseline_cache_lock = threading.Lock()

FAIL_THRESHOLD = 30.0
MIN_DATA_POINTS_FOR_PREDICTION = 24
LIFETIME_PREDICTION_WINDOW_HOURS = 72


def _ph(db_type):
    return '%s' if db_type == 'postgres' else '?'


def _now_val(db_type):
    if db_type == 'postgres':
        return datetime.utcnow()
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _parse_time(val, db_type):
    if db_type == 'postgres':
        return val
    if isinstance(val, str):
        try:
            return datetime.strptime(val, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return datetime.utcnow()
    return val


def is_maintenance_window():
    now = datetime.utcnow() + timedelta(hours=8)
    weekday = now.weekday()
    hour = now.hour
    return weekday >= 5 and 0 <= hour < 6


def _nonlinear_score(deviation):
    if deviation < 0.05:
        return 100.0
    elif deviation < 0.2:
        return 100.0 - (deviation - 0.05) / 0.15 * 30.0
    else:
        return max(0.0, 70.0 - (deviation - 0.2) / 0.8 * 70.0)


def compute_health_score(temperature, vibration, rf_power,
                         baseline_temp, baseline_vib, baseline_pwr,
                         device_id=None, equipment_type=None,
                         get_type_baseline_fn=None,
                         update_baseline_fn=None):
    if baseline_temp is None or baseline_vib is None or baseline_pwr is None:
        if equipment_type and device_id and get_type_baseline_fn:
            bt, bv, bp = get_type_baseline_fn(equipment_type)
            if bt is not None:
                baseline_temp, baseline_vib, baseline_pwr = bt, bv, bp
                if update_baseline_fn:
                    update_baseline_fn(device_id, baseline_temp, baseline_vib, baseline_pwr)
                    logger.info(f"Device {device_id}: baseline auto-calibrated from {equipment_type} avg")

    if baseline_temp is None or baseline_vib is None or baseline_pwr is None:
        return 50.0

    temp_dev = abs(temperature - baseline_temp) / baseline_temp if baseline_temp else 0
    vib_dev = abs(vibration - baseline_vib) / baseline_vib if baseline_vib else 0
    pwr_dev = abs(rf_power - baseline_pwr) / baseline_pwr if baseline_pwr else 0

    temp_score = _nonlinear_score(temp_dev)
    vib_score = _nonlinear_score(vib_dev)
    pwr_score = _nonlinear_score(pwr_dev)

    return round(0.3 * temp_score + 0.4 * vib_score + 0.3 * pwr_score, 2)


def update_baseline_ema(old_baseline, current_reading, alpha=EMA_ALPHA):
    if old_baseline is None:
        return current_reading
    return round(alpha * current_reading + (1 - alpha) * old_baseline, 4)


def should_update_baseline(temperature, vibration, rf_power,
                           baseline_temp, baseline_vib, baseline_pwr):
    if baseline_temp is None or baseline_temp == 0:
        return False
    temp_ok = abs(temperature - baseline_temp) / baseline_temp < 0.3
    vib_ok = baseline_vib and abs(vibration - baseline_vib) / baseline_vib < 0.5
    pwr_ok = baseline_pwr and abs(rf_power - baseline_pwr) / baseline_pwr < 0.3
    return temp_ok and vib_ok and pwr_ok


def update_baselines_ema(device_id, temperature, vibration, rf_power,
                         old_bt, old_bv, old_bp,
                         conn, db_type, update_baseline_fn=None):
    if not should_update_baseline(temperature, vibration, rf_power, old_bt, old_bv, old_bp):
        return old_bt, old_bv, old_bp

    new_bt = update_baseline_ema(old_bt, temperature)
    new_bv = update_baseline_ema(old_bv, vibration)
    new_bp = update_baseline_ema(old_bp, rf_power)

    if update_baseline_fn:
        update_baseline_fn(device_id, new_bt, new_bv, new_bp)
    else:
        p = _ph(db_type)
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE devices SET
                baseline_temperature = {p},
                baseline_vibration = {p},
                baseline_rf_power = {p}
            WHERE device_id = {p};
        """, (new_bt, new_bv, new_bp, device_id))
        conn.commit()
        cur.close()

    return new_bt, new_bv, new_bp


def check_and_fire_alerts(device_id, temperature, vibration, rf_power,
                          baseline_temp, baseline_vib, baseline_pwr,
                          conn, db_type, alert_callback):
    p = _ph(db_type)
    now = _now_val(db_type)
    cur = conn.cursor()

    temp_dev_pct = abs(temperature - baseline_temp) / baseline_temp * 100 if baseline_temp else 0
    vib_dev_pct = abs(vibration - baseline_vib) / baseline_vib * 100 if baseline_vib else 0

    if temp_dev_pct >= 30:
        cur.execute(f"""
            SELECT id, started_at FROM alerts
            WHERE device_id = {p} AND alert_type = {p} AND status = {p}
            ORDER BY started_at DESC LIMIT 1;
        """, (device_id, 'overheat', 'active'))
        row = cur.fetchone()
        if row:
            started_at = _parse_time(row[1], db_type)
            elapsed = (datetime.utcnow() - started_at).total_seconds()
            if 300 <= elapsed < 330:
                if alert_callback:
                    alert_callback({
                        "type": "alert", "alert_type": "overheat",
                        "device_id": device_id,
                        "metric_value": temperature,
                        "baseline_value": baseline_temp,
                        "deviation_pct": round(temp_dev_pct, 1),
                        "message": f"设备 {device_id} 过热告警：温度 {temperature:.1f}°C 超基线 {temp_dev_pct:.1f}%"
                    })
        else:
            cur.execute(f"""
                INSERT INTO alerts (device_id, alert_type, metric_value, baseline_value, deviation_pct, started_at, status)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p});
            """, (device_id, 'overheat', temperature, baseline_temp, round(temp_dev_pct, 1), now, 'active'))
    else:
        cur.execute(f"""
            UPDATE alerts SET status = {p}, ended_at = {p}
            WHERE device_id = {p} AND alert_type = {p} AND status = {p};
        """, ('resolved', now, device_id, 'overheat', 'active'))

    if vib_dev_pct >= 50:
        cur.execute(f"""
            SELECT id, started_at FROM alerts
            WHERE device_id = {p} AND alert_type = {p} AND status = {p}
            ORDER BY started_at DESC LIMIT 1;
        """, (device_id, 'vibration', 'active'))
        row = cur.fetchone()
        if row:
            started_at = _parse_time(row[1], db_type)
            elapsed = (datetime.utcnow() - started_at).total_seconds()
            if 180 <= elapsed < 210:
                if alert_callback:
                    alert_callback({
                        "type": "alert", "alert_type": "vibration",
                        "device_id": device_id,
                        "metric_value": vibration,
                        "baseline_value": baseline_vib,
                        "deviation_pct": round(vib_dev_pct, 1),
                        "message": f"设备 {device_id} 振动告警：振动 {vibration:.2f} 超基线 {vib_dev_pct:.1f}%"
                    })
        else:
            cur.execute(f"""
                INSERT INTO alerts (device_id, alert_type, metric_value, baseline_value, deviation_pct, started_at, status)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p});
            """, (device_id, 'vibration', vibration, baseline_vib, round(vib_dev_pct, 1), now, 'active'))
    else:
        cur.execute(f"""
            UPDATE alerts SET status = {p}, ended_at = {p}
            WHERE device_id = {p} AND alert_type = {p} AND status = {p};
        """, ('resolved', now, device_id, 'vibration', 'active'))

    cur.close()
    conn.commit()


def _linear_regression(x: List[float], y: List[float]) -> Tuple[float, float]:
    n = len(x)
    if n < 2:
        return 0.0, 0.0
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)
    denominator = n * sum_x2 - sum_x * sum_x
    if abs(denominator) < 1e-10:
        return 0.0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _detect_maintenance_event(device_id: str, conn, db_type: str, hours: int = 72) -> Optional[datetime]:
    predictor = LifePredictor(conn, db_type)
    return predictor._detect_maintenance_event(device_id, hours)


def _get_health_history(device_id: str, conn, db_type: str, hours: int = 72) -> List[Tuple[datetime, float]]:
    predictor = LifePredictor(conn, db_type)
    history, post_maintenance = predictor._get_health_history(device_id, hours)
    return history, post_maintenance


def predict_remaining_life(device_id: str, conn, db_type: str,
                            hours: int = LIFETIME_PREDICTION_WINDOW_HOURS,
                            fail_threshold: float = FAIL_THRESHOLD) -> Dict[str, Any]:
    predictor = LifePredictor(conn, db_type)
    result = predictor.predict(device_id, hours, fail_threshold)
    return result


def predict_all_devices_life(devices: List[str], conn, db_type: str) -> Dict[str, Dict[str, Any]]:
    predictor = LifePredictor(conn, db_type)
    return predictor.predict_batch(devices)
