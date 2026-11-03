# In-Process ASGI Caching Middleware & Stampede Mitigator

## Project Overview
**Problem Statement:** In high-throughput web applications, when a highly popular cache key expires or is evicted, hundreds of concurrent requests for that data hit the database simultaneously. This "Cache Stampede" causes connection pool exhaustion, latency spikes, and potential database crashes.

**Brief Abstract:** This project implements an In-Process ASGI Caching Middleware with Single-Flight Concurrency Control built inside FastAPI. Paired with Redis and PostgreSQL, the middleware intercepts concurrent requests during a cache miss. It uses an in-process `asyncio.Lock` to elect a single leader coroutine to query the database, while suspending others. Once the leader updates Redis, waiting coroutines perform a double-checked read, returning coalesced cache hits and eliminating redundant database queries.

## Team Members
*   **C S Akshay Khanna Reddy** - 25BCE1113
*   **Adarsh Varshan V N** - 25BCE1055
*   **Raghav Manickam** - 25BCE1978

## Functional Features and Technology Stack
**Functional Features:**
*   **Single-Flight Request Coalescing:** Prevents database cache stampedes.
*   **Stateless Pre-Cache RBAC:** Verifies HMAC-SHA256 JWTs in memory, namespacing cache keys by user role to prevent data leakage.
*   **Visual Telemetry & Simulation:** Real-time Streamlit dashboard to simulate concurrent bursts and visualize latency/cache disposition.
*   **Load Testing:** Locust framework to simulate thousands of concurrent users and baseline performance.

**Technology Stack:**
*   **Backend:** Python 3.11, FastAPI, Uvicorn, asyncpg
*   **Database & Cache:** PostgreSQL 15, Redis 7 (LRU Policy)
*   **Frontend/Telemetry:** Streamlit, Pandas, Plotly
*   **Load Engine:** Locust
*   **Orchestration:** Docker, Docker Compose

## Database Software and Application Setup Requirements
*   **Database:** PostgreSQL 15 (alpine image) and Redis 7 (alpine image).
*   **Requirements:** Docker Desktop (or Docker Engine + Docker Compose V2) installed and running on your host machine (Windows/Ubuntu). No local Python installation is strictly required, as the entire stack is containerized.

## Steps to Create Database, Configure, and Run
Because the project is fully containerized, database creation, data loading, and application configuration are automated.

1.  Open your terminal and navigate to the root directory of the project (where `docker-compose.yml` is located).
2.  Start the multi-container stack in the background:
    ```bash
    docker compose up --build -d
    ```
    *Note: During startup, PostgreSQL automatically executes the `init.sql` script to create tables and load sample data. The middleware waits for health checks to pass before booting.*
3.  Access the applications:
    *   **Visual Dashboard:** http://localhost:8501
    *   **Locust Load UI:** http://localhost:8089
    *   **FastAPI API Docs:** http://localhost:8000/docs
4.  To shut down the application:
    ```bash
    docker compose down
    ```

## Database Schema, Relationships, and Sample Data
The project uses a flat, read-heavy catalog structure to simulate enterprise inventory.

*   **Table Name:** `products`
*   **Schema:**
    *   `id` (SERIAL PRIMARY KEY)
    *   `sku` (VARCHAR(64) NOT NULL)
    *   `name` (VARCHAR(120) NOT NULL)
    *   `category` (VARCHAR(50) NOT NULL) - *B-Tree Indexed*
    *   `price` (NUMERIC(10, 2) NOT NULL)
    *   `stock` (INT NOT NULL)
    *   `specs` (JSONB NOT NULL)
    *   `updated_at` (TIMESTAMP)
*   **Sample Data:** The `init.sql` script uses PostgreSQL's `generate_series` function to automatically synthesize and insert 50,000 distinct hardware product records (e.g., Processors, NVMe Storage) with randomized prices, stock levels, and nested JSONB specifications. 

## Test Instructions and Known Limitations
**Test Instructions:**
1.  **Stampede Test:** Open the Streamlit Dashboard (http://localhost:8501), go to the "Interactive Stampede Burst Simulator" tab, and fire 50 concurrent requests. You should observe exactly 1 DB query (`MISS`) and 49 coalesced cache hits (`HIT-COALESCED`).
2.  **Stress Test:** Open Locust (http://localhost:8089), set number of users to 100 with a spawn rate of 25, and start the test to observe caching throughput versus direct database queries.

**Known Limitations:**
1.  **Single-Worker Constraint:** The single-flight locking mechanism relies on Python's `asyncio.Lock`, which operates on a single event loop. The ASGI server must run with a single worker process (`--workers 1`). It cannot natively scale across multiple pods without implementing a distributed lock like Redis Redlock.
2.  **Write-Read Race Conditions:** The architecture uses the Cache-Aside pattern. If a `PUT` request updates a record, it issues a cache deletion immediately after the DB commit. However, a concurrent `GET` request happening in the exact same microsecond could read stale data or repopulate the cache with old data, meaning the system guarantees eventual consistency rather than strict consistency.

### Locust Load Test Initialization Parameters

![Locust Load Test Setup](Project_Screenshots/1.png)

This interface captures the parameter configuration screen of the Locust load-testing engine (`http://localhost:8089`) before initiating benchmark execution against the caching proxy:

* **Number of Users (Peak Concurrency): `10`**  
  Defines the maximum ceiling of concurrent virtual user instances that will simulate active HTTP clients.
* **Ramp Up (Users Started/Second): `5`**  
  Controls the injection rate. Locust spawns 5 new concurrent worker coroutines every second until reaching the 10-user peak target within 2 seconds, avoiding abrupt TCP buffer saturation on the host.
* **Target Host: `http://middleware:8000`**  
  Directs traffic across Docker's internal bridge network to the FastAPI ASGI middleware container using container-level DNS resolution.
* **Cluster Status: `READY` / `0 RPS` / `0% Failures`**  
  Indicates the load engine has successfully mounted the test harness script, established network access to the target host, and is primed to begin firing weighted benchmark traffic.
