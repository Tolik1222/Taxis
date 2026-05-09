from django.contrib import admin
from .models import Order

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'passenger_name', 'price', 'distance', 'status', 'created_at')
    list_filter = ('status', 'created_at')