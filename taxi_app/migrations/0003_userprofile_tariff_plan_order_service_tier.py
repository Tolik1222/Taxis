# Generated manually for Taxi app UX

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('taxi_app', '0002_order_driver_order_passenger_alter_order_status_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='service_tier',
            field=models.CharField(
                choices=[('economy', 'Економ'), ('standard', 'Стандарт'), ('comfort', 'Комфорт')],
                default='standard',
                help_text='Клас поїздки, який обрав пасажир.',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='tariff_plan',
            field=models.CharField(
                choices=[('economy', 'Економ'), ('standard', 'Стандарт'), ('comfort', 'Комфорт')],
                default='standard',
                help_text='Тариф, з яким водій працює зміну.',
                max_length=20,
            ),
        ),
    ]
