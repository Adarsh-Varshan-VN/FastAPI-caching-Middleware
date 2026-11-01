import random
from locust import HttpUser, task, between


class ProxyStressUser(HttpUser):
    wait_time = between(0.01, 0.05)

    def on_start(self):
        """Acquire valid Admin JWT and Standard User JWT on startup."""
        admin_Id = random.choice([10, 20, 30])
        res_admin = self.client.post("/auth/token", json={"user_id": admin_Id, "role": "admin"}, name="/auth/token (Setup)")
        self.admin_token = res_admin.json().get("access_token", "") if res_admin.status_code == 200 else ""

        res_user = self.client.post("/auth/token", json={"user_id": 99, "role": "user"}, name="/auth/token (Setup)")
        self.user_token = res_user.json().get("access_token", "") if res_user.status_code == 200 else ""

    @task(6)
    def hit_hot_keys_cached(self):
        """High-contention hot keys (IDs 1-30) to stress-test single-flight stampede locking."""
        pid = random.randint(1, 30)
        self.client.get(f"/products/{pid}", name="1. [CACHED] Hot Products (IDs 1-30)")

    @task(2)
    def hit_broad_catalog_cached(self):
        """Broad catalog sweep across all 50,000 distinct records."""
        pid = random.randint(1, 50000)
        self.client.get(f"/products/{pid}", name="2. [CACHED] 50k Catalog Sweep")

    @task(2)
    def hit_uncached_baseline(self):
        """Direct PostgreSQL baseline (/nocache) for latency comparison."""
        pid = random.randint(1, 50000)
        self.client.get(f"/nocache/products/{pid}", name="3. [DIRECT DB] Uncached Baseline")

    @task(1)
    def hit_admin_analytics_authorized(self):
        """Authorized Admin JWT accessing user-scoped cached analytics."""
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        self.client.get("/admin/analytics", headers=headers, name="4. [AUTH ADMIN] Scoped Cache")
