# 半导体晶圆厂设备预防性维护平台

## 架构概览

```
┌─────────────────────────────────────────────────────────┐
│                     Nginx (端口 80)                       │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  静态资源     │  │  /api/*      │  │  /ws/*        │  │
│  │  /static/*   │  │  反向代理     │  │  WebSocket    │  │
│  └──────────────┘  └──────┬───────┘  └───────┬───────┘  │
└────────────────────────────┼──────────────────┼──────────┘
                             │                  │
┌────────────────────────────▼──────────────────▼──────────┐
│              FastAPI + gRPC Server (端口 8000/50051)       │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │ REST API    │  │ gRPC Server  │  │ WebSocket Hub   │  │
│  │ (uvicorn    │  │ (端口 50051) │  │ (告警推送)      │  │
│  │  workers=2) │  │              │  │                 │  │
│  └──────┬──────┘  └──────┬───────┘  └────────┬────────┘  │
│         │                │                    │            │
│  ┌──────▼────────────────▼────────────────────▼────────┐  │
│  │          health_evaluator.py  评分计算               │  │
│  │          workorder_engine.py   工单逻辑              │  │
│  └──────────────────────┬──────────────────────────────┘  │
└─────────────────────────┼─────────────────────────────────┘
                          │
┌─────────────────────────▼─────────────────────────────────┐
│           TimescaleDB (端口 5432)                          │
│  ┌───────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ sensor_data   │  │ work_orders  │  │ alerts        │  │
│  │ (超表, 1天分区)│  │              │  │               │  │
│  │ 7天自动压缩   │  │              │  │               │  │
│  │ 90天自动清理  │  │              │  │               │  │
│  └───────────────┘  └──────────────┘  └───────────────┘  │
└───────────────────────────────────────────────────────────┘
                          ▲
┌─────────────────────────┼─────────────────────────────────┐
│         gRPC 模拟器 (simulator.py)                        │
│  200台设备 → 30秒间隔 → ReportSensorData RPC              │
│  可配置：设备数 / 间隔 / 各型号基线参数                     │
└───────────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| API 服务 | `main.py` | FastAPI REST + WebSocket，仪表盘/设备/工单/告警接口 |
| gRPC 服务 | `grpc_server.py` | 流管理、线程安全集合、断连回调清理 |
| 健康评分 | `health_evaluator.py` | 非线性评分函数、EMA基线更新、告警规则检测 |
| 工单引擎 | `workorder_engine.py` | 自动工单生成、24h去重、维护时段过滤 |
| 模拟器 | `simulator.py` | 多设备传感器数据模拟，可配置基线参数 |
| 前端 | `frontend/app.js` | CSS Grid设备矩阵、Canvas趋势图、rAF批量更新 |

## 快速部署

### 前置条件

- Docker 20.10+
- Docker Compose 2.0+

### 生产模式部署

```bash
# 克隆项目
cd fab-maintenance

# 一键启动
docker compose up -d

# 查看日志
docker compose logs -f api

# 访问平台
# http://localhost
```

启动后 TimescaleDB 自动执行 `initdb/` 目录下的 SQL 初始化脚本，创建超表、压缩策略和保留策略。API 容器启动时自动运行 `init_db.py` 种子设备与工程师数据。

### 单独操作各服务

```bash
# 仅启动数据库
docker compose up -d timescaledb

# 重建 API 镜像
docker compose build api

# 重启模拟器
docker compose restart simulator

# 停止所有服务
docker compose down

# 清除数据卷
docker compose down -v
```

### 演示模式（无需 Docker）

```bash
cd backend
pip install fastapi uvicorn websockets
python init_demo_db.py
python demo_server.py
# 访问 http://localhost:8000
```

## gRPC 模拟器

### 运行方式

模拟器通过环境变量配置，支持三种运行方式：

**1. Docker Compose（推荐）**

在 `docker-compose.yml` 中修改 simulator 服务的环境变量：

```yaml
simulator:
  environment:
    NUM_DEVICES: 200
    REPORT_INTERVAL: 30
    GRPC_SERVER: api:50051
```

**2. 独立 Docker 容器**

```bash
docker run --rm \
  -e GRPC_SERVER=host.docker.internal:50051 \
  -e NUM_DEVICES=50 \
  -e REPORT_INTERVAL=10 \
  fab-maintenance-api \
  python simulator.py
```

**3. 本地 Python**

```bash
cd backend
export GRPC_SERVER=localhost:50051
export NUM_DEVICES=200
export REPORT_INTERVAL=30
python simulator.py
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GRPC_SERVER` | `localhost:50051` | gRPC 服务端地址 |
| `NUM_DEVICES` | `200` | 模拟设备数量 |
| `REPORT_INTERVAL` | `30` | 上报间隔（秒） |
| `BASELINE_{TYPE}_TEMP` | 见默认值 | 指定型号温度基线 |
| `BASELINE_{TYPE}_VIB` | 见默认值 | 指定型号振动基线 |
| `BASELINE_{TYPE}_PWR` | 见默认值 | 指定型号RF功率基线 |
| `CUSTOM_BASELINES` | 空 | JSON格式自定义基线 |

### 基线参数配置

**方式一：逐项环境变量**

每种设备型号支持三个参数，格式为 `BASELINE_{型号}_{指标}`：

```bash
export BASELINE_CVD_TEMP=350
export BASELINE_CVD_VIB=2.5
export BASELINE_CVD_PWR=500
export BASELINE_ETCH_TEMP=180
export BASELINE_ETCH_VIB=2.0
export BASELINE_ETCH_PWR=600
```

**方式二：JSON 批量配置**

通过 `CUSTOM_BASELINES` 一次性传入自定义基线，支持新增设备型号：

```bash
export CUSTOM_BASELINES='{
  "CVD":     {"temperature": 350, "vibration": 2.5, "rf_power": 500},
  "AOI":     {"temperature": 90,  "vibration": 0.3, "rf_power": 80},
  "FURNACE": {"temperature": 800, "vibration": 1.2, "rf_power": 0}
}'
```

### 默认基线参数

| 型号 | 温度 (°C) | 振动 (mm/s) | RF功率 (W) |
|------|-----------|-------------|------------|
| CVD | 350 | 2.5 | 500 |
| PVD | 280 | 3.0 | 800 |
| ETCH | 180 | 2.0 | 600 |
| LITHO | 120 | 1.0 | 300 |
| IMPLANT | 250 | 3.5 | 700 |
| CMP | 200 | 4.0 | 200 |
| DIFF | 400 | 1.5 | 450 |
| CLEAN | 150 | 1.8 | 150 |
| METRO | 100 | 0.5 | 100 |
| DEPO | 320 | 2.8 | 550 |

### 模拟器行为

- 8% 的设备设置为缓慢退化模式，健康评分随时间逐渐下降
- 1% 概率触发异常事件（2-10个周期），模拟温度/振动突变
- 退化因子持续累积，模拟设备老化过程
- gRPC 连接失败时自动重试，每10个周期打印一次错误日志

## 核心算法

### 健康评分

非线性评分函数，偏离基线 5% 以内不扣分（容忍区），5-20% 线性扣分，超过 20% 加速惩罚：

```
偏离 < 5%   → 100 分
5% ≤ 偏离 < 20% → 100 - (偏离-5%)/15% × 30 分
偏离 ≥ 20%  → 70 - (偏离-20%)/80% × 70 分
```

加权求和：`评分 = 0.3×温度分 + 0.4×振动分 + 0.3×功率分`

### 基线更新

EMA（指数移动平均）增量更新，α=0.03：

```
新基线 = 0.03 × 当前读数 + 0.97 × 旧基线
```

仅当读数偏离基线不超过阈值（温度30%、振动50%、功率30%）时才更新，异常数据不会拉偏基线。

新设备首次上报时自动从同型号设备均值校准基线。

### 工单生成

- 条件：健康评分 < 60 连续超过 1 小时
- 去重：同一设备 24 小时内不重复创建同原因工单
- 时段过滤：周末 0:00-6:00 维护时段不创建工单
- 状态流转：待接单 → 处理中 → 已完成 → 已验收

### 告警规则

| 告警类型 | 触发条件 | 持续时间 |
|----------|----------|----------|
| 过热告警 | 温度超基线 30% | 5 分钟 |
| 振动告警 | 振动超基线 50% | 3 分钟 |

告警通过 WebSocket 实时推送到前端弹窗，恢复正常后自动关闭。

## TimescaleDB 配置

数据库初始化脚本（`initdb/001_schema.sql`）自动配置：

- **超表分区**：`sensor_data` 和 `health_score_history` 按天分区
- **自动压缩**：7 天前的数据自动压缩，按 `device_id` 分段，按 `time DESC` 排序
- **自动清理**：90 天前的数据自动删除
- **索引**：`(device_id, time DESC)` 复合索引加速查询

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/dashboard/summary` | 仪表盘概览 |
| GET | `/api/devices` | 设备列表 |
| GET | `/api/devices/{id}` | 设备详情 |
| GET | `/api/devices/{id}/trend?hours=24` | 趋势数据 |
| GET | `/api/work-orders` | 工单列表 |
| POST | `/api/work-orders/{id}/accept` | 接单 |
| POST | `/api/work-orders/{id}/complete` | 完成 |
| POST | `/api/work-orders/{id}/verify` | 验收 |
| GET | `/api/alerts` | 告警列表 |
| WS | `/ws/alerts` | 实时告警推送 |

## 目录结构

```
.
├── Dockerfile                # 多阶段构建，python:3.12-slim
├── docker-compose.yml        # 四服务编排
├── entrypoint.sh             # API 容器启动脚本
├── initdb/
│   ├── 001_schema.sql        # 建表 + 超表 + 压缩/保留策略
│   └── 002_engineers.sql     # 工程师种子数据
├── nginx/
│   └── default.conf          # Nginx 配置（静态 + 反代 + WS）
├── backend/
│   ├── main.py               # FastAPI REST + WebSocket
│   ├── grpc_server.py         # gRPC 流管理
│   ├── health_evaluator.py   # 健康评分 + EMA 基线
│   ├── workorder_engine.py   # 工单生成引擎
│   ├── simulator.py          # gRPC 设备模拟器
│   ├── init_db.py            # 生产模式数据库初始化
│   ├── init_demo_db.py       # 演示模式 SQLite 初始化
│   ├── demo_server.py        # 演示模式服务器
│   ├── requirements.txt      # Python 依赖
│   ├── proto/
│   │   └── sensor_data.proto # gRPC 协议定义
│   └── generated/            # protoc 生成代码
└── frontend/
    ├── index.html
    ├── style.css
    └── app.js                # 设备矩阵 + Canvas 趋势图
```
