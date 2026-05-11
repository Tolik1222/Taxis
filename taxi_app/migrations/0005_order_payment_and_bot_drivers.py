from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("taxi_app", "0004_userprofile_driver_settings_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="payment_method",
            field=models.CharField(
                choices=[("cash", "Готівка"), ("card", "Картка")],
                default="cash",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="payment_status",
            field=models.CharField(
                choices=[
                    ("not_required", "Не потрібно"),
                    ("pending", "Очікує оплати"),
                    ("paid", "Оплачено"),
                    ("failed", "Помилка оплати"),
                ],
                default="not_required",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="paddle_transaction_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="is_bot_driver",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="lat",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="lon",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
