import sys
import os
import random
import time
import logging
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "generated"))
import grpc
import sensor_data_pb2
import sensor_data_pb2_grpc

GRPC_SERVER = os.getenv("GRPC_SERVER", "localhost:50051")
NUM_DEVICES = int(os.getenv("NUM_DEVICES", "200"))
REPORT_INTERVAL = int(os.getenv("REPORT_INTERVAL", "30"))

DEFAULT_BASELINES = {
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

CUSTOM_BASELINES_JSON = os.getenv("CUSTOM_BASELINES", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Simulator] %(message)s")
logger = logging.getLogger(__name__)


def load_baselines():
    baselines = {}
    for eq_type, defaults in DEFAULT_BASELINES.items():
        baselines[eq_type] = {
            "temperature": float(os.getenv(f"BASELINE_{eq_type}_TEMP", defaults["temperature"])),
            "vibration":   float(os.getenv(f"BASELINE_{eq_type}_VIB",  defaults["vibration"])),
            "rf_power":    float(os.getenv(f"BASELINE_{eq_type}_PWR",  defaults["rf_power"])),
        }
    if CUSTOM_BASELINES_JSON:
        try:
            custom = json.loads(CUSTOM_BASELINES_JSON)
            for eq_type, vals in custom.items():
                eq_upper = eq_type.upper()
                if eq_upper not in baselines:
                    baselines[eq_upper] = vals
                else:
                    baselines[eq_upper].update(vals)
            logger.info(f"Merged custom baselines for: {list(custom.keys())}")
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse CUSTOM_BASELINES JSON: {e}")
    return baselines


class DeviceSimulator:
    def __init__(self, device_id, eq_type, baseline):
        self.device_id = device_id
        self.eq_type = eq_type
        self.baseline = baseline
        self.degrading = random.random() < 0.08
        self.degradation_factor = 1.0
        self.degradation_rate = random.uniform(0.0005, 0.003)
        self.anomaly_mode = False
        self.anomaly_countdown = 0

    def generate_reading(self):
        bt = self.baseline["temperature"]
        bv = self.baseline["vibration"]
        bp = self.baseline["rf_power"]

        if self.degrading:
            self.degradation_factor += self.degradation_rate

        if random.random() < 0.01:
            self.anomaly_mode = True
            self.anomaly_countdown = random.randint(2, 10)

        if self.anomaly_mode and self.anomaly_countdown > 0:
            temp = bt * (1 + random.uniform(0.2, 0.5) * self.degradation_factor)
            vib = bv * (1 + random.uniform(0.3, 0.8) * self.degradation_factor)
            pwr = bp * (1 + random.uniform(-0.3, 0.3))
            self.anomaly_countdown -= 1
            if self.anomaly_countdown <= 0:
                self.anomaly_mode = False
        else:
            temp = bt + random.gauss(0, bt * 0.05) * self.degradation_factor
            vib = bv + random.gauss(0, bv * 0.08) * self.degradation_factor
            pwr = bp + random.gauss(0, bp * 0.04)

        temp = max(bt * 0.5, min(bt * 2.0, temp))
        vib = max(bv * 0.2, min(bv * 3.0, vib))
        pwr = max(bp * 0.3, min(bp * 1.8, pwr))

        return sensor_data_pb2.SensorReport(
            device_id=self.device_id,
            temperature=round(temp, 2),
            vibration=round(vib, 4),
            rf_power=round(pwr, 2),
            timestamp=int(time.time()),
        )


def create_devices(baselines):
    equipment_types = list(baselines.keys())
    devices = []
    for i in range(NUM_DEVICES):
        device_id = f"EQ-{i + 1:03d}"
        eq_type = equipment_types[i % len(equipment_types)]
        devices.append(DeviceSimulator(device_id, eq_type, baselines[eq_type]))
    return devices


def run_simulator():
    baselines = load_baselines()
    devices = create_devices(baselines)
    channel = grpc.insecure_channel(GRPC_SERVER)
    stub = sensor_data_pb2_grpc.SensorServiceStub(channel)

    logger.info(f"Starting simulator: {NUM_DEVICES} devices, interval={REPORT_INTERVAL}s, server={GRPC_SERVER}")
    logger.info(f"Equipment baselines: {list(baselines.keys())}")
    for eq, bl in baselines.items():
        logger.info(f"  {eq}: T={bl['temperature']} V={bl['vibration']} P={bl['rf_power']}")

    cycle = 0
    while True:
        cycle += 1
        start_time = time.time()
        success_count = 0
        error_count = 0

        for device in devices:
            report = device.generate_reading()
            try:
                ack = stub.ReportSensorData(report, timeout=5)
                if ack.success:
                    success_count += 1
                else:
                    error_count += 1
            except grpc.RpcError as e:
                error_count += 1
                if cycle % 10 == 0:
                    logger.warning(f"gRPC error for {device.device_id}: {e.code()}")

        elapsed = time.time() - start_time
        logger.info(f"Cycle {cycle}: {success_count} ok, {error_count} err, {elapsed:.1f}s")

        sleep_time = max(0, REPORT_INTERVAL - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)


if __name__ == "__main__":
    run_simulator()
