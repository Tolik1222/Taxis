from django.apps import AppConfig


class TaxiAppConfig(AppConfig):
    name = 'taxi_app'

    def ready(self):
        import taxi_app.signals
