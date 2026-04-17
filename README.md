# GBC Analytics — Order Monitoring System
**Test Task: AI Tools Specialist**

Программно-аппаратный комплекс (Dashboard + TG Bot) для агрегации и анализа данных из RetailCRM с использованием каскада AI-моделей.

## 🔗 Результаты
* **Dashboard (Vercel):** [https://gbc-khaki.vercel.app](https://gbc-khaki.vercel.app)
* **Repository:** [https://github.com/nazarnazarov399-cloud/GBC](https://github.com/nazarnazarov399-cloud/GBC)

---

## 🛠 Технологический стек

| Слой | Технологии |
| :--- | :--- |
| **Frontend** | React, Recharts, Lucide Icons, Vercel |
| **Backend/DB** | Supabase (PostgreSQL + RLS), Python (Telebot) |
| **Интеграция** | RetailCRM API v5, Groq API (Llama 3.3 70B) |
| **AI Workstack** | DeepSeek (Logic), Claude 3.7 (Refactoring) |

---

## 🚀 Реализованный функционал

### 1. Дашборд (React + Supabase)
* **Real-time KPI:** Оборот, количество заказов, средний чек и мониторинг VIP-сделок (≥50k ₸).
* **Визуализация:** График выручки по часам (Area Chart) и распределение по городам (Bar Chart).
* **Интерфейс:** Таблица заказов с раскрывающимся составом, адаптивная вёрстка и автообновление (30 сек).
* **Безопасность:** Защита от XSS и настройка политик RLS (Row Level Security).

### 2. Telegram-бот (Python)
* **Уведомления:** Мгновенные алерты о крупных заказах.
* **AI Analytics:** Генерация отчетов через **Llama 3.3 (Groq)**.
* **Web App:** Полноценный дашборд открывается прямо внутри Telegram.
* **Команды:** Статус системы, ТОП-5 товаров и оперативная сводка по сделкам.

---

## 🧠 Анализ работы AI-инструментов

Проект реализован в рамках R&D подхода к выбору LLM:
* **DeepSeek (V3/R1):** Основной инструмент разработки. Лучший результат в удержании контекста и генерации сложной бизнес-логики.
* **Claude 3.7 (Sonnet):** Использовался для полировки UI и рефакторинга. Потребовал ручной коррекции из-за склонности к галлюцинациям в структуре API-ответов.
* **Gemini & Copilot:** Протестированы на старте; использовались как вспомогательные инструменты для быстрых справок.

---

## 🧱 Технические решения

* **Нормализация данных:** Обработка конфликтов имен полей (`total_sum` / `total_summ`) через унифицированный метод `get_sum()`.
* **Оптимизация трафика:** Реализован механизм `visibilitychange` для остановки автообновления, когда вкладка неактивна.
* **Синхронизация:** Скрипт `sync_crm.py` с поддержкой сохранения `crm_id` для предотвращения дублей и корректной линковки заказов.

---

## 📦 Запуск проекта

```bash
# Клонирование
git clone [https://github.com/nazarnazarov399-cloud/GBC.git](https://github.com/nazarnazarov399-cloud/GBC.git)
cd GBC

# Установка зависимостей
pip install telebot httpx python-dotenv

# Настройка окружения
cp config.env.example config.env
# Отредактируйте ключи в config.env

# Синхронизация и запуск
python sync_crm.py
python bot.py
