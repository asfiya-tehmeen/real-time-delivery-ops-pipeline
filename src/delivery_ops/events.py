"""Event schema shared by the simulator and the pipeline."""
from dataclasses import dataclass, asdict
from typing import Optional

ORDER_PLACED = "ORDER_PLACED"
DRIVER_ASSIGNED = "DRIVER_ASSIGNED"
PICKED_UP = "PICKED_UP"
IN_TRANSIT = "IN_TRANSIT"
DELIVERED = "DELIVERED"
ORDER_CANCELLED = "ORDER_CANCELLED"
DRIVER_ONLINE = "DRIVER_ONLINE"
DRIVER_OFFLINE = "DRIVER_OFFLINE"

ORDER_EVENTS = {ORDER_PLACED, DRIVER_ASSIGNED, PICKED_UP, IN_TRANSIT, DELIVERED, ORDER_CANCELLED}
DRIVER_EVENTS = {DRIVER_ONLINE, DRIVER_OFFLINE}
ALL_EVENTS = ORDER_EVENTS | DRIVER_EVENTS


@dataclass
class Event:
    event_id: str
    event_type: str
    ts: str                      # event time, ISO-8601 (naive local time)
    zone: str
    lat: float
    lon: float
    order_id: Optional[str] = None
    driver_id: Optional[str] = None
    # ORDER_PLACED only
    quoted_eta_min: Optional[float] = None
    dropoff_lat: Optional[float] = None
    dropoff_lon: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)
