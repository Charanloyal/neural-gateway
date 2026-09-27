import sys
from fastapi.testclient import TestClient
from app.main import app

def main():
    print("Beginning NeuralGateway Verification Suite...")
    with TestClient(app) as client:
        # 1. Root route
        r_root = client.get("/")
        assert r_root.status_code == 200, f"Root status {r_root.status_code}"
        assert "NeuralGateway" in r_root.text, "Brand missing in dashboard"
        print("[PASS] Root route / returns dashboard HTML (200 OK)")

        # 2. Healthz
        r_health = client.get("/healthz")
        assert r_health.status_code == 200
        health_data = r_health.json()
        assert health_data["status"] == "healthy"
        print(f"[PASS] Healthz returns healthy: mode={health_data.get('mode')}")

        # 3. Non-streaming completion
        r_comp = client.post("/v1/chat/completions", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Explain neural architectures"}],
            "stream": False
        })
        assert r_comp.status_code == 200
        content = r_comp.json()["choices"][0]["message"]["content"]
        assert len(content) > 0, "Completion content is empty!"
        print(f"[PASS] Non-streaming completion returned text: '{content[:50]}...'")

        # 4. Semantic Cache Hit test
        r_cached = client.post("/v1/chat/completions", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Explain neural architectures"}],
            "stream": False
        })
        assert r_cached.status_code == 200
        assert r_cached.headers.get("x-cache") == "HIT", f"Expected HIT, got {r_cached.headers.get('x-cache')}"
        print(f"[PASS] Semantic Cache HIT: X-Cache={r_cached.headers.get('x-cache')}, Sim={r_cached.headers.get('x-cache-similarity')}")

        # 5. Clear cache
        r_clear = client.post("/api/dashboard/clear-cache")
        assert r_clear.status_code == 200
        print("[PASS] Cache cleared successfully")

        # 6. Reset circuits
        r_res = client.post("/api/dashboard/reset-circuits")
        assert r_res.status_code == 200
        print("[PASS] Reset circuits successfully")

        # 7. Dashboard stats
        r_stats = client.get("/api/dashboard/stats")
        assert r_stats.status_code == 200
        print(f"[PASS] Dashboard stats: {r_stats.json()}")

        # 8. Streaming inference (SSE)
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello streaming world"}],
            "stream": True
        }) as r_stream:
            assert r_stream.status_code == 200
            chunks = []
            for line in r_stream.iter_lines():
                if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                    chunks.append(line)
            assert len(chunks) > 0
            print(f"[PASS] SSE Stream received {len(chunks)} token chunks")

    print("\n[SUCCESS] ALL VERIFICATION SUITE TESTS PASSED!")

if __name__ == "__main__":
    main()
