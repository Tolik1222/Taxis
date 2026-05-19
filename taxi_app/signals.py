from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from .models import Order
from .telegram_utils import notify_drivers_about_order, notify_passenger_order_status

@receiver(pre_save, sender=Order)
def track_order_status(sender, instance, **kwargs):
    if instance.pk:
        old = Order.objects.get(pk=instance.pk)
        instance._old_status = old.status
    else:
        instance._old_status = None

@receiver(post_save, sender=Order)
def order_status_changed(sender, instance, created, **kwargs):
    old_status = getattr(instance, '_old_status', None)
    
    if created and instance.status == 'searching':
        notify_drivers_about_order(instance)
    elif old_status != instance.status:
        if instance.status == 'searching':
            notify_drivers_about_order(instance)
            notify_passenger_order_status(instance, "Пошук водія розпочато!")
        elif instance.status == 'accepted':
            driver_name = instance.driver.first_name if instance.driver else "Водій"
            notify_passenger_order_status(instance, f"{driver_name} прийняв замовлення і прямує до вас.")
        elif instance.status == 'waiting':
            notify_passenger_order_status(instance, "Водій очікує вас на місці.")
        elif instance.status == 'in_progress':
            notify_passenger_order_status(instance, "Поїздка розпочалася!")
        elif instance.status == 'completed':
            notify_passenger_order_status(instance, f"Поїздку завершено. Сума: {instance.price} UAH.")
        elif instance.status == 'cancelled':
            notify_passenger_order_status(instance, "Замовлення було скасовано.")
