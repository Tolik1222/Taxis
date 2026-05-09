from django.urls import path
from .views import create_order_api, index_page

urlpatterns = [
    path('', index_page, name='index'),
    path('api/create/', create_order_api, name='create_order'),
]