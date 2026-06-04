CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS factories (
    factory_id VARCHAR(32) PRIMARY KEY,
    factory_name VARCHAR(128),
    address VARCHAR(256),
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    status VARCHAR(32) DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS devices (
    device_id VARCHAR(32) PRIMARY KEY,
    device_name VARCHAR(128),
    equipment_type VARCHAR(32),
    area VARCHAR(16),
    factory_id VARCHAR(32) REFERENCES factories(factory_id),
    engineer_id VARCHAR(32),
    baseline_temperature DOUBLE PRECISION,
    baseline_vibration DOUBLE PRECISION,
    baseline_rf_power DOUBLE PRECISION,
    status VARCHAR(32) DEFAULT 'online'
);

CREATE TABLE IF NOT EXISTS sensor_data (
    time TIMESTAMPTZ NOT NULL,
    device_id VARCHAR(32) NOT NULL,
    temperature DOUBLE PRECISION,
    vibration DOUBLE PRECISION,
    rf_power DOUBLE PRECISION,
    health_score DOUBLE PRECISION
);

SELECT create_hypertable('sensor_data', 'time',
    chunk_time_interval => INTERVAL '1 day',
    migrate_data => true
);

CREATE INDEX IF NOT EXISTS idx_sensor_data_device_time
ON sensor_data (device_id, time DESC);

CREATE TABLE IF NOT EXISTS work_orders (
    id SERIAL PRIMARY KEY,
    device_id VARCHAR(32) NOT NULL,
    factory_id VARCHAR(32) REFERENCES factories(factory_id),
    engineer_id VARCHAR(32),
    recommended_engineer_id VARCHAR(32),
    spare_part_id VARCHAR(32),
    reason TEXT,
    assignment_reason TEXT,
    status VARCHAR(32) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS spare_parts (
    part_id VARCHAR(32) PRIMARY KEY,
    part_name VARCHAR(128),
    equipment_type VARCHAR(32),
    stock_quantity INTEGER DEFAULT 0,
    safe_stock_level INTEGER DEFAULT 5,
    unit_price DOUBLE PRECISION,
    supplier VARCHAR(128)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id SERIAL PRIMARY KEY,
    part_id VARCHAR(32) REFERENCES spare_parts(part_id),
    factory_id VARCHAR(32) REFERENCES factories(factory_id),
    quantity INTEGER,
    reason TEXT,
    status VARCHAR(32) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    device_id VARCHAR(32) NOT NULL,
    alert_type VARCHAR(32) NOT NULL,
    metric_value DOUBLE PRECISION,
    baseline_value DOUBLE PRECISION,
    deviation_pct DOUBLE PRECISION,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    status VARCHAR(32) DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS engineers (
    engineer_id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(64),
    specialty VARCHAR(32)
);

CREATE TABLE IF NOT EXISTS health_score_history (
    time TIMESTAMPTZ NOT NULL,
    device_id VARCHAR(32) NOT NULL,
    health_score DOUBLE PRECISION
);

SELECT create_hypertable('health_score_history', 'time',
    chunk_time_interval => INTERVAL '1 day',
    migrate_data => true
);

ALTER TABLE sensor_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('sensor_data', INTERVAL '7 days');

ALTER TABLE health_score_history SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('health_score_history', INTERVAL '7 days');

SELECT add_retention_policy('sensor_data', INTERVAL '90 days');
SELECT add_retention_policy('health_score_history', INTERVAL '90 days');
