from django.db import models
from django.contrib.auth.models import User

class Order(models.Model):
    passenger_name = models.CharField(max_length=100) # Для начала просто имя
    start_lat = models.FloatField()
    start_lon = models.FloatField()
    end_lat = models.FloatField()
    end_lon = models.FloatField()
    distance = models.FloatField(null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    status = models.CharField(max_length=20, default='new')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Заказ {self.id} - {self.passenger_name}"