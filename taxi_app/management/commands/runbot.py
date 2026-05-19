import os
import time
from telebot import TeleBot, types, custom_filters
from telebot.storage import StateRedisStorage
from telebot.handler_backends import State, StatesGroup

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from taxi_app.models import Order, UserProfile, PromoCode
from taxi_app.utils import get_route_info, calculate_price_details, geocode_city
from taxi_app.views import _create_stripe_checkout, _get_profile, _haversine_km
from django.contrib.auth.models import User


class BotStates(StatesGroup):
    role_selection = State()

    # Passenger states
    passenger_start = State()
    passenger_dest_prompt = State()
    passenger_tier_select = State()
    passenger_payment_select = State()
    passenger_waiting_order = State()

    # Driver states
    driver_idle = State()


class Command(BaseCommand):
    help = 'Run the Telegram Bot'

    def handle(self, *args, **options):
        # We can use StateRedisStorage for production if REDIS_URL is provided, 
        # but for simplicity let's use it directly or fallback to MemoryStorage if redis is not running.
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        try:
            import redis
            r = redis.Redis.from_url(redis_url)
            r.ping()
            storage = StateRedisStorage(redis_url=redis_url)
            self.stdout.write("Using Redis storage for Bot.")
        except Exception:
            from telebot.storage import StateMemoryStorage
            storage = StateMemoryStorage()
            self.stdout.write("Redis not available, using Memory storage for Bot.")

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            self.stdout.write(self.style.ERROR("TELEGRAM_BOT_TOKEN is not set."))
            return

        bot = TeleBot(bot_token, state_storage=storage)

        def get_user_profile(from_user):
            tg_id = from_user.id
            username = from_user.username
            first_name = from_user.first_name
            
            user, created = User.objects.get_or_create(
                username=f"tg_{tg_id}",
                defaults={"first_name": first_name or ""},
            )
            profile, _ = UserProfile.objects.get_or_create(
                user=user,
                defaults={
                    "telegram_id": tg_id,
                    "role": "passenger",
                },
            )
            return profile

        def passenger_main_menu():
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add(types.KeyboardButton("🚕 Замовити таксі"))
            markup.add(types.KeyboardButton("👤 Мій профіль"), types.KeyboardButton("🎧 Підтримка"))
            markup.add(types.KeyboardButton("🔄 Змінити роль (Я Водій)"))
            return markup

        def driver_main_menu(is_online=False):
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            if is_online:
                markup.add(types.KeyboardButton("🛑 Закінчити зміну"))
                markup.add(types.KeyboardButton("📍 Оновити локацію", request_location=True))
            else:
                markup.add(types.KeyboardButton("🟢 Почати зміну", request_location=True))
            markup.add(types.KeyboardButton("👤 Мій профіль"), types.KeyboardButton("🎧 Підтримка"))
            markup.add(types.KeyboardButton("🔄 Змінити роль (Я Пасажир)"))
            return markup

        @bot.message_handler(commands=['start'])
        def start_handler(message):
            profile = get_user_profile(message.from_user)
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            markup.add(types.KeyboardButton("🙋 Пасажир"), types.KeyboardButton("🚕 Водій"))
            bot.send_message(message.chat.id, f"Вітаю, {message.from_user.first_name}! Оберіть свій режим:", reply_markup=markup)
            bot.set_state(message.from_user.id, BotStates.role_selection, message.chat.id)

        @bot.message_handler(func=lambda m: m.text == "🎧 Підтримка", state="*")
        def support_handler(message):
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Відкрити сайт підтримки", url="https://musicdownloader-3.onrender.com/taxi/"))
            bot.send_message(message.chat.id, "Звернутися до підтримки можна на нашому сайті:", reply_markup=markup)

        @bot.message_handler(state=BotStates.role_selection)
        def role_selection_handler(message):
            profile = get_user_profile(message.from_user)
            if message.text == "🙋 Пасажир":
                profile.role = "passenger"
                profile.save(update_fields=["role"])
                bot.send_message(message.chat.id, "Ви перейшли в режим пасажира.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
            elif message.text == "🚕 Водій":
                profile.role = "driver"
                profile.save(update_fields=["role"])
                bot.send_message(message.chat.id, "Ви перейшли в режим водія.", reply_markup=driver_main_menu(profile.is_online))
                bot.set_state(message.from_user.id, BotStates.driver_idle, message.chat.id)
            else:
                bot.send_message(message.chat.id, "Оберіть '🙋 Пасажир' або '🚕 Водій'.")

        @bot.message_handler(func=lambda m: m.text == "🔄 Змінити роль (Я Водій)" or m.text == "🔄 Змінити роль (Я Пасажир)", state="*")
        def switch_role_handler(message):
            profile = get_user_profile(message.from_user)
            if profile.role == 'passenger':
                profile.role = 'driver'
                profile.save(update_fields=['role'])
                bot.send_message(message.chat.id, "Режим змінено на: Водій", reply_markup=driver_main_menu(profile.is_online))
                bot.set_state(message.from_user.id, BotStates.driver_idle, message.chat.id)
            else:
                profile.role = 'passenger'
                profile.save(update_fields=['role'])
                bot.send_message(message.chat.id, "Режим змінено на: Пасажир", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)

        @bot.message_handler(func=lambda m: m.text == "👤 Мій профіль", state="*")
        def profile_handler(message):
            profile = get_user_profile(message.from_user)
            if profile.role == 'passenger':
                orders = Order.objects.filter(passenger=profile.user).order_by('-created_at')
                completed = orders.filter(status='completed').count()
                active = orders.filter(status__in=['new', 'searching', 'accepted', 'waiting', 'in_progress']).first()
                
                text = f"👤 <b>Профіль Пасажира</b>\n\n"
                text += f"Усього поїздок: {completed}\n\n"
                text += "<b>Останні поїздки:</b>\n"
                for o in orders[:5]:
                    text += f"#{o.id} - {o.get_status_display()} - {o.price} UAH\n"
                
                markup = None
                if active:
                    text += f"\nАктивне замовлення: #{active.id} ({active.get_status_display()})"
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("❌ Скасувати активне", callback_data=f"psg_cancel_{active.id}"))
                
                bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
            else:
                orders = Order.objects.filter(driver=profile.user).order_by('-created_at')
                completed = orders.filter(status='completed').count()
                total_earned = sum(o.price for o in orders.filter(status='completed') if o.price)
                active = orders.filter(status__in=['accepted', 'waiting', 'in_progress']).first()
                
                text = f"🚕 <b>Профіль Водія</b>\n\n"
                text += f"Усього поїздок: {completed}\n"
                text += f"Зароблено: {total_earned} UAH\n\n"
                text += "<b>Останні поїздки:</b>\n"
                for o in orders[:5]:
                    text += f"#{o.id} - {o.get_status_display()} - {o.price} UAH\n"
                
                markup = None
                if active:
                    text += f"\nАктивне замовлення: #{active.id} ({active.get_status_display()})"
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("❌ Відхилити активне", callback_data=f"drv_cancel_{active.id}"))
                
                bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

        @bot.callback_query_handler(func=lambda call: call.data.startswith('psg_cancel_') or call.data.startswith('drv_cancel_'))
        def cancel_order_callback(call):
            action, order_id = call.data.split('_')[1:]
            order = Order.objects.filter(id=order_id).first()
            if not order:
                bot.answer_callback_query(call.id, "Замовлення не знайдено.")
                return
            if order.status in ['completed', 'cancelled']:
                bot.answer_callback_query(call.id, "Вже завершено або скасовано.")
                return
            order.status = 'cancelled'
            order.save(update_fields=['status'])
            bot.edit_message_text(f"Замовлення #{order.id} успішно скасовано.", call.message.chat.id, call.message.message_id)

        # PASSENGER FLOW
        @bot.message_handler(func=lambda m: m.text == "🚕 Замовити таксі", state=BotStates.passenger_start)
        def order_start_handler(message):
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add(types.KeyboardButton("📍 Поділитись локацією", request_location=True))
            markup.add(types.KeyboardButton("❌ Скасувати"))
            bot.send_message(message.chat.id, "Відправте свою локацію або напишіть адресу (наприклад, 'Київ, Хрещатик 1'):", reply_markup=markup)
            bot.set_state(message.from_user.id, BotStates.passenger_dest_prompt, message.chat.id)

        @bot.message_handler(content_types=['location', 'text'], state=BotStates.passenger_dest_prompt)
        def order_receive_start(message):
            if message.text == "❌ Скасувати":
                bot.send_message(message.chat.id, "Скасовано.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                return

            lat, lon = None, None
            if message.location:
                lat, lon = message.location.latitude, message.location.longitude
            else:
                lat, lon = geocode_city(message.text)
            
            if not lat or not lon:
                bot.send_message(message.chat.id, "Не вдалося визначити координати. Спробуйте ще раз пізніше.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                return

            with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
                data['start_lat'] = lat
                data['start_lon'] = lon
            
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add(types.KeyboardButton("❌ Скасувати"))
            bot.send_message(message.chat.id, "Супер! Тепер напишіть куди їдемо (кінцева адреса):", reply_markup=markup)
            bot.set_state(message.from_user.id, BotStates.passenger_tier_select, message.chat.id)

        @bot.message_handler(content_types=['location', 'text'], state=BotStates.passenger_tier_select)
        def order_receive_dest(message):
            if message.content_type == 'text' and message.text == "❌ Скасувати":
                bot.send_message(message.chat.id, "Скасовано.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                return

            lat, lon = None, None
            if message.content_type == 'location':
                lat, lon = message.location.latitude, message.location.longitude
            else:
                lat, lon = geocode_city(message.text)
                
            if not lat or not lon:
                bot.send_message(message.chat.id, "Не вдалося знайти цю адресу або отримати координати. Спробуйте почати з початку.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                return

            with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
                data['end_lat'] = lat
                data['end_lon'] = lon
                
                dist, _ = get_route_info(data['start_lat'], data['start_lon'], lat, lon)
                if not dist:
                    bot.send_message(message.chat.id, "Не вдалося прокласти маршрут. Почнемо з початку.", reply_markup=passenger_main_menu())
                    bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                    return
                data['distance'] = dist

            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add(types.KeyboardButton("🚗 Економ"), types.KeyboardButton("🚕 Стандарт"), types.KeyboardButton("🚘 Комфорт"))
            markup.add(types.KeyboardButton("❌ Скасувати"))
            
            prices = f"Відстань: {dist:.1f} км\n"
            prices += f"🚗 Економ: {calculate_price_details(dist, 'economy')['total']} UAH\n"
            prices += f"🚕 Стандарт: {calculate_price_details(dist, 'standard')['total']} UAH\n"
            prices += f"🚘 Комфорт: {calculate_price_details(dist, 'comfort')['total']} UAH\n"
            
            bot.send_message(message.chat.id, prices + "\nОберіть клас поїздки:", reply_markup=markup)
            bot.set_state(message.from_user.id, BotStates.passenger_payment_select, message.chat.id)

        @bot.message_handler(state=BotStates.passenger_payment_select)
        def order_receive_tier(message):
            if message.text == "❌ Скасувати":
                bot.send_message(message.chat.id, "Скасовано.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                return

            tier_map = {"🚗 Економ": "economy", "🚕 Стандарт": "standard", "🚘 Комфорт": "comfort"}
            if message.text not in tier_map:
                bot.send_message(message.chat.id, "Будь ласка, оберіть з меню. Спробуйте почати з початку.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                return

            with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
                data['tier'] = tier_map[message.text]

            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add(types.KeyboardButton("💵 Готівка"), types.KeyboardButton("💳 Картка (Stripe)"))
            markup.add(types.KeyboardButton("❌ Скасувати"))
            
            bot.send_message(message.chat.id, "Оберіть спосіб оплати:", reply_markup=markup)
            bot.set_state(message.from_user.id, BotStates.passenger_waiting_order, message.chat.id)

        @bot.message_handler(state=BotStates.passenger_waiting_order)
        def order_finalize(message):
            if message.text == "❌ Скасувати":
                bot.send_message(message.chat.id, "Скасовано.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                return

            if message.text not in ["💵 Готівка", "💳 Картка (Stripe)"]:
                bot.send_message(message.chat.id, "Будь ласка, оберіть спосіб оплати з меню. Почнемо з початку.", reply_markup=passenger_main_menu())
                bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                return

            payment_method = 'cash' if message.text == "💵 Готівка" else 'card'
            
            profile = get_user_profile(message.from_user)
            
            with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
                price_info = calculate_price_details(data['distance'], data['tier'])
                price = price_info['total']
                
                deposit_amount = 0.00
                if payment_method == "card":
                    deposit_amount = min(float(price), 100.0)
                    payment_status = "pending_deposit"
                    status = "new"
                else:
                    payment_status = "not_required"
                    status = "searching"

                # Check if there is already an active order to avoid spam
                active_order = Order.objects.filter(
                    passenger=profile.user,
                    status__in=['new', 'searching', 'accepted', 'waiting', 'in_progress']
                ).first()
                if active_order:
                    bot.send_message(message.chat.id, "У вас вже є активне замовлення!", reply_markup=passenger_main_menu())
                    bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)
                    return

                order = Order.objects.create(
                    passenger=profile.user,
                    passenger_name=profile.user.first_name,
                    service_tier=data['tier'],
                    start_lat=data['start_lat'],
                    start_lon=data['start_lon'],
                    end_lat=data['end_lat'],
                    end_lon=data['end_lon'],
                    distance=data['distance'],
                    price=price,
                    payment_method=payment_method,
                    payment_status=payment_status,
                    deposit_amount=deposit_amount,
                    status=status
                )

                if payment_method == "card":
                    # Empty mock request object for _create_stripe_checkout
                    class MockRequest:
                        def build_absolute_uri(self, location):
                            return os.getenv("PUBLIC_WEBAPP_URL", "http://127.0.0.1:8000") + location
                    req = MockRequest()
                    res = _create_stripe_checkout(order, req, deposit_amount, "deposit")
                    if res.get("checkout_url"):
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("Оплатити", url=res.get("checkout_url")))
                        bot.send_message(message.chat.id, f"Замовлення #{order.id} створено.\nСплатіть депозит {deposit_amount} UAH для початку пошуку.", reply_markup=markup)
                    else:
                        bot.send_message(message.chat.id, "Помилка Stripe: " + str(res.get('error')))
                else:
                    bot.send_message(message.chat.id, f"Замовлення #{order.id} створено! Починаємо пошук водія...", reply_markup=passenger_main_menu())
                
                # Notify online drivers if status is searching
                if status == "searching":
                    pass # Handled by django signals

            bot.set_state(message.from_user.id, BotStates.passenger_start, message.chat.id)

        # DRIVER FLOW
        @bot.message_handler(func=lambda m: m.text in ["🟢 Почати зміну", "🛑 Закінчити зміну"], state=BotStates.driver_idle)
        def driver_shift(message):
            profile = get_user_profile(message.from_user)
            if message.text == "🟢 Почати зміну":
                profile.is_online = True
                
                if not profile.lat or not profile.lon:
                    # Provide default lat/lon if driver doesn't have it to receive orders
                    profile.lat = 50.4501
                    profile.lon = 30.5234
                    
                profile.save(update_fields=['is_online', 'lat', 'lon'])
                bot.send_message(message.chat.id, "Ви на лінії! Для отримання найближчих замовлень рекомендуємо відправити актуальну локацію (натисніть '📍 Оновити локацію').", reply_markup=driver_main_menu(True))
            else:
                profile.is_online = False
                profile.save(update_fields=['is_online'])
                bot.send_message(message.chat.id, "Зміну завершено.", reply_markup=driver_main_menu(False))

        @bot.message_handler(content_types=['location'], state=BotStates.driver_idle)
        def driver_location(message):
            profile = get_user_profile(message.from_user)
            profile.is_online = True
            profile.lat = message.location.latitude
            profile.lon = message.location.longitude
            profile.save(update_fields=['is_online', 'lat', 'lon'])
            bot.send_message(message.chat.id, "📍 Ваша локація оновлена. Ви на лінії та готові отримувати замовлення!", reply_markup=driver_main_menu(True))

        # We will use Callbacks for Driver order actions
        @bot.callback_query_handler(func=lambda call: call.data.startswith('drv_'))
        def driver_callback(call):
            profile = get_user_profile(call.from_user)
            if profile.role != 'driver':
                bot.answer_callback_query(call.id, "Ви не водій!")
                return

            action, order_id = call.data.split('_')[1:]
            order = Order.objects.filter(id=order_id).first()
            if not order:
                bot.answer_callback_query(call.id, "Замовлення не знайдено.")
                return

            if action == 'accept':
                if order.status != 'searching':
                    bot.answer_callback_query(call.id, "Замовлення вже прийнято або неактуальне.")
                    return
                order.driver = profile.user
                order.status = 'accepted'
                order.save(update_fields=['driver', 'status'])
                
                bot.edit_message_text(f"Ви прийняли замовлення #{order.id}.\nПасажир: {order.passenger_name}\nЗвідки: {order.start_lat}, {order.start_lon}\nКуди: {order.end_lat}, {order.end_lon}",
                                      chat_id=call.message.chat.id, message_id=call.message.message_id)
                
                bot.send_message(call.message.chat.id, "📍 Точка посадки пасажира:")
                bot.send_location(call.message.chat.id, order.start_lat, order.start_lon)
                
                bot.send_message(call.message.chat.id, "🏁 Кінцева точка (куди їхати):")
                bot.send_location(call.message.chat.id, order.end_lat, order.end_lon)
                
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("Почати очікування", callback_data=f"drv_wait_{order.id}"))
                markup.add(types.InlineKeyboardButton("Почати поїздку", callback_data=f"drv_start_{order.id}"))
                bot.send_message(call.message.chat.id, "Оберіть подальшу дію:", reply_markup=markup)

            elif action == 'wait':
                if order.driver != profile.user or order.status not in ['accepted', 'in_progress']:
                    bot.answer_callback_query(call.id, "Помилка.")
                    return
                order.is_waiting = True
                order.waiting_started_at = timezone.now()
                # Do not change status if in_progress, just set is_waiting
                if order.status == 'accepted':
                    order.status = 'waiting'
                order.save(update_fields=['is_waiting', 'waiting_started_at', 'status'])
                
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("Зупинити очікування", callback_data=f"drv_stopwait_{order.id}"))
                if order.status == 'waiting':
                    markup.add(types.InlineKeyboardButton("Почати поїздку", callback_data=f"drv_start_{order.id}"))
                else:
                    markup.add(types.InlineKeyboardButton("Завершити поїздку", callback_data=f"drv_complete_{order.id}"))
                bot.edit_message_text(f"Очікування почалося для замовлення #{order.id}.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)

            elif action == 'stopwait':
                if order.driver != profile.user or not order.is_waiting:
                    bot.answer_callback_query(call.id, "Помилка.")
                    return
                elapsed = int((timezone.now() - order.waiting_started_at).total_seconds())
                order.waiting_seconds += elapsed
                order.waiting_price = (order.waiting_seconds // 10) * 2
                order.is_waiting = False
                order.waiting_started_at = None
                if order.status == 'waiting':
                    order.status = 'accepted'
                order.save(update_fields=['waiting_seconds', 'waiting_price', 'is_waiting', 'waiting_started_at', 'status'])
                
                markup = types.InlineKeyboardMarkup()
                if order.status == 'accepted':
                    markup.add(types.InlineKeyboardButton("Почати поїздку", callback_data=f"drv_start_{order.id}"))
                    bot.edit_message_text(f"Очікування зупинено. Можна починати поїздку.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
                else:
                    markup.add(types.InlineKeyboardButton("Почати очікування", callback_data=f"drv_wait_{order.id}"))
                    markup.add(types.InlineKeyboardButton("Завершити поїздку", callback_data=f"drv_complete_{order.id}"))
                    bot.edit_message_text(f"Очікування зупинено. Поїздка триває.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)
            
            elif action == 'start':
                if order.driver != profile.user:
                    bot.answer_callback_query(call.id, "Помилка.")
                    return
                if order.is_waiting:
                    elapsed = int((timezone.now() - order.waiting_started_at).total_seconds())
                    order.waiting_seconds += elapsed
                    order.waiting_price = (order.waiting_seconds // 10) * 2
                    order.is_waiting = False
                    order.waiting_started_at = None
                
                order.status = 'in_progress'
                order.save(update_fields=['status', 'waiting_seconds', 'waiting_price', 'is_waiting', 'waiting_started_at'])
                
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("Почати очікування", callback_data=f"drv_wait_{order.id}"))
                markup.add(types.InlineKeyboardButton("Завершити поїздку", callback_data=f"drv_complete_{order.id}"))
                bot.edit_message_text(f"Поїздка #{order.id} розпочата.", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup)

            elif action == 'complete':
                if order.driver != profile.user or order.status != 'in_progress':
                    bot.answer_callback_query(call.id, "Помилка.")
                    return
                
                orig_price = float(order.price) if order.price else 0.0
                waiting_pr = float(order.waiting_price)
                final_price = orig_price + waiting_pr

                order.price = final_price
                order.remainder_amount = max(0.0, final_price - float(order.deposit_amount))
                order.status = 'completed'
                
                if order.payment_method == 'card':
                    if order.remainder_amount > 0:
                        order.payment_status = 'pending_remainder'
                    else:
                        order.payment_status = 'paid'
                else:
                    order.payment_status = 'paid'
                
                order.save(update_fields=['status', 'payment_status', 'price', 'remainder_amount'])
                
                bot.edit_message_text(f"Поїздка #{order.id} завершена. Сума: {final_price} UAH", chat_id=call.message.chat.id, message_id=call.message.message_id)



        bot.add_custom_filter(custom_filters.StateFilter(bot))

        self.stdout.write("Bot is polling...")
        bot.infinity_polling()
