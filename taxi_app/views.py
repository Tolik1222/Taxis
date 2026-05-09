from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import Order
from .utils import get_route_info, calculate_price
from django.shortcuts import render

def index_page(request):
    return render(request, 'taxi_app/index.html')

@api_view(['POST'])
def create_order_api(request):
    data = request.data
    
    lat1, lon1 = data['start_lat'], data['start_lon']
    lat2, lon2 = data['end_lat'], data['end_lon']
    
    dist, dur = get_route_info(lat1, lon1, lat2, lon2)
    
    if dist is None:
        return Response({"error": "Не получилось проложить маршрут"}, status=400)
    
    final_price = calculate_price(dist)
    
    order = Order.objects.create(
        passenger_name=data.get('name', 'Анонім'),
        start_lat=lat1, start_lon=lon1,
        end_lat=lat2, end_lon=lon2,
        distance=dist,
        price=final_price
    )
    
    return Response({
        "order_id": order.id,
        "distance": f"{dist:.2f} км",
        "price": f"{final_price} грн",
        "duration": f"{int(dur)} хв"
    })