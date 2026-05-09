from django.contrib import admin
from .models import Order, UserProfile

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'passenger_name', 'service_tier', 'price', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('passenger_name', 'id')
    list_editable = ('status',)

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'tariff_plan', 'is_online', 'telegram_id')
    list_filter = ('role', 'tariff_plan', 'is_online')