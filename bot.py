import os

from telebot import types

PUBLIC_WEBAPP_URL = os.environ.get("PUBLIC_WEBAPP_URL", "http://127.0.0.1:8000/taxi/")

@bot.message_handler(commands=['start'])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = types.KeyboardButton("📍 Замовити по моїй локації", request_location=True)
    btn2 = types.KeyboardButton("🗺️ Відкрити карту (Web App)", 
                                 web_app=types.WebAppInfo(PUBLIC_WEBAPP_URL))
    markup.add(btn1)
    markup.add(btn2)
    
    welcome_text = (f"Вітаю, {message.from_user.first_name}! 👋\n\n"
                    "Я — твій професійний сервіс таксі.\n"
                    "Обери варіант замовлення нижче:")
    
    bot.send_message(message.chat.id, welcome_text, reply_markup=markup)

@bot.message_handler(content_types=['location'])
def handle_location(message):
    inline_markup = types.InlineKeyboardMarkup()
    btn_confirm = types.InlineKeyboardButton("✅ Підтвердити замовлення", callback_data="confirm")
    btn_cancel = types.InlineKeyboardButton("❌ Скасувати", callback_data="cancel")
    inline_markup.add(btn_confirm, btn_cancel)
    
    bot.send_message(message.chat.id, "Ціна знайдена! Підтверджуєте?", reply_markup=inline_markup)