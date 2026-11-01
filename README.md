# In-Process ASGI Caching Middleware & Stampede Mitigator

An asynchronous in-process caching proxy built with **FastAPI**, **Uvicorn**, **Redis**, and **PostgreSQL (50,000 records)**. Designed to mitigate **Cache Stampedes** via Single-Flight `asyncio.Lock` coalescing and enforce **Pre-Cache Stateless JWT (HMAC-SHA256) Role-Based Access Control**.

## Directory Structure
- `middleware/`: FastAPI application, single-flight `asyncio.Lock` middleware, JWT verifier, and telemetry endpoints.
- `frontend/`: Streamlit real-time visual telemetry dashboard and interactive stampede burst simulator.
- `locust/`: Containerized Locust stress-testing suite comparing cached vs. direct PostgreSQL queries.
- `postgres/`: SQL schema initialization seeding 50,000 synthetic enterprise product records.

## Quick Start (Docker Compose)
```bash
docker compose up --build -d
