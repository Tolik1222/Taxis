from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("taxi_app", "0005_order_payment_and_bot_drivers"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="location_city",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
