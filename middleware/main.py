import asyncio
import json
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, Any

import asyncpg
import jwt
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# --- Configuration ---
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/proxy_bench")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
JWT_SECRET = os.getenv("JWT_SECRET", "vit-cse-hmac-sha256-secret-key-2026")
JWT_ALGORITHM = "HS256"
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))

# In-process Single-Flight Lock Registry
key_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Real-time Telemetry State for Output Dashboard
telemetry: Dict[str, Any] = {
    "total_requests": 0,
    "cache_hits": 0,             # Fast-path Redis hits
    "coalesced_hits": 0,         # Stampede requests saved by asyncio.Lock!
    "cache_misses": 0,           # Single leader requests that queried DB
    "bypass_requests": 0,        # Direct DB (/nocache) requests
    "auth_blocks": 0,            # 401/403 blocked before touching Redis/DB
    "db_queries_executed": 0,    # Actual SQL executions on PostgreSQL
    "cached_latencies_ms": deque(maxlen=300),
    "uncached_latencies_ms": deque(maxlen=300),
    "recent_events": deque(maxlen=40),
}


def record_event(method: str, path: str, cache_status: str, latency_ms: float, cache_key: str, status_code: int):
    telemetry["recent_events"].appendleft({
        "time": time.strftime("%H:%M:%S"),
        "method": method,
        "path": path,
        "cache_status": cache_status,
        "latency_ms": round(latency_ms, 2),
        "cache_key": cache_key,
        "status_code": status_code
    })


# --- 1. Lifespan Context Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=10,
        max_size=30
    )
    app.state.redis = redis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True
    )
    yield
    await app.state.redis.close()
    await app.state.db_pool.close()


app = FastAPI(
    title="In-Process ASGI Caching Middleware & Stampede Mitigator",
    lifespan=lifespan
)


# --- 2. Stateless HMAC-SHA256 JWT Verification ---
def verify_jwt(auth_header: str) -> dict:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = auth_header.split(" ")[1]
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="JWT session has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid HMAC-SHA256 signature")


# --- 3. In-Process ASGI Single-Flight Caching Middleware ---
@app.middleware("http")
async def single_flight_caching_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    path = request.url.path
    method = request.method

    # Do not count internal telemetry polling in benchmark stats
    if path.startswith("/telemetry") or path.startswith("/auth") or path == "/health":
        return await call_next(request)

    telemetry["total_requests"] += 1

    # Baseline uncached route or non-GET write operations bypass cache
    if method != "GET" or path.startswith("/nocache"):
        response = await call_next(request)
        latency_ms = (time.perf_counter() - start_time) * 1000
        telemetry["bypass_requests"] += 1
        if path.startswith("/nocache"):
            telemetry["uncached_latencies_ms"].append(latency_ms)
        response.headers["X-Cache"] = "BYPASS"
        response.headers["X-Process-Time-Ms"] = f"{latency_ms:.2f}"
        record_event(method, path, "BYPASS", latency_ms, "N/A (Direct DB)", response.status_code)
        return response

    # Step A: Pre-Cache JWT Authentication & Role-Based Access Control (RBAC)
    namespace = "public"
    if path.startswith("/admin"):
        try:
            claims = verify_jwt(request.headers.get("Authorization", ""))
        except HTTPException as exc:
            latency_ms = (time.perf_counter() - start_time) * 1000
            telemetry["auth_blocks"] += 1
            record_event(method, path, "AUTH-BLOCKED-401", latency_ms, "NONE (Blocked Pre-Cache)", exc.status_code)
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers={"X-Cache": "AUTH-BLOCKED", "X-Process-Time-Ms": f"{latency_ms:.2f}"}
            )

        if claims.get("role") != "admin":
            latency_ms = (time.perf_counter() - start_time) * 1000
            telemetry["auth_blocks"] += 1
            record_event(method, path, "RBAC-BLOCKED-403", latency_ms, "NONE (Blocked Pre-Cache)", 403)
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden: Admin role required"},
                headers={"X-Cache": "RBAC-BLOCKED", "X-Process-Time-Ms": f"{latency_ms:.2f}"}
            )

        # Isolate cache key by verified role and user ID
        namespace = f"role:{claims['role']}:user:{claims['sub']}"

    cache_key = f"{method}:{path}:{namespace}"
    redis_client: redis.Redis = request.app.state.redis

    # Step B: Fast Path - Uncontested Cache Hit
    cached_payload = await redis_client.get(cache_key)
    if cached_payload is not None:
        latency_ms = (time.perf_counter() - start_time) * 1000
        telemetry["cache_hits"] += 1
        telemetry["cached_latencies_ms"].append(latency_ms)
        record_event(method, path, "HIT", latency_ms, cache_key, 200)
        return Response(
            content=cached_payload,
            media_type="application/json",
            headers={
                "X-Cache": "HIT",
                "X-Cache-Key": cache_key,
                "X-Process-Time-Ms": f"{latency_ms:.2f}",
                "Cache-Control": f"public, max-age={CACHE_TTL}"
            }
        )

    # Step C: Cache Miss -> Acquire Single-Flight asyncio.Lock per cache_key
    lock = key_locks[cache_key]
    async with lock:
        # Double-Checked Locking: Check if the leader coroutine already populated Redis
        cached_payload = await redis_client.get(cache_key)
        if cached_payload is not None:
            latency_ms = (time.perf_counter() - start_time) * 1000
            telemetry["coalesced_hits"] += 1
            telemetry["cached_latencies_ms"].append(latency_ms)
            record_event(method, path, "HIT-COALESCED", latency_ms, cache_key, 200)
            return Response(
                content=cached_payload,
                media_type="application/json",
                headers={
                    "X-Cache": "HIT-COALESCED",
                    "X-Cache-Key": cache_key,
                    "X-Process-Time-Ms": f"{latency_ms:.2f}",
                    "Cache-Control": f"public, max-age={CACHE_TTL}"
                }
            )

        # Step D: True Miss (Leader Request) -> Query Downstream Route & PostgreSQL
        response = await call_next(request)

        response_body = b""
        async for chunk in response.body_iterator:
            response_body += chunk

        if response.status_code == 200 and "no-store" not in response.headers.get("Cache-Control", ""):
            await redis_client.setex(
                name=cache_key,
                time=CACHE_TTL,
                value=response_body.decode("utf-8")
            )

        latency_ms = (time.perf_counter() - start_time) * 1000
        telemetry["cache_misses"] += 1
        telemetry["cached_latencies_ms"].append(latency_ms)
        record_event(method, path, "MISS", latency_ms, cache_key, response.status_code)

        headers = dict(response.headers)
        headers["X-Cache"] = "MISS"
        headers["X-Cache-Key"] = cache_key
        headers["X-Process-Time-Ms"] = f"{latency_ms:.2f}"

        return Response(
            content=response_body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type
        )


# --- 4. API Endpoints ---

class TokenRequest(BaseModel):
    user_id: int
    role: str = "admin"


@app.post("/auth/token")
async def issue_token(req: TokenRequest):
    payload = {
        "sub": req.user_id,
        "role": req.role,
        "exp": int(time.time()) + 3600
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"access_token": token, "role": req.role, "user_id": req.user_id}


@app.get("/products/{product_id}")
async def get_product_cached(product_id: int, request: Request):
    pool: asyncpg.Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        telemetry["db_queries_executed"] += 1
        # Simulate heavy SQL join / disk I/O delay (40ms) to highlight stampede risk
        await asyncio.sleep(0.04)
        row = await conn.fetchrow(
            "SELECT id, sku, name, category, price, stock, specs FROM products WHERE id = $1",
            product_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")
        return {
            "id": row["id"],
            "sku": row["sku"],
            "name": row["name"],
            "category": row["category"],
            "price": float(row["price"]),
            "stock": row["stock"],
            "specs": json.loads(row["specs"]),
            "served_by": "PostgreSQL Origin"
        }


@app.get("/nocache/products/{product_id}")
async def get_product_uncached(product_id: int, request: Request):
    pool: asyncpg.Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        telemetry["db_queries_executed"] += 1
        await asyncio.sleep(0.04)
        row = await conn.fetchrow(
            "SELECT id, sku, name, category, price, stock, specs FROM products WHERE id = $1",
            product_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")
        return {
            "id": row["id"],
            "sku": row["sku"],
            "name": row["name"],
            "category": row["category"],
            "price": float(row["price"]),
            "stock": row["stock"],
            "specs": json.loads(row["specs"]),
            "served_by": "Direct PostgreSQL (Uncached Baseline)"
        }


class StockUpdate(BaseModel):
    stock: int


@app.put("/products/{product_id}")
async def update_product_stock(product_id: int, payload: StockUpdate, request: Request):
    pool: asyncpg.Pool = request.app.state.db_pool
    redis_client: redis.Redis = request.app.state.redis

    async with pool.acquire() as conn:
        telemetry["db_queries_executed"] += 1
        result = await conn.execute(
            "UPDATE products SET stock = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
            payload.stock, product_id
        )
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="Product not found")

    cache_key = f"GET:/products/{product_id}:public"
    await redis_client.delete(cache_key)
    return {"status": "updated", "invalidated_key": cache_key, "new_stock": payload.stock}


@app.get("/admin/analytics")
async def get_admin_analytics(request: Request):
    """Heavy aggregation across all 50,000 records, cached per admin user ID."""
    pool: asyncpg.Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        telemetry["db_queries_executed"] += 1
        await asyncio.sleep(0.06)
        stats = await conn.fetchrow("""
            SELECT 
                COUNT(*) AS total_records,
                SUM(stock) AS total_inventory_units,
                ROUND(AVG(price), 2) AS avg_unit_price,
                COUNT(DISTINCT category) AS distinct_categories
            FROM products
        """)
        return {
            "total_records": stats["total_records"],
            "total_inventory_units": stats["total_inventory_units"],
            "avg_unit_price": float(stats["avg_unit_price"]),
            "distinct_categories": stats["distinct_categories"],
            "generated_at": time.strftime("%H:%M:%S")
        }


# --- 5. Telemetry & Control Endpoints for Frontend Dashboard ---

@app.get("/telemetry/metrics")
async def get_metrics(request: Request):
    cached_list = list(telemetry["cached_latencies_ms"])
    uncached_list = list(telemetry["uncached_latencies_ms"])
    
    avg_cached = round(sum(cached_list) / len(cached_list), 2) if cached_list else 0.0
    avg_uncached = round(sum(uncached_list) / len(uncached_list), 2) if uncached_list else 0.0

    total_cache_eligible = telemetry["cache_hits"] + telemetry["coalesced_hits"] + telemetry["cache_misses"]
    hit_ratio = round(((telemetry["cache_hits"] + telemetry["coalesced_hits"]) / total_cache_eligible) * 100, 2) if total_cache_eligible > 0 else 0.0

    redis_info = await request.app.state.redis.dbsize()

    return {
        "total_requests": telemetry["total_requests"],
        "cache_hits": telemetry["cache_hits"],
        "coalesced_hits": telemetry["coalesced_hits"],
        "cache_misses": telemetry["cache_misses"],
        "bypass_requests": telemetry["bypass_requests"],
        "auth_blocks": telemetry["auth_blocks"],
        "db_queries_executed": telemetry["db_queries_executed"],
        "hit_ratio_percent": hit_ratio,
        "avg_cached_latency_ms": avg_cached,
        "avg_uncached_latency_ms": avg_uncached,
        "active_redis_keys": redis_info,
        "recent_events": list(telemetry["recent_events"])
    }


@app.post("/telemetry/reset")
async def reset_metrics(request: Request):
    await request.app.state.redis.flushdb()
    telemetry["total_requests"] = 0
    telemetry["cache_hits"] = 0
    telemetry["coalesced_hits"] = 0
    telemetry["cache_misses"] = 0
    telemetry["bypass_requests"] = 0
    telemetry["auth_blocks"] = 0
    telemetry["db_queries_executed"] = 0
    telemetry["cached_latencies_ms"].clear()
    telemetry["uncached_latencies_ms"].clear()
    telemetry["recent_events"].clear()
    return {"status": "Telemetry and Redis cache cleared"}
