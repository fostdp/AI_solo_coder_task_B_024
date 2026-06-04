FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    protobuf-compiler && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local

COPY backend/ .
COPY backend/proto/ ./proto/
COPY frontend/ ../frontend/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN pip install grpcio-tools==1.59.3 && \
    python -m grpc_tools.protoc \
        -I./proto \
        --python_out=./generated \
        --grpc_python_out=./generated \
        ./proto/sensor_data.proto && \
    pip uninstall -y grpcio-tools && \
    rm -rf /root/.cache/pip

EXPOSE 8000 50051

ENTRYPOINT ["/app/entrypoint.sh"]
