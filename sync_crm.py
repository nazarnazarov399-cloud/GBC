import os
import sys
import logging
import argparse
import httpx
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ОБРАБОТКА АРГУМЕНТОВ КОМАНДНОЙ СТРОКИ ---
parser = argparse.ArgumentParser(description='Синхронизация заказов RetailCRM -> Supabase')
parser.add_argument('--config', type=str, default='config.env',
                    help='Путь к файлу config.env (по умолчанию config.env в текущей папке)')
args = parser.parse_args()

ENV_PATH = args.config

if not os.path.exists(ENV_PATH):
    logging.error(f"❌ Файл config.env не найден по пути: {ENV_PATH}")
    logging.info("Укажите правильный путь: python sync_crm.py --config /путь/к/config.env")
    sys.exit(1)

logging.info(f"✅ Используется config.env: {ENV_PATH}")
load_dotenv(ENV_PATH)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
RETAILCRM_URL = os.getenv("CRM_URL")
RETAILCRM_API_KEY = os.getenv("RETAILCRM_API_KEY")

if not all([SUPABASE_URL, SUPABASE_KEY, RETAILCRM_URL, RETAILCRM_API_KEY]):
    logging.error("❌ Не все переменные окружения загружены")
    sys.exit(1)

# Заголовки для Supabase
supabase_headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}
client = httpx.Client(timeout=30.0)
manager_cache = {}

def get_manager_name(manager_id):
    if manager_id in manager_cache:
        return manager_cache[manager_id]
    if not manager_id:
        return ""
    try:
        resp = client.get(
            f"{RETAILCRM_URL}/api/v5/users/{manager_id}",
            headers={"X-API-KEY": RETAILCRM_API_KEY}
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                user = data.get("user", {})
                name = user.get("fullName", "")
                manager_cache[manager_id] = name
                return name
    except:
        pass
    manager_cache[manager_id] = ""
    return ""

def get_retailcrm_orders():
    url = f"{RETAILCRM_URL}/api/v5/orders"
    params = {"limit": 100}
    headers = {"X-API-KEY": RETAILCRM_API_KEY, "Content-Type": "application/json"}
    all_orders = []
    page = 1
    while True:
        params["page"] = page
        resp = client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            logging.error(f"Ошибка API: {resp.status_code} {resp.text}")
            break
        data = resp.json()
        if not data.get("success"):
            logging.error(f"Ошибка: {data.get('errorMsg')}")
            break
        orders = data.get("orders", [])
        if not orders:
            break
        all_orders.extend(orders)
        pagination = data.get("pagination", {})
        if page >= pagination.get("totalPageCount", 1):
            break
        page += 1
    logging.info(f"Получено {len(all_orders)} позиций из RetailCRM")
    return all_orders

def parse_date(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except:
        return None

def aggregate_orders(raw_orders):
    grouped = defaultdict(lambda: {
        "total_summ": 0,
        "customer_name": "",
        "customer_email": "",
        "customer_phone": "",
        "city": "",
        "status": "",
        "manager": "",
        "manager_id": None,
        "delivery_address": "",
        "order_date": None,
        "payment_type": "",
        "paid_amount": 0,
        "items": [],
        "number": None,
        "crm_id": None
    })

    for order in raw_orders:
        number = order.get("number")
        if not number:
            continue

        total = float(order.get("totalSumm", 0))
        if total > grouped[number]["total_summ"]:
            grouped[number]["total_summ"] = total

        if not grouped[number]["customer_name"]:
            customer = order.get("customer", {})
            if customer:
                name = customer.get("fullName") or f"{customer.get('firstName','')} {customer.get('lastName','')}".strip()
                grouped[number]["customer_name"] = name
                grouped[number]["customer_email"] = customer.get("email", "")
                phones = customer.get("phones", [])
                if phones:
                    grouped[number]["customer_phone"] = phones[0].get("number", "")

                address = customer.get("address", {})
                city = address.get("city", "")
                if not city:
                    delivery = order.get("delivery", {})
                    addr = delivery.get("address", {})
                    city = addr.get("city", "")
                grouped[number]["city"] = city

                if not grouped[number]["delivery_address"]:
                    delivery = order.get("delivery", {})
                    addr = delivery.get("address", {})
                    grouped[number]["delivery_address"] = addr.get("text", "")

            grouped[number]["status"] = order.get("status", "")
            manager_id = order.get("managerId")
            if manager_id and not grouped[number]["manager"]:
                manager_name = get_manager_name(manager_id)
                grouped[number]["manager"] = manager_name
                grouped[number]["manager_id"] = manager_id

            date_str = order.get("createdAt")
            if date_str:
                grouped[number]["order_date"] = parse_date(date_str)

            grouped[number]["payment_type"] = order.get("paymentType", "")
            grouped[number]["crm_id"] = order.get("id")

        items = order.get("items", [])
        if items:
            for item in items:
                grouped[number]["items"].append({
                    "name": item.get("offer", {}).get("name", ""),
                    "price": float(item.get("initialPrice", 0)),
                    "quantity": item.get("quantity", 1),
                    "total": float(item.get("initialPrice", 0)) * item.get("quantity", 1)
                })

        grouped[number]["number"] = number

    return list(grouped.values())

def upsert_orders(orders):
    for order in orders:
        number = order["number"]
        record = {
            "number": number,
            "total_summ": order["total_summ"],
            "customer_name": order["customer_name"],
            "customer_email": order["customer_email"],
            "customer_phone": order["customer_phone"],
            "city": order["city"],
            "status": order["status"],
            "manager": order["manager"],
            "delivery_address": order["delivery_address"],
            "order_date": order["order_date"].isoformat() if order["order_date"] else None,
            "payment_type": order["payment_type"],
            "paid_amount": order["paid_amount"],
            "items": order["items"],
            "crm_id": order["crm_id"]
        }

        check_url = f"{SUPABASE_URL}/rest/v1/orders?number=eq.{number}&select=id"
        check_resp = client.get(check_url, headers=supabase_headers)
        existing = check_resp.json() if check_resp.status_code == 200 else []

        if existing:
            order_id = existing[0]["id"]
            update_url = f"{SUPABASE_URL}/rest/v1/orders?id=eq.{order_id}"
            resp = client.patch(update_url, headers=supabase_headers, json=record)
            if resp.status_code in (200, 204):
                logging.info(f"🔄 Обновлён: {number}")
            else:
                logging.error(f"Ошибка обновления {number}: {resp.text}")
        else:
            insert_url = f"{SUPABASE_URL}/rest/v1/orders"
            resp = client.post(insert_url, headers=supabase_headers, json=record)
            if resp.status_code == 201:
                logging.info(f"➕ Добавлен: {number}")
            else:
                logging.error(f"Ошибка вставки {number}: {resp.text}")

def sync():
    raw = get_retailcrm_orders()
    if not raw:
        logging.info("Нет заказов.")
        return
    aggregated = aggregate_orders(raw)
    logging.info(f"Уникальных заказов: {len(aggregated)}")
    upsert_orders(aggregated)
    logging.info("✅ Синхронизация завершена.")

if __name__ == "__main__":
    sync()
    client.close()
