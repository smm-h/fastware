"""Benchmark: msgspec (fastware) vs Pydantic (FastAPI) JSON serialization throughput.

Compares encode (Python -> JSON bytes) and decode (JSON bytes -> Python)
for a realistic nested data structure: a list of 100 user objects with
nested address sub-objects.
"""

import time

import msgspec
import pydantic


# -- Data structures: msgspec ------------------------------------------------

class MsgspecAddress(msgspec.Struct):
    street: str
    city: str
    state: str
    zip_code: str
    country: str


class MsgspecUser(msgspec.Struct):
    id: int
    name: str
    email: str
    age: int
    is_active: bool
    tags: list[str]
    address: MsgspecAddress


# -- Data structures: pydantic -----------------------------------------------

class PydanticAddress(pydantic.BaseModel):
    street: str
    city: str
    state: str
    zip_code: str
    country: str


class PydanticUser(pydantic.BaseModel):
    id: int
    name: str
    email: str
    age: int
    is_active: bool
    tags: list[str]
    address: PydanticAddress


# -- Sample data generation ---------------------------------------------------

def make_sample_data(n: int = 100) -> list[dict]:
    """Generate n user dicts with nested addresses."""
    users = []
    for i in range(n):
        users.append({
            "id": i,
            "name": f"User {i}",
            "email": f"user{i}@example.com",
            "age": 20 + (i % 50),
            "is_active": i % 3 != 0,
            "tags": [f"tag{j}" for j in range(i % 5 + 1)],
            "address": {
                "street": f"{100 + i} Main St",
                "city": f"City{i % 10}",
                "state": f"S{i % 50:02d}",
                "zip_code": f"{10000 + i}",
                "country": "US",
            },
        })
    return users


def run_benchmark():
    sample_dicts = make_sample_data(100)
    iterations = 10_000

    # Build typed objects for msgspec
    msgspec_users = [
        MsgspecUser(
            id=d["id"],
            name=d["name"],
            email=d["email"],
            age=d["age"],
            is_active=d["is_active"],
            tags=d["tags"],
            address=MsgspecAddress(**d["address"]),
        )
        for d in sample_dicts
    ]

    # Build typed objects for pydantic
    pydantic_users = [PydanticUser(**d) for d in sample_dicts]

    # Pre-encode for decode benchmarks
    msgspec_encoded = msgspec.json.encode(msgspec_users)
    pydantic_encoded = pydantic_users[0].model_json_schema()  # just for validation
    # For pydantic, use the adapter pattern
    pydantic_adapter = pydantic.TypeAdapter(list[PydanticUser])
    pydantic_encoded_bytes = pydantic_adapter.dump_json(pydantic_users)

    # -- Encode benchmark: msgspec -------------------------------------------
    t0 = time.perf_counter()
    for _ in range(iterations):
        msgspec.json.encode(msgspec_users)
    t_msgspec_encode = time.perf_counter() - t0

    # -- Encode benchmark: pydantic ------------------------------------------
    t0 = time.perf_counter()
    for _ in range(iterations):
        pydantic_adapter.dump_json(pydantic_users)
    t_pydantic_encode = time.perf_counter() - t0

    # -- Decode benchmark: msgspec -------------------------------------------
    t0 = time.perf_counter()
    for _ in range(iterations):
        msgspec.json.decode(msgspec_encoded, type=list[MsgspecUser])
    t_msgspec_decode = time.perf_counter() - t0

    # -- Decode benchmark: pydantic ------------------------------------------
    t0 = time.perf_counter()
    for _ in range(iterations):
        pydantic_adapter.validate_json(pydantic_encoded_bytes)
    t_pydantic_decode = time.perf_counter() - t0

    # -- Results -------------------------------------------------------------
    payload_size = len(msgspec_encoded)

    print("=" * 70)
    print("JSON Serialization Benchmark: msgspec vs Pydantic")
    print(f"  Data: 100 user objects with nested addresses ({payload_size:,} bytes)")
    print(f"  Iterations: {iterations:,}")
    print("=" * 70)
    print()
    print(f"{'Operation':<25} {'msgspec':>12} {'pydantic':>12} {'ratio':>10}")
    print("-" * 60)

    ratio_encode = t_pydantic_encode / t_msgspec_encode
    print(f"{'Encode (total)':<25} {t_msgspec_encode:>11.3f}s {t_pydantic_encode:>11.3f}s {ratio_encode:>9.1f}x")

    ops_msgspec_enc = iterations / t_msgspec_encode
    ops_pydantic_enc = iterations / t_pydantic_encode
    print(f"{'Encode (ops/sec)':<25} {ops_msgspec_enc:>11,.0f} {ops_pydantic_enc:>11,.0f}")

    us_msgspec_enc = (t_msgspec_encode / iterations) * 1_000_000
    us_pydantic_enc = (t_pydantic_encode / iterations) * 1_000_000
    print(f"{'Encode (us/op)':<25} {us_msgspec_enc:>11.1f} {us_pydantic_enc:>11.1f}")

    print()

    ratio_decode = t_pydantic_decode / t_msgspec_decode
    print(f"{'Decode (total)':<25} {t_msgspec_decode:>11.3f}s {t_pydantic_decode:>11.3f}s {ratio_decode:>9.1f}x")

    ops_msgspec_dec = iterations / t_msgspec_decode
    ops_pydantic_dec = iterations / t_pydantic_decode
    print(f"{'Decode (ops/sec)':<25} {ops_msgspec_dec:>11,.0f} {ops_pydantic_dec:>11,.0f}")

    us_msgspec_dec = (t_msgspec_decode / iterations) * 1_000_000
    us_pydantic_dec = (t_pydantic_decode / iterations) * 1_000_000
    print(f"{'Decode (us/op)':<25} {us_msgspec_dec:>11.1f} {us_pydantic_dec:>11.1f}")

    print()
    print(f"msgspec is {ratio_encode:.1f}x faster at encoding, {ratio_decode:.1f}x faster at decoding")


if __name__ == "__main__":
    run_benchmark()
