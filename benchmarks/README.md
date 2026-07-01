# Benchmarks: fastware vs FastAPI

Comparative benchmarks measuring serialization throughput, cold import time, and HTTP request throughput.

## Benchmarks

### bench_serialization.py

Compares **msgspec** (fastware's serializer) vs **Pydantic** (FastAPI's serializer) for JSON encode/decode of a realistic nested data structure: 100 user objects, each with a nested address. Runs 10,000 iterations and reports ops/sec, microseconds/op, and speedup ratio.

### bench_import.py

Measures **cold import time** for each framework and its key dependencies using isolated subprocess invocations (no bytecode cache advantage from the parent process). Runs 5 iterations and reports mean/min/max.

### bench_throughput.py

Measures **HTTP request throughput** (requests/second). Starts a minimal JSON endpoint (GET /users returning 100 user objects) on each server stack, then sends 1,000 requests with 50-concurrent batches via httpx. Compares fastware+granian vs FastAPI+uvicorn.

## Running

```bash
cd ~/Projects/fastware

# Install comparison dependencies (if not already present)
pip install fastapi uvicorn pydantic httpx --break-system-packages

# Run individual benchmarks
python benchmarks/bench_serialization.py
python benchmarks/bench_import.py
python benchmarks/bench_throughput.py
```

## Results

Measured on 2026-07-01.

### Environment

| Component | Version |
|-----------|---------|
| Python | 3.14.5 |
| OS | Fedora 43, Linux 6.18.16 |
| CPU | 13th Gen Intel Core i7-13620H |
| RAM | 24 GB |
| fastware | 0.1.0 |
| FastAPI | 0.136.1 |
| msgspec | 0.21.1 |
| Pydantic | 2.12.5 |
| granian | 2.7.4 |
| uvicorn | 0.38.0 |

### Serialization (msgspec vs Pydantic)

Data: 100 user objects with nested addresses (21 KB JSON payload), 10,000 iterations.

| Operation | msgspec | Pydantic | Speedup |
|-----------|---------|----------|---------|
| Encode (ops/sec) | 65,255 | 16,720 | 3.9x |
| Encode (us/op) | 15.3 | 59.8 | |
| Decode (ops/sec) | 21,067 | 6,174 | 3.4x |
| Decode (us/op) | 47.5 | 162.0 | |

### Cold Import Time

| Module | Mean | Min |
|--------|------|-----|
| fastware | 74.5 ms | 65.6 ms |
| FastAPI | 319.8 ms | 278.2 ms |
| msgspec | 19.0 ms | 16.6 ms |
| Pydantic | 36.8 ms | 34.2 ms |
| granian | 98.9 ms | 79.0 ms |
| uvicorn | 113.5 ms | 97.1 ms |

FastAPI imports 4.3x slower than fastware. Pydantic imports 1.9x slower than msgspec.

### Request Throughput (GET /users, 100 JSON objects)

1,000 requests, 50-concurrent batches.

| Framework | req/s | Total Time |
|-----------|-------|------------|
| fastware (granian) | 1,047 | 0.96s |
| FastAPI (uvicorn) | 404 | 2.48s |

fastware is 2.6x faster at serving JSON responses.
