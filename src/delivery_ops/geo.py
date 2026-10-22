"""Small geo helpers."""
import math

EARTH_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def offset_point(lat: float, lon: float, east_km: float, north_km: float) -> tuple[float, float]:
    """Move a point by a local east/north offset (flat-earth approximation)."""
    dlat = north_km / 111.32
    dlon = east_km / (111.32 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def interpolate(a: tuple[float, float], b: tuple[float, float], frac: float) -> tuple[float, float]:
    return a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac
