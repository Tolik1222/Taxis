import requests

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
DEFAULT_CURRENCY = "UAH"


def get_route_info(lat1, lon1, lat2, lon2):
    """Повертає (distance_km, duration_mins) або (None, None)."""
    url = f"{OSRM_BASE}/{lon1},{lat1};{lon2},{lat2}?overview=false"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        payload = response.json()
        route = payload["routes"][0]
        distance_km = route["distance"] / 1000
        duration_mins = route["duration"] / 60
        return distance_km, duration_mins
    except Exception:
        # Fallback to straight line distance
        try:
            from .views import _haversine_km
            dist = _haversine_km(lat1, lon1, lat2, lon2)
            # Add 30% for road routing and assume 30 km/h average speed (2 mins per km)
            return dist * 1.3, (dist * 1.3) * 2
        except Exception:
            return 5.0, 10.0  # Safe default if all else fails

TIER_RULES = {
    'economy': {'base': 160, 'per_km': 32},
    'standard': {'base': 200, 'per_km': 40},
    'comfort': {'base': 260, 'per_km': 52},
}


def calculate_price(distance_km, tier='standard'):
    if distance_km is None:
        return None
    cfg = TIER_RULES.get(tier, TIER_RULES['standard'])
    return round(cfg['base'] + distance_km * cfg['per_km'], 2)


def calculate_price_details(distance_km, tier='standard'):
    if distance_km is None:
        return None
    cfg = TIER_RULES.get(tier, TIER_RULES['standard'])
    total = round(cfg['base'] + distance_km * cfg['per_km'], 2)
    return {
        "currency": DEFAULT_CURRENCY,
        "distance_km": round(float(distance_km), 2),
        "base_fare": round(float(cfg["base"]), 2),
        "per_km_rate": round(float(cfg["per_km"]), 2),
        "total": total,
    }


def geocode_city(city_name):
    city = (city_name or "").strip()
    if not city:
        return None, None
    url = (
        "https://nominatim.openstreetmap.org/search?"
        f"format=json&limit=1&q={requests.utils.quote(city)}"
    )
    try:
        response = requests.get(
            url,
            headers={"Accept": "application/json", "User-Agent": "TaxiPro/1.0"},
            timeout=10,
        )
        response.raise_for_status()
        items = response.json() or []
        if not items:
            return None, None
        return float(items[0]["lat"]), float(items[0]["lon"])
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None, None