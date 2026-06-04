import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Callable

from managers.life_predictor import LifePredictor

logger = logging.getLogger(__name__)


class PredictionScheduler:
    def __init__(self, conn_factory, db_type: str, interval_minutes: int = 5):
        self.conn_factory = conn_factory
        self.db_type = db_type
        self.interval_minutes = interval_minutes
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    def _run_loop(self):
        logger.info(f"Prediction scheduler started (interval: {self.interval_minutes}min)")
        while not self._stop_event.is_set():
            try:
                self._run_prediction_batch()
            except Exception as e:
                logger.exception(f"Prediction batch failed: {e}")
            self._stop_event.wait(self.interval_minutes * 60)
        logger.info("Prediction scheduler stopped")

    def _run_prediction_batch(self):
        conn = self.conn_factory()
        try:
            cur = conn.cursor()
            cur.execute("SELECT device_id FROM devices WHERE status = 'online';")
            device_ids = [row[0] for row in cur.fetchall()]
            cur.close()

            predictor = LifePredictor(conn, self.db_type)
            predictions = predictor.predict_batch(device_ids)

            saved = 0
            for device_id, pred in predictions.items():
                if predictor.save_prediction(pred):
                    saved += 1

            conn.commit()
            logger.info(f"Prediction batch complete: {saved}/{len(device_ids)} devices updated")
        finally:
            conn.close()

    def start(self):
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._running = True

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        self._running = False
