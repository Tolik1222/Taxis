from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    ROLE_CHOICES = (
        ('passenger', 'Пасажир'),
        ('driver', 'Водій'),
        ('support', 'Підтримка'),
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
    location_city = models.CharField(max_length=120, blank=True, default="")
    is_bot_driver = models.BooleanField(default=False)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"


class Order(models.Model):
    STATUS_CHOICES = (
        ('new', 'Новий'),
        ('searching', 'Пошук водія'),
        ('accepted', 'Прийнято водієм'),
        ('waiting', 'Очікування пасажира'),
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
        ('pending_deposit', 'Очікує передплати'),
        ('deposit_paid', 'Передплачено (100 грн)'),
        ('pending_remainder', 'Очікує доплати'),
        ('paid', 'Повністю оплачено'),
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
    
    # Спліт-оплата
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    remainder_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    # Простій / очікування
    waiting_seconds = models.IntegerField(default=0)
    waiting_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    is_waiting = models.BooleanField(default=False)
    waiting_started_at = models.DateTimeField(null=True, blank=True)

    # Реєстр водіїв, які відхилили пропозицію
    declined_by = models.ManyToManyField(User, related_name='declined_orders', blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Замовлення {self.id} — {self.status}"


class PromoCode(models.Model):
    code = models.CharField(max_length=32, unique=True)
    discount_percent = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.code} ({self.discount_percent}%)"


class SupportTicket(models.Model):
    STATUS_CHOICES = (
        ("open", "Відкрито"),
        ("closed", "Закрито"),
    )
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="support_tickets")
    subject = models.CharField(max_length=160)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Ticket #{self.pk} — {self.subject}"


class SupportMessage(models.Model):
    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="support_messages")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Msg #{self.pk} in ticket #{self.ticket_id}"