import os
import time
import logging
import threading
import telebot
import httpx
from dotenv import load_dotenv

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
load_dotenv("config.env")

# --- ПРОВЕРКА КОНФИГУРАЦИИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID_STR = os.getenv("CHAT_ID")
GROQ_KEY = os.getenv("GROQ_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
CRM_BASE_URL = os.getenv("CRM_URL", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "")
INVITE_CODE = os.getenv("INVITE_CODE", "GBC2024")

if not all([BOT_TOKEN, ADMIN_CHAT_ID_STR, GROQ_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("❌ ОШИБКА: Проверьте ключи в config.env!")

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_STR)
bot = telebot.TeleBot(BOT_TOKEN)

# Формируем шаблон ссылки на заказ в RetailCRM
if CRM_BASE_URL:
    CRM_BASE_URL = CRM_BASE_URL.rstrip('/')
    CRM_ORDER_LINK_TEMPLATE = f"{CRM_BASE_URL}/orders/{{order_id}}/edit"
else:
    CRM_ORDER_LINK_TEMPLATE = None

# --- ГЛОБАЛЬНЫЕ РЕСУРСЫ ---
http_client = httpx.Client(timeout=20.0)
cache = {"data": None, "time": 0}
CACHE_TTL = 30

DB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# Для отслеживания новых заказов
last_processed_id = 0
notification_enabled = True

# Кэш разрешённых пользователей
allowed_users_cache = {"data": None, "time": 0}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_sum(order):
    return float(order.get('total_sum') or order.get('total_summ') or 0)

def get_crm_id(order):
    return order.get('crm_id') or order.get('number') or order.get('id')

def escape_html(text):
    if not text:
        return ''
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_items_short(items):
    if not items or not isinstance(items, list) or len(items) == 0:
        return '—'
    item_names = []
    for i in items[:2]:
        name = i.get('name') or 'Товар'
        qty = i.get('quantity') or 1
        item_names.append(f"{name} × {qty}")
    if len(items) > 2:
        item_names.append('…')
    return ', '.join(item_names)

def is_allowed(chat_id):
    """Проверяет, есть ли пользователь в белом списке"""
    global allowed_users_cache
    current_time = time.time()
    
    if allowed_users_cache["data"] is not None and (current_time - allowed_users_cache["time"] < 60):
        return chat_id in allowed_users_cache["data"]
    
    url = f"{SUPABASE_URL}/rest/v1/users?select=chat_id"
    try:
        res = http_client.get(url, headers=DB_HEADERS)
        if res.status_code == 200:
            data = res.json()
            allowed = {u['chat_id'] for u in data}
            allowed.add(ADMIN_CHAT_ID)
            allowed_users_cache["data"] = allowed
            allowed_users_cache["time"] = current_time
            return chat_id in allowed
    except Exception as e:
        logging.error(f"Ошибка проверки пользователя: {e}")
    return chat_id == ADMIN_CHAT_ID

def add_user_to_db(chat_id, username, first_name):
    """Добавляет пользователя в белый список"""
    url = f"{SUPABASE_URL}/rest/v1/users"
    data = {
        "chat_id": chat_id,
        "username": username,
        "first_name": first_name,
        "added_by": ADMIN_CHAT_ID
    }
    try:
        res = http_client.post(url, headers=DB_HEADERS, json=data)
        if res.status_code == 201:
            allowed_users_cache["time"] = 0
            return True
    except:
        pass
    return False

def delete_user_from_db(chat_id):
    """Удаляет пользователя из белого списка"""
    url = f"{SUPABASE_URL}/rest/v1/users?chat_id=eq.{chat_id}"
    try:
        res = http_client.delete(url, headers=DB_HEADERS)
        if res.status_code in (200, 204):
            allowed_users_cache["time"] = 0
            return True
    except:
        pass
    return False

def list_users():
    """Возвращает список разрешённых пользователей"""
    url = f"{SUPABASE_URL}/rest/v1/users?select=chat_id,username,first_name"
    try:
        res = http_client.get(url, headers=DB_HEADERS)
        if res.status_code == 200:
            return res.json()
    except:
        pass
    return []

# --- МОДУЛЬ ДАННЫХ (SUPABASE) ---

def fetch_orders(force_refresh=False, limit=100):
    current_time = time.time()
    if not force_refresh and cache["data"] is not None and (current_time - cache["time"] < CACHE_TTL):
        return cache["data"]

    url = f"{SUPABASE_URL}/rest/v1/orders?select=*&order=id.desc&limit={limit}"
    try:
        res = http_client.get(url, headers=DB_HEADERS)
        if res.status_code != 200:
            logging.error(f"Supabase error {res.status_code}: {res.text}")
            return cache["data"] if cache["data"] is not None else None

        data = res.json()
        if not isinstance(data, list):
            logging.error("Supabase returned non-list")
            return cache["data"] if cache["data"] is not None else []

        cache["data"] = data
        cache["time"] = current_time
        return data
    except Exception as e:
        logging.error(f"DB fetch error: {e}")
        return cache["data"]

def get_max_order_id():
    url = f"{SUPABASE_URL}/rest/v1/orders?select=id&order=id.desc&limit=1"
    try:
        res = http_client.get(url, headers=DB_HEADERS)
        if res.status_code == 200:
            data = res.json()
            if data:
                return data[0]['id']
    except:
        pass
    return 0

# --- МОДУЛЬ AI (GROQ) ---

def get_groq_prediction(data):
    if not data:
        return "Нет данных для анализа."

    summary = "\n".join([
        f"• {o.get('category', 'Общее')}: {get_sum(o):,.0f} ₸"
        for o in data[:12] if o.get('category')
    ])
    if not summary:
        return "Нет категорий для анализа."

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "Ты — финансовый аналитик CRM. Дай 3 кратких вывода и 1 совет по увеличению прибыли."},
            {"role": "user", "content": f"Данные последних сделок:\n{summary}"}
        ],
        "temperature": 0.5
    }

    try:
        res = http_client.post(url, headers=headers, json=payload)
        if res.status_code == 200:
            return res.json()['choices'][0]['message']['content']
        else:
            return f"⚠️ Ошибка ИИ (Код {res.status_code})"
    except Exception as e:
        return f"⚠️ Сбой нейросети: {e}"

# --- ФОНОВАЯ ПРОВЕРКА НОВЫХ ЗАКАЗОВ ---

def check_new_orders_loop():
    global last_processed_id, notification_enabled
    logging.info("🔁 Запущен мониторинг новых заказов...")
    last_processed_id = get_max_order_id()

    while True:
        try:
            if not notification_enabled:
                time.sleep(60)
                continue

            url = f"{SUPABASE_URL}/rest/v1/orders?select=*&id=gt.{last_processed_id}&order=id.asc"
            res = http_client.get(url, headers=DB_HEADERS)
            if res.status_code == 200:
                new_orders = res.json()
                for order in new_orders:
                    order_id = order['id']
                    total = get_sum(order)
                    if total >= 50000:
                        customer = escape_html(order.get('customer_name', 'Клиент'))
                        city = escape_html(order.get('city', '—'))
                        manager = escape_html(order.get('manager', '—'))
                        items_short = format_items_short(order.get('items', []))
                        crm_id = get_crm_id(order)

                        message = f"🚀 <b>КРУПНЫЙ ЗАКАЗ!</b>\n"
                        message += f"<code>──────────────</code>\n"
                        message += f"💰 Сумма: <b>{total:,.0f} ₸</b>\n"
                        message += f"👤 Клиент: {customer}\n"
                        message += f"🏙 Город: {city}\n"
                        message += f"👔 Менеджер: {manager}\n"
                        message += f"📦 Состав: {items_short}\n"

                        if CRM_ORDER_LINK_TEMPLATE:
                            link = CRM_ORDER_LINK_TEMPLATE.format(order_id=crm_id)
                            message += f"🔗 <a href='{link}'>Открыть в CRM</a>"
                        else:
                            message += f"🆔 ID: {crm_id}"

                        notify_all_users(message)
                    last_processed_id = max(last_processed_id, order_id)
        except Exception as e:
            logging.error(f"Ошибка в мониторинге: {e}")
        time.sleep(60)

def notify_all_users(message):
    url = f"{SUPABASE_URL}/rest/v1/users?select=chat_id"
    try:
        res = http_client.get(url, headers=DB_HEADERS)
        if res.status_code == 200:
            users = res.json()
            for user in users:
                try:
                    bot.send_message(user['chat_id'], message, parse_mode="HTML", disable_web_page_preview=False)
                except:
                    pass
        try:
            bot.send_message(ADMIN_CHAT_ID, message, parse_mode="HTML", disable_web_page_preview=False)
        except:
            pass
    except:
        pass

threading.Thread(target=check_new_orders_loop, daemon=True).start()

# --- ИНТЕРФЕЙС ---

def safe_send(chat_id, text, markup=None):
    try:
        return bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=False)
    except:
        try:
            return bot.send_message(chat_id, text, reply_markup=markup)
        except:
            pass

def main_markup():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📊 Дашборд", "📊 Полный отчет")
    markup.row("📊 Статус", "📦 ТОП товаров")
    markup.row("🏆 ТОП-5 сделок", "🔮 AI Анализ")
    return markup

def admin_markup():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📊 Дашборд", "📊 Полный отчет")
    markup.row("📊 Статус", "📦 ТОП товаров")
    markup.row("🏆 ТОП-5 сделок", "🔮 AI Анализ")
    markup.row("👥 Пользователи", "🔗 Пригласить")
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    args = message.text.split()
    
    if len(args) > 1 and args[1] == INVITE_CODE:
        username = message.from_user.username or ''
        first_name = message.from_user.first_name or ''
        add_user_to_db(chat_id, username, first_name)
        safe_send(chat_id, "✅ Доступ разрешён! Используйте меню для навигации.", main_markup())
        return
    
    if is_allowed(chat_id):
        is_admin = (chat_id == ADMIN_CHAT_ID)
        welcome = (
            "<b>GBC Analytics</b> — система мониторинга продаж.\n\n"
            "<b>Возможности:</b>\n"
            "• Мгновенные уведомления о заказах от 50 000 ₸\n"
            "• Дашборд с ключевыми метриками\n"
            "• Отчёты по выручке и товарам\n"
            "• AI-аналитика продаж\n\n"
            "Для навигации используйте меню."
        )
        safe_send(chat_id, welcome, admin_markup() if is_admin else main_markup())
    else:
        safe_send(chat_id, "⛔ Доступ запрещён. Используйте пригласительную ссылку.")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    chat_id = message.chat.id
    text = message.text
    
    if not is_allowed(chat_id):
        safe_send(chat_id, "⛔ Доступ запрещён.")
        return
    
    is_admin = (chat_id == ADMIN_CHAT_ID)

    if text == "📊 Дашборд":
        if not DASHBOARD_URL:
            return safe_send(chat_id, "❌ Ссылка на дашборд не настроена.")
        markup = telebot.types.InlineKeyboardMarkup()
        web_app = telebot.types.WebAppInfo(DASHBOARD_URL)
        markup.add(telebot.types.InlineKeyboardButton("📊 Открыть дашборд", web_app=web_app))
        safe_send(chat_id, "Нажмите, чтобы открыть дашборд:", markup)

    elif text == "📊 Полный отчет":
        orders = fetch_orders(force_refresh=True)
        if not orders:
            return safe_send(chat_id, "❌ Нет данных.")
        total = sum(get_sum(o) for o in orders)
        report = (
            f"📊 <b>ПОЛНЫЙ ОТЧЕТ</b>\n"
            f"<code>══════════════════</code>\n"
            f"💰 Оборот: <b>{total:,.0f} ₸</b>\n"
            f"📦 Заказов: <b>{len(orders)}</b>\n"
            f"📈 Средний чек: <b>{total/len(orders):,.0f} ₸</b>\n"
            f"<code>══════════════════</code>\n"
            f"📋 <b>ПОСЛЕДНИЕ ЗАКАЗЫ:</b>\n"
        )
        for o in orders[:10]:
            summ = get_sum(o)
            customer = escape_html(o.get('customer_name', '—'))
            city = escape_html(o.get('city', '—'))
            report += f"• <b>{summ:,.0f}₸</b> | {customer} | {city}\n"
        safe_send(chat_id, report)

    elif text == "📊 Статус":
        orders = fetch_orders()
        if not orders:
            return safe_send(chat_id, "❌ Нет данных.")
        total = sum(get_sum(o) for o in orders)
        large = len([o for o in orders if get_sum(o) >= 50000])
        avg = total / len(orders) if orders else 0
        status_text = (
            f"📊 <b>СТАТУС</b>\n"
            f"<code>──────────</code>\n"
            f"💰 Оборот: <b>{total:,.0f} ₸</b>\n"
            f"📦 Заказов: <b>{len(orders)}</b>\n"
            f"📈 Средний чек: <b>{avg:,.0f} ₸</b>\n"
            f"🚀 Крупных: <b>{large}</b>"
        )
        safe_send(chat_id, status_text)

    elif text == "📦 ТОП товаров":
        orders = fetch_orders()
        if not orders:
            return safe_send(chat_id, "❌ Нет данных.")
        product_counts = {}
        for o in orders:
            items = o.get('items') or []
            if isinstance(items, list):
                for item in items:
                    name = item.get('name') or 'Товар'
                    qty = item.get('quantity') or 1
                    product_counts[name] = product_counts.get(name, 0) + qty
        top = sorted(product_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        if not top:
            return safe_send(chat_id, "Нет данных о товарах.")
        res = "📦 <b>ТОП-5 ТОВАРОВ</b>\n<code>──────────</code>\n"
        for i, (name, qty) in enumerate(top, 1):
            res += f"{i}. <b>{name}</b> — {qty} шт.\n"
        safe_send(chat_id, res)

    elif text == "🏆 ТОП-5 сделок":
        orders = fetch_orders()
        if not orders:
            return safe_send(chat_id, "❌ Нет данных.")
        top = sorted(orders, key=lambda x: get_sum(x), reverse=True)[:5]
        res = "🏆 <b>ТОП-5 СДЕЛОК</b>\n<code>──────────</code>\n"
        for i, o in enumerate(top, 1):
            val = get_sum(o)
            customer = escape_html(o.get('customer_name', '—'))
            crm_id = get_crm_id(o)
            if CRM_ORDER_LINK_TEMPLATE:
                link = CRM_ORDER_LINK_TEMPLATE.format(order_id=crm_id)
                res += f"{i}. <b>{val:,.0f} ₸</b> — <a href='{link}'>{customer}</a>\n"
            else:
                res += f"{i}. <b>{val:,.0f} ₸</b> — {customer}\n"
        safe_send(chat_id, res)

    elif text == "🔮 AI Анализ":
        safe_send(chat_id, "🤖 <i>Анализирую данные...</i>")
        orders = fetch_orders()
        insight = get_groq_prediction(orders)
        safe_send(chat_id, f"🔮 <b>AI АНАЛИЗ</b>\n<code>──────────</code>\n{insight}")

    elif text == "🔗 Пригласить" and is_admin:
        bot_username = bot.get_me().username
        invite_link = f"https://t.me/{bot_username}?start={INVITE_CODE}"
        safe_send(chat_id, f"🔗 Пригласительная ссылка:\n{invite_link}\n\nОтправьте её сотруднику. После перехода по ссылке он получит доступ.")

    elif text == "👥 Пользователи" and is_admin:
        users = list_users()
        if not users:
            return safe_send(chat_id, "Список пользователей пуст.")
        
        markup = telebot.types.InlineKeyboardMarkup()
        for u in users:
            name = u.get('first_name') or u.get('username') or str(u['chat_id'])
            btn_text = f"❌ {name}"
            callback_data = f"delete_user_{u['chat_id']}"
            markup.add(telebot.types.InlineKeyboardButton(btn_text, callback_data=callback_data))
        
        safe_send(chat_id, "<b>Выберите пользователя для удаления:</b>", markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_user_"))
def callback_delete_user(call):
    chat_id = call.message.chat.id
    if chat_id != ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "⛔ Только администратор")
        return
    
    user_id = int(call.data.replace("delete_user_", ""))
    
    if user_id == ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "⚠️ Нельзя удалить самого себя")
        return
    
    if delete_user_from_db(user_id):
        bot.answer_callback_query(call.id, "✅ Пользователь удалён")
        users = list_users()
        if users:
            markup = telebot.types.InlineKeyboardMarkup()
            for u in users:
                name = u.get('first_name') or u.get('username') or str(u['chat_id'])
                btn_text = f"❌ {name}"
                callback_data = f"delete_user_{u['chat_id']}"
                markup.add(telebot.types.InlineKeyboardButton(btn_text, callback_data=callback_data))
            bot.edit_message_text(
                "<b>Выберите пользователя для удаления:</b>",
                chat_id,
                call.message.message_id,
                reply_markup=markup
            )
        else:
            bot.edit_message_text(
                "Список пользователей пуст.",
                chat_id,
                call.message.message_id
            )
    else:
        bot.answer_callback_query(call.id, "❌ Ошибка удаления")

if __name__ == "__main__":
    logging.info("🚀 GBC Analytics v5.2 стартует...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
