import sys
import os
import logging
import time
import threading
from concurrent import futures
from datetime import datetime

import grpc
import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "generated"))
import sensor_data_pb2
import sensor_data_pb2_grpc

from health_evaluator import (
    compute_health_score, update_baselines_ema, check_and_fire_alerts
)
from workorder_engine import check_work_order

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "fab_maintenance")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")

GRPC_PORT = int(os.getenv("GRPC_PORT", "50051"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [gRPC] %(message)s")
logger = logging.getLogger(__name__)

_alert_callback = None

_active_streams = set()
_streams_lock = threading.Lock()


def set_alert_callback(cb):
    global _alert_callback
    _alert_callback = cb


def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
    )


def _get_type_baseline(equipment_type):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT AVG(baseline_temperature), AVG(baseline_vibration), AVG(baseline_rf_power)
        FROM devices WHERE equipment_type = %s;
    """, (equipment_type,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and row[0]:
        return row[0], row[1], row[2]
    return None, None, None


def _update_baseline_db(device_id, bt, bv, bp):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE devices SET
            baseline_temperature = %s,
            baseline_vibration = %s,
            baseline_rf_power = %s
        WHERE device_id = %s;
    """, (bt, bv, bp, device_id))
    conn.commit()
    cur.close()
    conn.close()


class SensorServiceServicer(sensor_data_pb2_grpc.SensorServiceServicer):

    def ReportSensorData(self, request, context):
        try:
            conn = get_connection()
            cur = conn.cursor()

            cur.execute("""
                SELECT baseline_temperature, baseline_vibration, baseline_rf_power, equipment_type
                FROM devices WHERE device_id = %s;
            """, (request.device_id,))
            row = cur.fetchone()
            if not row:
                cur.close()
                conn.close()
                return sensor_data_pb2.SensorAck(success=False, message="Device not found")

            bt, bv, bp, eq_type = row

            health = compute_health_score(
                request.temperature, request.vibration, request.rf_power,
                bt, bv, bp, request.device_id, eq_type,
                get_type_baseline_fn=_get_type_baseline,
                update_baseline_fn=_update_baseline_db
            )

            new_bt, new_bv, new_bp = update_baselines_ema(
                request.device_id, request.temperature, request.vibration, request.rf_power,
                bt, bv, bp, conn, 'postgres', _update_baseline_db
            )

            ts = datetime.utcnow() if request.timestamp == 0 else datetime.utcfromtimestamp(request.timestamp)

            cur.execute("""
                INSERT INTO sensor_data (time, device_id, temperature, vibration, rf_power, health_score)
                VALUES (%s, %s, %s, %s, %s, %s);
            """, (ts, request.device_id, request.temperature, request.vibration, request.rf_power, health))

            cur.execute("""
                INSERT INTO health_score_history (time, device_id, health_score)
                VALUES (%s, %s, %s);
            """, (ts, request.device_id, health))

            conn.commit()
            cur.close()

            check_and_fire_alerts(
                request.device_id, request.temperature, request.vibration,
                request.rf_power, new_bt, new_bv, new_bp,
                conn, 'postgres', _alert_callback
            )
            check_work_order(request.device_id, health, conn, 'postgres', _alert_callback)

            conn.close()
            logger.info(f"Device {request.device_id}: T={request.temperature:.1f} V={request.vibration:.2f} P={request.rf_power:.1f} Health={health}")
            return sensor_data_pb2.SensorAck(success=True, message=f"Health score: {health}")

        except Exception as e:
            logger.error(f"Error processing report: {e}")
            return sensor_data_pb2.SensorAck(success=False, message=str(e))

    def StreamSensorData(self, request_iterator, context):
        stream_id = id(context)
        device_id = None
        with _streams_lock:
            _active_streams.add(stream_id)
            active_count = len(_active_streams)
        logger.info(f"Stream opened. Active streams: {active_count}")

        try:
            def on_close():
                with _streams_lock:
                    if stream_id in _active_streams:
                        _active_streams.discard(stream_id)
                        remaining = len(_active_streams)
                logger.info(f"Stream closed. Active streams: {remaining}")

            context.add_callback(on_close)

            for report in request_iterator:
                device_id = report.device_id
                ack = self.ReportSensorData(report, context)
                yield ack

        except grpc.RpcError as e:
            logger.info(f"Stream RPC error for {device_id or 'unknown'}: {e.code()}")
        except Exception as e:
            logger.error(f"Stream exception for {device_id or 'unknown'}: {e}")
        finally:
            with _streams_lock:
                _active_streams.discard(stream_id)
                remaining = len(_active_streams)
            logger.info(f"Stream finally cleaned up. Active streams: {remaining}")


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    sensor_data_pb2_grpc.add_SensorServiceServicer_to_server(SensorServiceServicer(), server)
    server.add_insecure_port(f"[::]:{GRPC_PORT}")
    server.start()
    logger.info(f"gRPC server started on port {GRPC_PORT}")
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        server.stop(0)


if __name__ == "__main__":
    serve()
