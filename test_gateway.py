import json
import httpx
import time

BASE_URL = "http://127.0.0.1:8000"

def test_health():
    resp = httpx.get(f"{BASE_URL}/healthz")
    print(f"[1] /healthz: status={resp.status_code}, body={resp.json()}")

def test_streaming():
    print("\n[2] Testing SSE Streaming (/v1/chat/completions):")
    headers = {"Content-Type": "application/json", "X-Tenant-ID": "tenant-test"}
    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Explain neural architectures"}],
        "stream": True,
    }
    with httpx.stream("POST", f"{BASE_URL}/v1/chat/completions", headers=headers, json=payload) as resp:
        print(f"Status: {resp.status_code}, Headers: X-Cache={resp.headers.get('x-cache')}")
        for line in resp.iter_lines():
            if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                try:
                    data = json.loads(line[6:])
                    content = data["choices"][0]["delta"].get("content", "")
                    print(content, end="", flush=True)
                except Exception:
                    pass
    print("\nStream complete.")

def test_cache_hit():
    print("\n[3] Testing Semantic Cache Hit (Exact query replay):")
    headers = {"Content-Type": "application/json", "X-Tenant-ID": "tenant-test"}
    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Explain neural architectures"}],
        "stream": True,
    }
    start = time.time()
    resp = httpx.post(f"{BASE_URL}/v1/chat/completions", headers=headers, json=payload)
    duration = (time.time() - start) * 1000.0
    print(f"Status: {resp.status_code}, X-Cache: {resp.headers.get('x-cache')}, X-Cache-Similarity: {resp.headers.get('x-cache-similarity')}, Latency: {duration:.2f}ms")

def test_rate_limit():
    print("\n[4] Testing Rate Limiting (Token Bucket bursts):")
    headers = {"Content-Type": "application/json", "X-Tenant-ID": "tenant-burst"}
    payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "fast"}], "stream": True}
    got_429 = False
    for i in range(250):
        r = httpx.post(f"{BASE_URL}/v1/chat/completions", headers=headers, json=payload)
        if r.status_code == 429:
            print(f"Hit 429 after {i+1} requests! Headers: X-RateLimit-Limit={r.headers.get('x-ratelimit-limit')}, Retry-After={r.headers.get('retry-after')}")
            got_429 = True
            break
    if not got_429:
        print("Burst limit did not trip (capacity large or refilled).")

def test_metrics():
    print("\n[5] Testing Prometheus Telemetry (/metrics):")
    resp = httpx.get(f"{BASE_URL}/metrics")
    for line in resp.text.splitlines():
        if line.startswith("llm_gateway_"):
            print(f"  {line}")

if __name__ == "__main__":
    test_health()
    test_streaming()
    test_cache_hit()
    test_rate_limit()
    test_metrics()
