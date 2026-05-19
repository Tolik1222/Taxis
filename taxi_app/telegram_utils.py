import os
from telebot import TeleBot, types
from taxi_app.models import UserProfile

def get_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    return TeleBot(token)

def notify_drivers_about_order(order):
    bot = get_bot()
    if not bot: return
    
    drivers = UserProfile.objects.filter(role='driver', is_online=True).exclude(telegram_id__isnull=True)
    for d in drivers:
        try:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Прийняти", callback_data=f"drv_accept_{order.id}"))
            bot.send_message(
                d.telegram_id, 
                f"🚕 Нове замовлення #{order.id} ({order.service_tier})\nПасажир: {order.passenger_name}\nЦіна: {order.price} UAH\nЗвідки: {order.start_lat}, {order.start_lon}\nКуди: {order.end_lat}, {order.end_lon}", 
                reply_markup=markup
            )
        except Exception:
            pass

def notify_passenger_order_status(order, text):
    bot = get_bot()
    if not bot: return
    
    if order.passenger:
        p_prof = UserProfile.objects.filter(user=order.passenger).first()
        if p_prof and p_prof.telegram_id:
            try:
                bot.send_message(p_prof.telegram_id, f"🔔 Оновлення замовлення #{order.id}:\n{text}")
            except Exception:
                pass
