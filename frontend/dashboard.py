import asyncio
import os
import random
import time
import httpx
import pandas as pd
import plotly.express as px
import requests
import streamlit as st

MIDDLEWARE_URL = os.getenv("MIDDLEWARE_URL", "http://middleware:8000")

st.set_page_config(
    page_title="ASGI Caching Middleware Telemetry",
    layout="wide"
)

st.title("In-Process ASGI Caching Middleware & Stampede Mitigator")
st.caption("Real-Time Telemetry: Single-Flight (`asyncio.Lock`) Stampede Prevention • HMAC-SHA256 JWT Namespacing • 50,000 PostgreSQL Records")

# Sidebar Controls
with st.sidebar:
    st.header("Control Panel")
    auto_refresh = st.toggle("Auto-Refresh Live Telemetry (2s)", value=True)
    if st.button("Reset Redis Cache & Metrics", use_container_width=True):
        requests.post(f"{MIDDLEWARE_URL}/telemetry/reset", timeout=5)
        st.toast("Cache and metrics reset!")
    st.divider()
    st.markdown("""
    **Project Architecture**
    - **Server:** Uvicorn (Single Event Loop)
    - **Locking:** In-Process `asyncio.Lock`
    - **Cache:** Redis 7 (LRU Policy)
    - **Origin DB:** PostgreSQL 15 (50k Rows)
    - **Load Engine:** Locust (`:8089`)
    """)

# Fetch live metrics from Middleware
try:
    res = requests.get(f"{MIDDLEWARE_URL}/telemetry/metrics", timeout=3)
    metrics = res.json()
except Exception as e:
    st.error(f"Waiting for Middleware container at {MIDDLEWARE_URL}... ({e})")
    st.stop()

# Top-Level KPI Row
col1, col2, col3, col4, col5, col6 = st.columns(6)
col1.metric("Total Requests", f"{metrics['total_requests']:,}")
col2.metric("Cache Hit Ratio", f"{metrics['hit_ratio_percent']}%")
col3.metric("Stampedes Coalesced", f"{metrics['coalesced_hits']:,}", help="Concurrent requests caught by asyncio.Lock that avoided hitting PostgreSQL")
col4.metric("DB Queries Saved", f"{metrics['cache_hits'] + metrics['coalesced_hits']:,}")
col5.metric("Avg Cached Latency", f"{metrics['avg_cached_latency_ms']} ms")
col6.metric("Avg Direct DB Latency", f"{metrics['avg_uncached_latency_ms']} ms")

st.divider()

tab1, tab2, tab3 = st.tabs([
    "Live Benchmark & Stampede Telemetry",
    "Interactive Stampede Burst Simulator",
    "JWT Auth & RBAC Key Namespacing Inspector"
])

with tab1:
    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Traffic Disposition Breakdown")
        breakdown_df = pd.DataFrame({
            "Disposition": [
                "Fast-Path Cache Hit (HIT)",
                "Stampede Coalesced (HIT-COALESCED)",
                "True Cache Miss -> DB (MISS)",
                "Uncached Baseline (BYPASS)",
                "Pre-Cache Auth Blocked (401/403)"
            ],
            "Count": [
                metrics["cache_hits"],
                metrics["coalesced_hits"],
                metrics["cache_misses"],
                metrics["bypass_requests"],
                metrics["auth_blocks"]
            ]
        })
        fig1 = px.bar(
            breakdown_df,
            x="Count",
            y="Disposition",
            orientation="h",
            color="Disposition",
            text_auto=True
        )
        fig1.update_layout(showlegend=False, height=320)
        st.plotly_chart(fig1, use_container_width=True)

    with c2:
        st.subheader("Latency Comparison: Middleware vs Direct PostgreSQL")
        lat_df = pd.DataFrame({
            "Path Architecture": ["In-Process Caching Middleware", "Direct PostgreSQL (/nocache)"],
            "Avg Latency (ms)": [metrics["avg_cached_latency_ms"], metrics["avg_uncached_latency_ms"]]
        })
        fig2 = px.bar(
            lat_df,
            x="Path Architecture",
            y="Avg Latency (ms)",
            color="Path Architecture",
            text_auto=True
        )
        fig2.update_layout(showlegend=False, height=320)
        st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Live Request Stream (Last 40 Intercepted Requests)")
    if metrics["recent_events"]:
        events_df = pd.DataFrame(metrics["recent_events"])
        st.dataframe(events_df, use_container_width=True, hide_index=True)
    else:
        st.info("No requests recorded yet. Start Locust at http://localhost:8089 or run the Stampede Simulator in Tab 2.")

with tab2:
    st.subheader("Prove Single-Flight (`asyncio.Lock`) Stampede Mitigation")
    st.markdown("""
    Click below to fire **50 simultaneous asynchronous requests** at a single cold product record (`1` to `50,000`).
    Without single-flight locking, all 50 requests would hit PostgreSQL simultaneously. With our middleware, **only 1 request queries PostgreSQL (`MISS`)** while the other **49 wait on the in-process `asyncio.Lock` and read from Redis (`HIT-COALESCED`)**.
    """)

    sim_col1, sim_col2 = st.columns([1, 2])
    with sim_col1:
        concurrency = st.slider("Concurrent Burst Requests", min_value=10, max_value=100, value=50, step=10)
        target_id = st.number_input("Target Product ID (1 - 50,000)", min_value=1, max_value=50000, value=random.randint(500, 49000))
        run_burst = st.button("🚀 Fire Concurrent Stampede Burst", type="primary", use_container_width=True)

    if run_burst:
        async def fire_stampede(n: int, pid: int):
            async with httpx.AsyncClient(base_url=MIDDLEWARE_URL, timeout=15.0) as client:
                tasks = [client.get(f"/products/{pid}") for _ in range(n)]
                responses = await asyncio.gather(*tasks)
                return [
                    {
                        "Request #": idx + 1,
                        "HTTP Status": r.status_code,
                        "X-Cache Header": r.headers.get("X-Cache"),
                        "X-Cache-Key": r.headers.get("X-Cache-Key"),
                        "Latency (ms)": float(r.headers.get("X-Process-Time-Ms", 0))
                    }
                    for idx, r in enumerate(responses)
                ]

        results = asyncio.run(fire_stampede(concurrency, target_id))
        res_df = pd.DataFrame(results)
        counts = res_df["X-Cache Header"].value_counts().to_dict()

        with sim_col2:
            st.success(
                f"Burst Complete for Product #{target_id}! "
                f"DB Queries Executed (MISS): {counts.get('MISS', 0)} | "
                f"Coalesced Stampede Hits (HIT-COALESCED): {counts.get('HIT-COALESCED', 0)} | "
                f"Standard Hits (HIT): {counts.get('HIT', 0)}"
            )
            st.dataframe(res_df, use_container_width=True, hide_index=True, height=280)

with tab3:
    st.subheader("Stateless JWT Authentication & Role-Scoped Cache Keys")
    st.markdown("Test how the middleware verifies `HMAC-SHA256` signatures in memory and isolates cache keys by `role` and `user_id` before querying Redis.")
    
    auth_c1, auth_c2 = st.columns(2)
    with auth_c1:
        test_user_id = st.number_input("User ID (`sub` claim)", min_value=1, max_value=999, value=42)
        test_role = st.selectbox("User Role (`role` claim)", ["admin", "user", "tampered_signature"])
        test_auth_btn = st.button("Send Request to `GET /admin/analytics`", use_container_width=True)

    if test_auth_btn:
        with auth_c2:
            if test_role == "tampered_signature":
                headers = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjQyLCJyb2xlIjoiYWRtaW4ifQ.bad_sig"}
            else:
                tok_res = requests.post(f"{MIDDLEWARE_URL}/auth/token", json={"user_id": test_user_id, "role": test_role})
                token = tok_res.json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}

            r = requests.get(f"{MIDDLEWARE_URL}/admin/analytics", headers=headers)
            st.code(f"HTTP Status: {r.status_code}\nX-Cache: {r.headers.get('X-Cache')}\nX-Cache-Key: {r.headers.get('X-Cache-Key', 'None (Rejected Before Redis)')}\nX-Process-Time-Ms: {r.headers.get('X-Process-Time-Ms')} ms")
            st.json(r.json())

if auto_refresh:
    time.sleep(2)
    st.rerun()
