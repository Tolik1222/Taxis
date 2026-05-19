import os
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch
from django.contrib.auth.models import User
from taxi_app.models import Order, UserProfile

class StripeWebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testpassenger", password="password")
        self.profile = UserProfile.objects.create(user=self.user, role="passenger")
        self.order = Order.objects.create(
            passenger=self.user,
            price=250.0,
            start_lat=50.4501,
            start_lon=30.5234,
            end_lat=50.4550,
            end_lon=30.5300,
            service_tier="standard",
            payment_method="card",
            payment_status="pending"
        )
        self.url = reverse('stripe_webhook')

    @patch('stripe.Webhook.construct_event')
    def test_webhook_successful_payment(self, mock_construct):
        # Налаштовуємо мок, щоб він повертав подію успішної оплати
        mock_construct.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_12345",
                    "metadata": {
                        "order_id": str(self.order.id)
                    }
                }
            }
        }
        
        # Нам потрібен налаштований STRIPE_WEBHOOK_SECRET в оточенні, щоб уникнути помилки 500
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_test_secret"}):
            response = self.client.post(
                self.url,
                data='{"payload": "test"}',
                content_type='application/json',
                HTTP_STRIPE_SIGNATURE='t=123,v1=abc'
            )
            
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"received": True})
            
            # Оновлюємо стан замовлення з бази даних і перевіряємо статус оплати
            self.order.refresh_from_db()
            self.assertEqual(self.order.payment_status, "paid")
            self.assertEqual(self.order.paddle_transaction_id, "cs_test_12345")

    @patch('stripe.Webhook.construct_event')
    def test_webhook_invalid_signature(self, mock_construct):
        # Змушуємо construct_event викинути виняток (імітація неправильного підпису)
        mock_construct.side_effect = Exception("Signature verification failed")
        
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_test_secret"}):
            response = self.client.post(
                self.url,
                data='{"payload": "test"}',
                content_type='application/json',
                HTTP_STRIPE_SIGNATURE='invalid_signature'
            )
            
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"error": "Invalid signature"})
            
            # Статус оплати повинен залишитися "pending"
            self.order.refresh_from_db()
            self.assertEqual(self.order.payment_status, "pending")

    def test_webhook_secret_not_configured(self):
        # Секретний ключ вебхука відсутній в конфігурації
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": ""}):
            response = self.client.post(
                self.url,
                data='{"payload": "test"}',
                content_type='application/json',
                HTTP_STRIPE_SIGNATURE='test_signature'
            )
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json(), {"error": "Webhook secret is not configured"})

    def test_webhook_get_method_not_allowed(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json(), {"error": "Method not allowed"})
