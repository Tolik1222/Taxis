from django.urls import path
from .views import create_order_api

urlpatterns = [
    path('api/create/', create_order_api, name='create_order'),
]