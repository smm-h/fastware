"""Benchmark: cold import time for fastware vs FastAPI.

Uses subprocess to measure real cold-import time (no cached bytecode
advantage from the parent process). Each iteration is a fresh Python
process.
"""

import subprocess
import sys
import time


def measure_import(module_name: str, iterations: int = 5) -> list[float]:
    """Measure cold import time for a module across multiple subprocess runs.

    Returns a list of durations in seconds.
    """
    times = []
    script = (
        f"import time; t0 = time.perf_counter(); "
        f"import {module_name}; "
        f"print(time.perf_counter() - t0)"
    )
    for _ in range(iterations):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"  ERROR importing {module_name}: {result.stderr.strip()}")
            continue
        elapsed = float(result.stdout.strip())
        times.append(elapsed)
    return times


def run_benchmark():
    iterations = 5

    # Also measure Python baseline (no import)
    baseline_times = []
    for _ in range(iterations):
        result = subprocess.run(
            [sys.executable, "-c", "import time; print(0.0)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            # Measure the subprocess overhead by timing the whole call
            pass

    print("=" * 70)
    print("Cold Import Time Benchmark")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  Iterations: {iterations}")
    print("=" * 70)
    print()

    modules = [
        ("fastware", "fastware (core only, no server)"),
        ("fastapi", "FastAPI"),
        ("msgspec", "msgspec (fastware's serializer)"),
        ("pydantic", "Pydantic (FastAPI's serializer)"),
        ("granian", "granian (fastware's server)"),
        ("uvicorn", "uvicorn (FastAPI's server)"),
    ]

    print(f"{'Module':<40} {'mean':>10} {'min':>10} {'max':>10}")
    print("-" * 72)

    results = {}
    for module_name, label in modules:
        times = measure_import(module_name, iterations)
        if not times:
            print(f"{label:<40} {'FAILED':>10}")
            continue

        mean_ms = (sum(times) / len(times)) * 1000
        min_ms = min(times) * 1000
        max_ms = max(times) * 1000
        results[module_name] = {"mean": mean_ms, "min": min_ms, "times": times}

        print(f"{label:<40} {mean_ms:>9.1f}ms {min_ms:>9.1f}ms {max_ms:>9.1f}ms")

    print()

    # Compare framework totals
    if "fastware" in results and "fastapi" in results:
        fw_mean = results["fastware"]["mean"]
        fa_mean = results["fastapi"]["mean"]
        ratio = fa_mean / fw_mean if fw_mean > 0 else float("inf")
        print(f"FastAPI import is {ratio:.1f}x slower than fastware ({fa_mean:.1f}ms vs {fw_mean:.1f}ms)")

    if "msgspec" in results and "pydantic" in results:
        ms_mean = results["msgspec"]["mean"]
        pd_mean = results["pydantic"]["mean"]
        ratio = pd_mean / ms_mean if ms_mean > 0 else float("inf")
        print(f"Pydantic import is {ratio:.1f}x slower than msgspec ({pd_mean:.1f}ms vs {ms_mean:.1f}ms)")


if __name__ == "__main__":
    run_benchmark()
