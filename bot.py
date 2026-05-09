import telebot
import requests

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
bot = telebot.TeleBot(TOKEN)

API_URL = "http://127.0.0.1:8000/taxi/api/create/"

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Привіт! Надішли мені свою локацію (через скріпку), щоб замовити таксі.")

@bot.message_handler(content_types=['location'])
def handle_location(message):
    data = {
        "name": message.from_user.first_name,
        "start_lat": 50.4501,
        "start_lon": 30.5234,
        "end_lat": message.location.latitude,
        "end_lon": message.location.longitude
    }

    response = requests.post(API_URL, json=data)
    
    if response.status_code == 200:
        res_data = response.json()
        bot.send_message(message.chat.id, 
            f"✅ Замовлення прийнято!\n"
            f"Відстань: {res_data['distance']}\n"
            f"Ціна: {res_data['price']}\n"
            f"Час в дорозі: {res_data['duration']}")
    else:
        bot.send_message(message.chat.id, "Помилка при створенні замовлення.")

bot.polling()