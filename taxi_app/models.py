from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    ROLE_CHOICES = (
        ('passenger', 'Пасажир'),
        ('driver', 'Водій'),
    )
    DRIVER_TARIFF_CHOICES = (
        ('economy', 'Економ'),
        ('standard', 'Стандарт'),
        ('comfort', 'Комфорт'),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    telegram_id = models.BigIntegerField(unique=True, null=True, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='passenger')
    is_online = models.BooleanField(default=False)
    tariff_plan = models.CharField(
        max_length=20,
        choices=DRIVER_TARIFF_CHOICES,
        default='standard',
        help_text='Тариф, з яким водій працює зміну.',
    )
    phone = models.CharField(max_length=32, blank=True, default="")
    car_make = models.CharField(max_length=64, blank=True, default="")
    car_model = models.CharField(max_length=64, blank=True, default="")
    car_plate = models.CharField(max_length=16, blank=True, default="")
    driver_bio = models.CharField(max_length=160, blank=True, default="")
    is_bot_driver = models.BooleanField(default=False)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"


class Order(models.Model):
    STATUS_CHOICES = (
        ('new', 'Новий'),
        ('searching', 'Пошук водія'),
        ('in_progress', 'У дорозі'),
        ('completed', 'Завершено'),
        ('cancelled', 'Скасовано'),
    )
    SERVICE_TIER_CHOICES = (
        ('economy', 'Економ'),
        ('standard', 'Стандарт'),
        ('comfort', 'Комфорт'),
    )
    PAYMENT_METHOD_CHOICES = (
        ('cash', 'Готівка'),
        ('card', 'Картка'),
    )
    PAYMENT_STATUS_CHOICES = (
        ('not_required', 'Не потрібно'),
        ('pending', 'Очікує оплати'),
        ('paid', 'Оплачено'),
        ('failed', 'Помилка оплати'),
    )

    passenger = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders_as_passenger', null=True)
    driver = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='orders_as_driver', null=True, blank=True)
    
    passenger_name = models.CharField(max_length=100)
    service_tier = models.CharField(
        max_length=20,
        choices=SERVICE_TIER_CHOICES,
        default='standard',
        help_text='Клас поїздки, який обрав пасажир.',
    )

    start_lat = models.FloatField()
    start_lon = models.FloatField()
    end_lat = models.FloatField()
    end_lon = models.FloatField()

    distance = models.FloatField(null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='cash')
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='not_required')
    paddle_transaction_id = models.CharField(max_length=64, blank=True, default="")
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Замовлення {self.id} — {self.status}"