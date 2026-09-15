from __future__ import annotations

import math
from datetime import datetime, timedelta

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def estimate_travel_minutes(distance_km: float, mode: str) -> int:
    speed = 4.0 if mode == "walking" else 28.0
    return max(1, math.ceil(distance_km / speed * 60))


def latlon_to_kma_grid(lat: float, lon: float) -> tuple[int, int]:
    # 기상청 DFS 격자 변환 공식
    re = 6371.00877
    grid = 5.0
    slat1 = 30.0
    slat2 = 60.0
    olon = 126.0
    olat = 38.0
    xo = 43.0
    yo = 136.0
    degrad = math.pi / 180.0
    re /= grid
    slat1 *= degrad
    slat2 *= degrad
    olon *= degrad
    olat *= degrad
    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)
    ra = math.tan(math.pi * 0.25 + lat * degrad * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * degrad - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    x = int(ra * math.sin(theta) + xo + 0.5)
    y = int(ro - ra * math.cos(theta) + yo + 0.5)
    return x, y


def latest_ultra_short_base(now: datetime) -> tuple[str, str]:
    # 초단기실황은 매시 40분 이후 이용 가능. 안전하게 45분 이전이면 1시간 더 이전 자료 사용.
    target = now - timedelta(hours=1 if now.minute < 45 else 0)
    return target.strftime("%Y%m%d"), target.strftime("%H00")
