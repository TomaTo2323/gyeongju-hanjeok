from __future__ import annotations

import json

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(timeout=60) as client:
        health = client.get(f"{BASE}/health")
        print("HEALTH", health.status_code, json.dumps(health.json(), ensure_ascii=False, indent=2))
        if health.status_code != 200:
            return 1
        payload = {
            "latitude": 35.8562,
            "longitude": 129.2247,
            "available_minutes": 240,
            "transport": "walking",
            "radius_km": 8,
            "preferences": ["역사", "조용한 곳"],
            "free_only": False,
            "indoor_preferred": False,
            "desired_course_count": 3,
            "seed": 42,
        }
        response = client.post(f"{BASE}/api/v1/courses/recommend", json=payload)
        print("RECOMMEND", response.status_code, json.dumps(response.json(), ensure_ascii=False, indent=2)[:5000])
        return 0 if response.status_code == 200 else 2


if __name__ == "__main__":
    raise SystemExit(main())
