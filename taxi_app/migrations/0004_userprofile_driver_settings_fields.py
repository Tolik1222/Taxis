# Generated manually for driver settings fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("taxi_app", "0003_userprofile_tariff_plan_order_service_tier"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="phone",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="car_make",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="car_model",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="car_plate",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="driver_bio",
            field=models.CharField(blank=True, default="", max_length=160),
        ),
    ]

