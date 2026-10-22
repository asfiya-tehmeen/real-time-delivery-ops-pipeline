"""Static simulation and business-rule configuration."""
from dataclasses import dataclass

SLA_MINUTES = 45           # placed -> delivered must finish within this window
ETA_TOLERANCE_MIN = 5      # an ETA counts as "accurate" within +/- this many minutes
CITY_CENTER = (12.9716, 77.5946)

# Relative demand by hour of day (index = hour). Lunch and dinner peaks.
HOURLY_DEMAND = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,   # 00-07: closed
    0.02, 0.03, 0.04, 0.07,                    # 08-11
    0.12, 0.13, 0.08, 0.04,                    # 12-15 lunch peak
    0.04, 0.05, 0.07, 0.11,                    # 16-19
    0.13, 0.11, 0.05, 0.01,                    # 20-23 dinner peak
]
_total = sum(HOURLY_DEMAND)
HOURLY_DEMAND = [h / _total for h in HOURLY_DEMAND]

# Road speed multiplier by hour: congestion at peaks slows drivers down.
TRAFFIC_FACTOR = {h: 1.0 for h in range(24)}
TRAFFIC_FACTOR.update({12: 0.80, 13: 0.75, 14: 0.85, 19: 0.78, 20: 0.72, 21: 0.80})

BASE_SPEED_KMH = 22.0

# Shift templates: (start_hour, end_hour). Drivers are split evenly across them,
# so supply is thin in the morning and after 20:00 by design.
SHIFTS = [(8, 16), (12, 20), (16, 24)]


@dataclass(frozen=True)
class Zone:
    name: str
    lat: float
    lon: float
    radius_km: float
    demand_weight: float
    drivers: int
    # Extra demand multiplier at lunch (12-14) and dinner (19-21) for this zone.
    lunch_boost: float = 1.0
    dinner_boost: float = 1.0


ZONES = [
    Zone("Downtown",   12.9716, 77.5946, 2.5, 0.28, 24, lunch_boost=1.1, dinner_boost=1.2),
    Zone("Tech Park",  12.9352, 77.6245, 3.0, 0.20, 15, lunch_boost=1.6, dinner_boost=0.8),
    Zone("Old Town",   12.9507, 77.5848, 2.0, 0.14, 12, dinner_boost=1.1),
    Zone("University", 13.0125, 77.5700, 2.5, 0.12, 9, lunch_boost=1.2),
    Zone("North Hub",  13.0358, 77.5970, 3.5, 0.14, 12, dinner_boost=1.3),
    Zone("Harbor",     12.9000, 77.5700, 3.5, 0.12, 6),
]
ZONE_BY_NAME = {z.name: z for z in ZONES}
