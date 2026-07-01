"""Benchmark: request throughput for fastware (granian) vs FastAPI (uvicorn).

Starts each server as a subprocess, sends 1000 requests via httpx,
measures requests/second.
"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request


def find_free_port() -> int:
    """Find an ephemeral free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(host: str, port: int, timeout: float = 15.0) -> bool:
    """Poll until the server responds on the given port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(f"http://{host}:{port}/users", timeout=2)
            if resp.status == 200:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def stop_server(proc: subprocess.Popen) -> None:
    """Stop a server subprocess gracefully, then force-kill if needed."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


async def send_requests(url: str, n: int) -> tuple[float, int]:
    """Send n GET requests to url, return (total_seconds, success_count)."""
    import httpx

    success = 0
    async with httpx.AsyncClient() as client:
        t0 = time.perf_counter()
        # Send in batches of 50 concurrent requests
        batch_size = 50
        for batch_start in range(0, n, batch_size):
            batch_end = min(batch_start + batch_size, n)
            tasks = [client.get(url) for _ in range(batch_end - batch_start)]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for r in responses:
                if not isinstance(r, Exception) and r.status_code == 200:
                    success += 1
        elapsed = time.perf_counter() - t0
    return elapsed, success


# -- Fastware app (written to a temp file so granian can import it) ----------

FASTWARE_APP_CODE = '''
import msgspec
from fastware import Router, create_app, AppConfig


class Address(msgspec.Struct):
    street: str
    city: str
    state: str
    zip_code: str
    country: str


class User(msgspec.Struct):
    id: int
    name: str
    email: str
    age: int
    is_active: bool
    tags: list[str]
    address: Address


USERS = [
    User(
        id=i,
        name=f"User {i}",
        email=f"user{i}@example.com",
        age=20 + (i % 50),
        is_active=i % 3 != 0,
        tags=[f"tag{j}" for j in range(i % 5 + 1)],
        address=Address(
            street=f"{100 + i} Main St",
            city=f"City{i % 10}",
            state=f"S{i % 50:02d}",
            zip_code=f"{10000 + i}",
            country="US",
        ),
    )
    for i in range(100)
]

router = Router()

@router.get("/users")
async def get_users(request):
    return USERS

app = create_app(router, config=AppConfig(request_id=False, request_timing=False))
'''


# -- FastAPI app (written to a temp file so uvicorn can import it) -----------

FASTAPI_APP_CODE = '''
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Address(BaseModel):
    street: str
    city: str
    state: str
    zip_code: str
    country: str


class User(BaseModel):
    id: int
    name: str
    email: str
    age: int
    is_active: bool
    tags: list[str]
    address: Address


USERS = [
    User(
        id=i,
        name=f"User {i}",
        email=f"user{i}@example.com",
        age=20 + (i % 50),
        is_active=i % 3 != 0,
        tags=[f"tag{j}" for j in range(i % 5 + 1)],
        address=Address(
            street=f"{100 + i} Main St",
            city=f"City{i % 10}",
            state=f"S{i % 50:02d}",
            zip_code=f"{10000 + i}",
            country="US",
        ),
    )
    for i in range(100)
]


@app.get("/users")
async def get_users():
    return [u.model_dump() for u in USERS]
'''


def run_benchmark():
    n_requests = 1000
    host = "127.0.0.1"

    # Write temp app files in the benchmarks directory
    bench_dir = os.path.dirname(os.path.abspath(__file__))
    fastware_app_path = os.path.join(bench_dir, "_bench_fastware_app.py")
    fastapi_app_path = os.path.join(bench_dir, "_bench_fastapi_app.py")

    try:
        with open(fastware_app_path, "w") as f:
            f.write(FASTWARE_APP_CODE)
        with open(fastapi_app_path, "w") as f:
            f.write(FASTAPI_APP_CODE)

        print("=" * 70)
        print("Request Throughput Benchmark: fastware (granian) vs FastAPI (uvicorn)")
        print(f"  Requests: {n_requests:,}")
        print(f"  Concurrency: 50 concurrent requests per batch")
        print(f"  Endpoint: GET /users (100 user objects with nested addresses)")
        print("=" * 70)
        print()

        results = {}

        # -- Benchmark fastware (granian) ------------------------------------
        port_fw = find_free_port()
        print(f"Starting fastware (granian) on port {port_fw}...")
        env_fw = os.environ.copy()
        env_fw["FASTWARE_HOST"] = host
        env_fw["FASTWARE_PORT"] = str(port_fw)
        proc_fw = subprocess.Popen(
            [
                sys.executable, "-c",
                f"from granian import Granian; "
                f"import sys; sys.path.insert(0, {bench_dir!r}); "
                f"Granian('_bench_fastware_app:app', address='{host}', port={port_fw}, interface='asgi').serve()",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            if not wait_for_server(host, port_fw):
                print("  ERROR: fastware server did not start")
            else:
                print(f"  Server ready. Sending {n_requests} requests...")
                elapsed, success = asyncio.run(
                    send_requests(f"http://{host}:{port_fw}/users", n_requests)
                )
                rps = success / elapsed if elapsed > 0 else 0
                results["fastware"] = {
                    "elapsed": elapsed,
                    "success": success,
                    "rps": rps,
                }
                print(f"  Done: {success}/{n_requests} successful in {elapsed:.2f}s ({rps:,.0f} req/s)")
        finally:
            stop_server(proc_fw)
            print()

        # -- Benchmark FastAPI (uvicorn) -------------------------------------
        port_fa = find_free_port()
        print(f"Starting FastAPI (uvicorn) on port {port_fa}...")
        proc_fa = subprocess.Popen(
            [
                sys.executable, "-c",
                f"import uvicorn; import sys; sys.path.insert(0, {bench_dir!r}); "
                f"uvicorn.run('_bench_fastapi_app:app', host='{host}', port={port_fa}, "
                f"log_level='error')",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            if not wait_for_server(host, port_fa):
                print("  ERROR: FastAPI server did not start")
            else:
                print(f"  Server ready. Sending {n_requests} requests...")
                elapsed, success = asyncio.run(
                    send_requests(f"http://{host}:{port_fa}/users", n_requests)
                )
                rps = success / elapsed if elapsed > 0 else 0
                results["fastapi"] = {
                    "elapsed": elapsed,
                    "success": success,
                    "rps": rps,
                }
                print(f"  Done: {success}/{n_requests} successful in {elapsed:.2f}s ({rps:,.0f} req/s)")
        finally:
            stop_server(proc_fa)
            print()

        # -- Summary ---------------------------------------------------------
        if "fastware" in results and "fastapi" in results:
            fw = results["fastware"]
            fa = results["fastapi"]

            print(f"{'Framework':<25} {'req/s':>12} {'total time':>12} {'success':>10}")
            print("-" * 60)
            print(f"{'fastware (granian)':<25} {fw['rps']:>11,.0f} {fw['elapsed']:>11.2f}s {fw['success']:>9}/{n_requests}")
            print(f"{'FastAPI (uvicorn)':<25} {fa['rps']:>11,.0f} {fa['elapsed']:>11.2f}s {fa['success']:>9}/{n_requests}")
            print()

            if fw["rps"] > fa["rps"]:
                ratio = fw["rps"] / fa["rps"]
                print(f"fastware is {ratio:.1f}x faster ({fw['rps']:,.0f} vs {fa['rps']:,.0f} req/s)")
            else:
                ratio = fa["rps"] / fw["rps"]
                print(f"FastAPI is {ratio:.1f}x faster ({fa['rps']:,.0f} vs {fw['rps']:,.0f} req/s)")
        else:
            print("Could not compare -- one or both servers failed to start.")

    finally:
        # Clean up temp app files
        for path in (fastware_app_path, fastapi_app_path):
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    run_benchmark()
