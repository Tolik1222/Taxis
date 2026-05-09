import requests

def get_route_info(lat1, lon1, lat2, lon2):
    url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"
    try:
        response = requests.get(url).json()
        distance_km = response['routes'][0]['distance'] / 1000
        duration_mins = response['routes'][0]['duration'] / 60
        return distance_km, duration_mins
    except:
        return None, None

def calculate_price(distance_km):
    base_price = 200
    price_per_km = 40
    return round(base_price + (distance_km * price_per_km), 2)