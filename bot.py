import asyncio
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
import aiosqlite
import smtplib
from email.mime.text import MIMEText
from openpyxl import Workbook
import os
import json
import datetime

# Настройки
BOT_TOKEN = '8034399145:AAEVIsikLZDVD3aGMJ8cDZaeTN91VOivAHM'  # Вставь токен от @BotFather
YOUR_ADMIN_ID = 6329978401  # Вставь свой Telegram ID
ADMIN_EMAIL = 'swear000@yandex.ru'  # Вставь свой Яндекс email
EMAIL_PASSWORD = 'cobrcbopfkzzfisr'  # Вставь пароль приложения от Яндекса

# Константы для очистки корзины
CART_LIFETIME_DAYS = 14  # Корзина хранится 2 недели
CLEANUP_INTERVAL_HOURS = 24  # Проверка и очистка старых корзин раз в 24 часа

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# --- Состояния для FSM ---
class AdminStates(StatesGroup):
    add_product_name = State()
    add_product_price = State()
    add_product_description = State()
    add_product_quantity = State()
    update_quantity_select_product = State()
    update_quantity_enter_new_qty = State()
    edit_product_select = State()
    edit_product_menu = State()
    edit_product_name_input = State()
    edit_product_price_input = State()
    edit_product_description_input = State()

class UserStates(StatesGroup):
    awaiting_delivery_name = State()
    awaiting_delivery_address = State()
    awaiting_delivery_phone = State()
    awaiting_order_confirmation = State()  # Новое состояние для подтверждения заказа
    cart_remove_select_item = State()
    cart_change_qty_enter_new_qty = State()

# --- Глобальные переменные ---
user_cart = {}  # user_id -> list of (product_id, quantity)

# Регистрация роутера
dp.include_router(router)

# --- Вспомогательные функции для работы с корзиной в БД ---
async def load_user_cart_from_db(user_id):
    """Загружает корзину пользователя из базы данных."""
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT cart_items FROM user_carts WHERE user_id = ?", (user_id,))
        result = await cursor.fetchone()
        if result and result[0]:
            print(f"DEBUG: Loaded cart for user {user_id}: {result[0]}")
            return json.loads(result[0])
        print(f"DEBUG: No cart found or empty cart for user {user_id}")
        return []

async def save_user_cart_to_db(user_id, cart):
    """Сохраняет корзину пользователя в базу данных и обновляет метку времени."""
    async with aiosqlite.connect("products.db") as db:
        cart_json = json.dumps(cart)
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        await db.execute(
            "INSERT OR REPLACE INTO user_carts (user_id, cart_items, last_updated) VALUES (?, ?, ?)",
            (user_id, cart_json, current_time)
        )
        await db.commit()
        print(f"DEBUG: Saved cart for user {user_id}: {cart_json} at {current_time}")

async def init_db():
    async with aiosqlite.connect("products.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                price INTEGER,
                description TEXT,
                quantity INTEGER
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                delivery_info TEXT,
                status TEXT DEFAULT 'Оформлен'
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                product_id INTEGER,
                quantity INTEGER
            )""")
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_carts'")
        user_carts_exists = await cursor.fetchone()
        if not user_carts_exists:
            print("DEBUG: Creating new 'user_carts' table with 'last_updated' column.")
            await db.execute("""
                CREATE TABLE user_carts (
                    user_id INTEGER PRIMARY KEY,
                    cart_items TEXT,
                    last_updated TEXT DEFAULT CURRENT_TIMESTAMP
                )""")
        else:
            cursor = await db.execute("PRAGMA table_info(user_carts)")
            columns = [col[1] for col in await cursor.fetchall()]
            if 'last_updated' not in columns:
                print("DEBUG: 'last_updated' column missing in 'user_carts'. Performing schema migration.")
                await db.execute("ALTER TABLE user_carts RENAME TO _old_user_carts")
                await db.execute("""
                    CREATE TABLE user_carts (
                        user_id INTEGER PRIMARY KEY,
                        cart_items TEXT,
                        last_updated TEXT DEFAULT CURRENT_TIMESTAMP
                    )""")
                await db.execute("""
                    INSERT INTO user_carts (user_id, cart_items, last_updated)
                    SELECT user_id, cart_items, CURRENT_TIMESTAMP FROM _old_user_carts
                """)
                await db.execute("DROP TABLE _old_user_carts")
                print("DEBUG: Schema migration for 'user_carts' complete.")
        await db.commit()

async def send_email(subject, body):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = ADMIN_EMAIL
    msg['To'] = ADMIN_EMAIL
    try:
        with smtplib.SMTP_SSL('smtp.yandex.ru', 465) as server:
            server.login(ADMIN_EMAIL, EMAIL_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"Email error: {e}")

async def notify_admin(message_text):
    try:
        await bot.send_message(YOUR_ADMIN_ID, message_text)
    except Exception as e:
        print(f"Telegram notify error: {e}")

# --- Фоновая задача для очистки старых корзин ---
async def clear_old_carts_task():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_HOURS * 3600)
        print("DEBUG: Running old carts cleanup task...")
        async with aiosqlite.connect("products.db") as db:
            threshold_time = datetime.datetime.now() - datetime.timedelta(days=CART_LIFETIME_DAYS)
            threshold_time_str = threshold_time.strftime('%Y-%m-%d %H:%M:%S')
            await db.execute("DELETE FROM user_carts WHERE last_updated < ?", (threshold_time_str,))
            await db.commit()
            print(f"DEBUG: Old carts cleanup complete. Removed carts older than {threshold_time_str}")

# --- Основные команды и кнопки ---
@router.message(Command("start"))
async def start(message: types.Message):
    keyboard = [
        [KeyboardButton(text="📋 Каталог")],
        [KeyboardButton(text="🛒 Корзина")],
        [KeyboardButton(text="📦 Мои заказы")]
    ]
    if message.from_user.id == YOUR_ADMIN_ID:
        keyboard += [
            [KeyboardButton(text="📥 Заказы (админ)"), KeyboardButton(text="📤 Экспорт заказов")],
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="🔄 Обновить количество")],
            [KeyboardButton(text="✏️ Редактировать товар")]
        ]
    await message.answer("Добро пожаловать!", reply_markup=ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True))

@router.message(F.text == "📋 Каталог")
async def catalog(message: types.Message):
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT id, name, price, quantity FROM products")
        products = await cursor.fetchall()
    
    buttons = [[InlineKeyboardButton(text=f"{name} ({qty} шт) - {price / 100}₽", callback_data=f"add_{id}")]
               for id, name, price, qty in products if qty > 0]
    
    if buttons:
        await message.answer("Выберите товары:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        await message.answer("Каталог пуст.")

@router.callback_query(F.data.startswith("add_"))
async def add_to_cart(callback: types.CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT quantity FROM products WHERE id = ?", (product_id,))
        product_qty_in_db = (await cursor.fetchone())[0]

    cart = await load_user_cart_from_db(user_id)
    user_cart[user_id] = cart

    current_qty_in_cart = 0
    for idx, (pid, qty) in enumerate(cart):
        if pid == product_id:
            current_qty_in_cart = qty
            break
    
    if current_qty_in_cart < product_qty_in_db:
        for idx, (pid, qty) in enumerate(cart):
            if pid == product_id:
                cart[idx] = (pid, qty + 1)
                break
        else:
            cart.append((product_id, 1))
        await save_user_cart_to_db(user_id, cart)
        await callback.answer("Добавлено в корзину")
    else:
        await callback.answer("Извините, этого товара больше нет в наличии.", show_alert=True)

@router.message(F.text == "🛒 Корзина")
async def view_cart(message: types.Message):
    user_id = message.from_user.id
    print(f"DEBUG: view_cart called for user {user_id}")
    cart = await load_user_cart_from_db(user_id)
    user_cart[user_id] = cart

    if not cart:
        await message.answer("Ваша корзина пуста")
        return
    
    text = "🛒 **Ваша корзина**:\n\n"
    total_price = 0
    buttons = []
    async with aiosqlite.connect("products.db") as db:
        for pid, qty in cart:
            cursor = await db.execute("SELECT name, price FROM products WHERE id = ?", (pid,))
            result = await cursor.fetchone()
            if result:
                name, price = result
                item_total = price * qty / 100
                text += f"**{name}** — {qty} шт — {item_total:.2f}₽\n"
                total_price += item_total
                buttons.append([
                    InlineKeyboardButton(text=f"✏️ Изменить кол-во ({name})", callback_data=f"change_qty_item_id_{pid}"),
                    InlineKeyboardButton(text=f"🗑️ Удалить ({name})", callback_data=f"remove_item_id_{pid}")
                ])
            else:
                text += f"Товар ID {pid} (удалён из каталога) — {qty} шт\n"
                buttons.append([
                    InlineKeyboardButton(text=f"🗑️ Удалить (Товар ID {pid})", callback_data=f"remove_item_id_{pid}")
                ])
    
    text += f"\n**Итого**: {total_price:.2f}₽"
    
    markup = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Каталог")],
        [KeyboardButton(text="✅ Оформить заказ")],
        [KeyboardButton(text="🗑️ Очистить корзину")]
    ], resize_keyboard=True)
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await message.answer("Выберите действие:", reply_markup=markup)

@router.message(F.text == "🗑️ Очистить корзину")
async def clear_cart(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    cart = await load_user_cart_from_db(user_id)
    user_cart[user_id] = cart

    if not cart:
        await message.answer("Ваша корзина уже пуста.")
        await start(message)  # Возвращаем в главное меню
        return

    await save_user_cart_to_db(user_id, [])
    user_cart[user_id] = []
    await message.answer("Ваша корзина очищена.")
    await state.clear()
    await start(message)  # Возвращаем в главное меню

@router.callback_query(F.data.startswith("remove_item_id_"))
async def cart_remove_item_confirm(callback: types.CallbackQuery, state: FSMContext):
    product_id_to_remove = int(callback.data.split("_")[3])
    user_id = callback.from_user.id
    
    cart = await load_user_cart_from_db(user_id)
    user_cart[user_id] = cart

    user_cart[user_id] = [(pid, qty) for pid, qty in cart if pid != product_id_to_remove]
    
    await save_user_cart_to_db(user_id, user_cart[user_id])
    
    await callback.message.edit_text("Товар успешно удалён из корзины.", reply_markup=None)
    await view_cart(callback.message)
    await callback.answer()
    await state.clear()

@router.callback_query(F.data.startswith("change_qty_item_id_"))
async def cart_change_qty_selected(callback: types.CallbackQuery, state: FSMContext):
    product_id_to_change = int(callback.data.split("_")[4])
    await state.update_data(product_id_to_change_qty=product_id_to_change)
    
    user_id = callback.from_user.id
    cart = await load_user_cart_from_db(user_id)
    user_cart[user_id] = cart

    current_qty = 0
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT name FROM products WHERE id = ?", (product_id_to_change,))
        result = await cursor.fetchone()
        product_name = result[0] if result else f"Товар ID {product_id_to_change}"
    
    for pid, qty in cart:
        if pid == product_id_to_change:
            current_qty = qty
            break

    await state.set_state(UserStates.cart_change_qty_enter_new_qty)
    await callback.message.answer(f"Введите новое количество для товара **{product_name}** (текущее: {current_qty} шт). Введите 0 для удаления товара из корзины:", parse_mode="Markdown")
    await callback.answer()

@router.message(UserStates.cart_change_qty_enter_new_qty)
async def cart_change_qty_process(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        new_quantity = int(message.text)
        if new_quantity < 0:
            raise ValueError("Количество не может быть отрицательным.")
        
        data = await state.get_data()
        product_id = data.get('product_id_to_change_qty')

        if not product_id:
            await message.answer("Произошла ошибка. Пожалуйста, попробуйте снова.")
            await state.clear()
            return

        async with aiosqlite.connect("products.db") as db:
            cursor = await db.execute("SELECT name, quantity FROM products WHERE id = ?", (product_id,))
            result = await cursor.fetchone()
            if not result:
                await message.answer("Извините, этот товар был удалён из каталога. Вы можете удалить его из корзины, нажав 'Удалить'.")
                await state.clear()
                await view_cart(message)
                return

            product_name, available_qty_in_db = result

        if new_quantity > available_qty_in_db:
            await message.answer(f"Извините, доступно только {available_qty_in_db} шт. товара '{product_name}'. Пожалуйста, введите количество не более {available_qty_in_db}.")
            return

        cart = await load_user_cart_from_db(user_id)
        user_cart[user_id] = cart

        found = False
        for idx, (pid, qty) in enumerate(cart):
            if pid == product_id:
                if new_quantity == 0:
                    cart.pop(idx)
                    await message.answer(f"Товар '{product_name}' удалён из корзины.")
                else:
                    cart[idx] = (pid, new_quantity)
                    await message.answer(f"Количество товара '{product_name}' обновлено до {new_quantity} шт.")
                found = True
                break
        
        if not found:
            await message.answer("Произошла ошибка: товар не найден в вашей корзине.")

        await save_user_cart_to_db(user_id, user_cart[user_id])
        await state.clear()
        await view_cart(message)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное количество (целое неотрицательное число).")

@router.message(F.text == "✅ Оформить заказ")
async def start_order_process(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    cart = await load_user_cart_from_db(user_id)
    user_cart[user_id] = cart

    if not cart:
        await message.answer("Корзина пуста. Добавьте товары, прежде чем оформлять заказ.")
        await state.clear()
        return
    await state.set_state(UserStates.awaiting_delivery_name)
    await message.answer("Введите ваши фамилию и имя:")

@router.message(UserStates.awaiting_delivery_name)
async def process_delivery_name(message: types.Message, state: FSMContext):
    await state.update_data(delivery_name=message.text)
    await state.set_state(UserStates.awaiting_delivery_address)
    await message.answer("Введите ваш город, улицу, дом, квартиру (например: Москва, ул. Ленина, д. 10, кв. 5):")

@router.message(UserStates.awaiting_delivery_address)
async def process_delivery_address(message: types.Message, state: FSMContext):
    await state.update_data(delivery_address=message.text)
    await state.set_state(UserStates.awaiting_delivery_phone)
    await message.answer("Введите ваш номер телефона (например: +79123456789):")

@router.message(UserStates.awaiting_delivery_phone)
async def process_delivery_phone(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    phone_number = message.text
    await state.update_data(delivery_phone=phone_number)
    
    cart = await load_user_cart_from_db(user_id)
    user_cart[user_id] = cart

    data = await state.get_data()
    delivery_name = data.get('delivery_name')
    delivery_address = data.get('delivery_address')
    
    # Формируем сводку заказа
    full_delivery_info = f"**Данные доставки**:\nИмя: {delivery_name}\nАдрес: {delivery_address}\nТелефон: {phone_number}\n\n"
    order_summary = f"**Ваш заказ**:\n"
    total_price = 0
    async with aiosqlite.connect("products.db") as db:
        for pid, qty in cart:
            cursor = await db.execute("SELECT name, price FROM products WHERE id = ?", (pid,))
            result = await cursor.fetchone()
            if result:
                name, price = result
                item_total = price * qty / 100
                order_summary += f"- {name} ({qty} шт) — {item_total:.2f}₽\n"
                total_price += item_total
            else:
                order_summary += f"- Товар ID {pid} (удалён из каталога) — {qty} шт\n"
    
    order_summary += f"\n**Итого**: {total_price:.2f}₽"
    full_summary = full_delivery_info + order_summary

    # Проверяем наличие товаров
    async with aiosqlite.connect("products.db") as db:
        for pid, qty in cart:
            cursor = await db.execute("SELECT quantity FROM products WHERE id = ?", (pid,))
            available_qty = (await cursor.fetchone())[0]
            if qty > available_qty:
                await message.answer(f"Извините, товара с ID {pid} нет в достаточном количестве. Доступно: {available_qty} шт.")
                await state.clear()
                await start(message)
                return

    # Отправляем сводку с кнопками подтверждения
    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_order")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")]
    ]
    await state.set_state(UserStates.awaiting_order_confirmation)
    await message.answer(full_summary, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@router.callback_query(F.data == "confirm_order", UserStates.awaiting_order_confirmation)
async def confirm_order(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    cart = await load_user_cart_from_db(user_id)
    user_cart[user_id] = cart

    data = await state.get_data()
    delivery_name = data.get('delivery_name')
    delivery_address = data.get('delivery_address')
    phone_number = data.get('delivery_phone')
    full_delivery_info = f"Имя: {delivery_name}\nАдрес: {delivery_address}\nТелефон: {phone_number}"

    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("INSERT INTO orders (user_id, delivery_info) VALUES (?, ?)", (user_id, full_delivery_info))
        await db.commit()
        order_id = cursor.lastrowid
        
        for pid, qty in cart:
            await db.execute("INSERT INTO order_items (order_id, product_id, quantity) VALUES (?, ?, ?)", (order_id, pid, qty))
            await db.execute("UPDATE products SET quantity = quantity - ? WHERE id = ?", (qty, pid))
        await db.commit()

    await save_user_cart_to_db(user_id, [])
    user_cart[user_id] = []
    await state.clear()

    await callback.message.edit_text(f"Ваш заказ #{order_id} оформлен!", reply_markup=None)
    await start(callback.message)

    order_details_for_admin = f"Новый заказ #{order_id} от пользователя {callback.from_user.full_name} (ID: {user_id})\n"
    order_details_for_admin += f"Данные доставки:\n{full_delivery_info}\n\nСостав заказа:\n"
    async with aiosqlite.connect("products.db") as db:
        for pid, qty in cart:
            cursor = await db.execute("SELECT name, price FROM products WHERE id = ?", (pid,))
            result = await cursor.fetchone()
            if result:
                name, price = result
                order_details_for_admin += f"- {name} ({qty} шт) - {price * qty / 100:.2f}₽\n"
            else:
                order_details_for_admin += f"- Товар ID {pid} (удалён из каталога) — {qty} шт\n"

    await notify_admin(order_details_for_admin)
    await send_email("Новый заказ", order_details_for_admin)
    await callback.answer()

@router.callback_query(F.data == "cancel_order", UserStates.awaiting_order_confirmation)
async def cancel_order(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Оформление заказа отменено.", reply_markup=None)
    await start(callback.message)
    await callback.answer()

@router.message(F.text == "📦 Мои заказы")
async def my_orders(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT id, status FROM orders WHERE user_id = ?", (user_id,))
        orders = await cursor.fetchall()
    if not orders:
        await message.answer("У вас пока нет заказов.")
        return
    reply = "Ваши заказы:\n" + "\n".join([f"#{oid} — {status}" for oid, status in orders])
    await message.answer(reply)

# --- Админские функции ---
@router.message(F.text == "📥 Заказы (админ)", F.from_user.id == YOUR_ADMIN_ID)
async def admin_orders(message: types.Message):
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT id, user_id, status FROM orders")
        orders = await cursor.fetchall()
    
    if not orders:
        await message.answer("Пока нет заказов.")
        return

    buttons = [[InlineKeyboardButton(text=f"Заказ #{oid} от {uid} — {status}", callback_data=f"status_{oid}")]
               for oid, uid, status in orders]
    await message.answer("Список заказов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("status_"), F.from_user.id == YOUR_ADMIN_ID)
async def status_update(callback: types.CallbackQuery):
    order_id = int(callback.data.split('_')[1])
    statuses = ["Принят", "Отправлен", "Завершен"]
    buttons = [[InlineKeyboardButton(text=s, callback_data=f"set_{order_id}_{s}")] for s in statuses]
    await callback.message.answer("Выберите новый статус:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@router.callback_query(F.data.startswith("set_"), F.from_user.id == YOUR_ADMIN_ID)
async def set_status(callback: types.CallbackQuery):
    _, order_id, new_status = callback.data.split("_", 2)
    order_id = int(order_id)
    async with aiosqlite.connect("products.db") as db:
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
        cursor = await db.execute("SELECT user_id FROM orders WHERE id = ?", (order_id,))
        user = await cursor.fetchone()
        await db.commit()
    
    if user:
        try:
            await bot.send_message(user[0], f"Статус вашего заказа #{order_id} изменён на: {new_status}")
        except Exception as e:
            await notify_admin(f"Не удалось уведомить пользователя {user[0]} о смене статуса заказа #{order_id}: {e}")
    await callback.answer(f"Статус заказа #{order_id} обновлён на {new_status}")
    await admin_orders(callback.message)

@router.message(F.text == "📤 Экспорт заказов", F.from_user.id == YOUR_ADMIN_ID)
async def export_orders(message: types.Message):
    async with aiosqlite.connect("products.db") as db:
        wb = Workbook()
        ws_orders = wb.active
        ws_orders.title = "Заказы"
        ws_orders.append(["ID Заказа", "ID Пользователя", "Информация о доставке", "Статус", "Общая стоимость (₽)"])
        ws_order_details = wb.create_sheet("Детали Заказов")
        ws_order_details.append(["ID Заказа", "Название товара", "Количество", "Цена за ед. (₽)", "Сумма (₽)"])
        ws_products = wb.create_sheet("Каталог Товаров")
        ws_products.append(["ID Товара", "Название", "Цена (₽)", "Описание", "Количество"])
        cursor_orders = await db.execute("SELECT id, user_id, delivery_info, status FROM orders")
        orders_data = await cursor_orders.fetchall()
        all_order_items_data = []
        for order_id, user_id, delivery_info, status in orders_data:
            order_total = 0
            cursor_items = await db.execute("SELECT product_id, quantity FROM order_items WHERE order_id = ?", (order_id,))
            order_items = await cursor_items.fetchall()
            for product_id, item_qty in order_items:
                cursor_product = await db.execute("SELECT name, price FROM products WHERE id = ?", (product_id,))
                product_info = await cursor_product.fetchone()
                product_name = f"Товар ID {product_id} (удалён)" if not product_info else product_info[0]
                product_price = 0 if not product_info else product_info[1] / 100
                item_total_cost = product_price * item_qty
                order_total += item_total_cost
                all_order_items_data.append([order_id, product_name, item_qty, product_price, item_total_cost])
            ws_orders.append([order_id, user_id, delivery_info, status, order_total])
        for row in all_order_items_data:
            ws_order_details.append(row)
        cursor_products_catalog = await db.execute("SELECT id, name, price, description, quantity FROM products")
        for row in await cursor_products_catalog.fetchall():
            ws_products.append([row[0], row[1], row[2] / 100, row[3], row[4]])
        path = "orders_export.xlsx"
        wb.save(path)
    await bot.send_document(message.chat.id, types.FSInputFile(path), caption="Экспорт заказов, их деталей и каталога товаров")
    os.remove(path)

# --- Админские функции: Добавление товара ---
@router.message(F.text == "➕ Добавить товар", F.from_user.id == YOUR_ADMIN_ID)
async def add_product_start(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.add_product_name)
    await message.answer("Введите название товара:")

@router.message(AdminStates.add_product_name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AdminStates.add_product_price)
    await message.answer("Введите цену товара в рублях (например, 123.45):")

@router.message(AdminStates.add_product_price)
async def add_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=int(price * 100))
        await state.set_state(AdminStates.add_product_description)
        await message.answer("Введите описание товара:")
    except ValueError:
        await message.answer("Пожалуйста, введите корректную цену (число, например 123.45).")

@router.message(AdminStates.add_product_description)
async def add_product_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(AdminStates.add_product_quantity)
    await message.answer("Введите начальное количество товара:")

@router.message(AdminStates.add_product_quantity)
async def add_product_quantity(message: types.Message, state: FSMContext):
    try:
        quantity = int(message.text)
        if quantity < 0:
            raise ValueError("Количество не может быть отрицательным.")
        product_data = await state.get_data()
        name = product_data['name']
        price = product_data['price']
        description = product_data['description']
        async with aiosqlite.connect("products.db") as db:
            await db.execute(
                "INSERT INTO products (name, price, description, quantity) VALUES (?, ?, ?, ?)",
                (name, price, description, quantity)
            )
            await db.commit()
        await message.answer(f"Товар '{name}' успешно добавлен в каталог!")
        await state.clear()
    except ValueError:
        await message.answer("Пожалуйста, введите корректное количество (целое неотрицательное число).")

@router.message(F.text == "🔄 Обновить количество", F.from_user.id == YOUR_ADMIN_ID)
async def update_quantity_start(message: types.Message, state: FSMContext):
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT id, name, quantity FROM products")
        products = await cursor.fetchall()
    
    if not products:
        await message.answer("Каталог товаров пуст. Нечего обновлять.")
        await state.clear()
        return

    buttons = [[InlineKeyboardButton(text=f"{name} (текущее: {qty} шт)", callback_data=f"select_qty_{id}")]
               for id, name, qty in products]
    
    await state.set_state(AdminStates.update_quantity_select_product)
    await message.answer("Выберите товар, количество которого хотите обновить:", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("select_qty_"), AdminStates.update_quantity_select_product, F.from_user.id == YOUR_ADMIN_ID)
async def update_quantity_select_product_callback(callback: types.CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[2])
    await state.update_data(product_id_to_update=product_id)
    await state.set_state(AdminStates.update_quantity_enter_new_qty)
    await callback.message.answer("Введите новое количество для этого товара:")
    await callback.answer()

@router.message(AdminStates.update_quantity_enter_new_qty)
async def update_quantity_enter_new_qty(message: types.Message, state: FSMContext):
    try:
        new_quantity = int(message.text)
        if new_quantity < 0:
            raise ValueError("Количество не может быть отрицательным.")
        data = await state.get_data()
        product_id = data.get('product_id_to_update')
        if not product_id:
            await message.answer("Произошла ошибка. Пожалуйста, попробуйте снова, начиная с кнопки 'Обновить количество'.")
            await state.clear()
            return
        async with aiosqlite.connect("products.db") as db:
            await db.execute("UPDATE products SET quantity = ? WHERE id = ?", (new_quantity, product_id))
            await db.commit()
            cursor = await db.execute("SELECT name FROM products WHERE id = ?", (product_id,))
            product_name = (await cursor.fetchone())[0]
        await message.answer(f"Количество товара '{product_name}' успешно обновлено до {new_quantity} шт!")
        await state.clear()
    except ValueError:
        await message.answer("Пожалуйста, введите корректное количество (целое неотрицательное число).")

@router.message(F.text == "✏️ Редактировать товар", F.from_user.id == YOUR_ADMIN_ID)
async def edit_product_start(message: types.Message, state: FSMContext):
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT id, name FROM products")
        products = await cursor.fetchall()
    
    if not products:
        await message.answer("Каталог товаров пуст. Нечего редактировать.")
        await state.clear()
        return

    buttons = [[InlineKeyboardButton(text=name, callback_data=f"edit_product_id_{id}")]
               for id, name in products]
    
    await state.set_state(AdminStates.edit_product_select)
    await message.answer("Выберите товар для редактирования:", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("edit_product_id_"), AdminStates.edit_product_select, F.from_user.id == YOUR_ADMIN_ID)
async def edit_product_selected(callback: types.CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[3])
    await state.update_data(product_id_to_edit=product_id)
    
    async with aiosqlite.connect("products.db") as db:
        cursor = await db.execute("SELECT name, price, description FROM products WHERE id = ?", (product_id,))
        name, price, description = await cursor.fetchone()

    await state.update_data(
        current_name=name,
        current_price=price,
        current_description=description
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить название", callback_data="edit_name")],
        [InlineKeyboardButton(text="Изменить цену", callback_data="edit_price")],
        [InlineKeyboardButton(text="Изменить описание", callback_data="edit_description")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_edit")]
    ])
    await state.set_state(AdminStates.edit_product_menu)
    await callback.message.edit_text(
        f"Вы выбрали товар: **{name}**\n"
        f"Текущая цена: {price / 100:.2f}₽\n"
        f"Текущее описание: {description}\n\n"
        "Что вы хотите изменить?",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    await callback.answer()

@router.message(AdminStates.edit_product_menu)
async def handle_unexpected_text_in_edit_menu(message: types.Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить название", callback_data="edit_name")],
        [InlineKeyboardButton(text="Изменить цену", callback_data="edit_price")],
        [InlineKeyboardButton(text="Изменить описание", callback_data="edit_description")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_edit")]
    ])
    await message.answer(
        "Пожалуйста, выберите действие, используя кнопки ниже.\n\n"
        "Что вы хотите изменить?",
        reply_markup=markup
    )

@router.callback_query(F.data == "edit_name", AdminStates.edit_product_menu, F.from_user.id == YOUR_ADMIN_ID)
async def edit_product_name_prompt(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current_name = data.get('current_name', 'Неизвестно')
    await state.set_state(AdminStates.edit_product_name_input)
    await callback.message.answer(f"Введите новое название для товара (текущее: **{current_name}**):", parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.edit_product_name_input)
async def edit_product_name_process(message: types.Message, state: FSMContext):
    new_name = message.text
    data = await state.get_data()
    product_id = data.get('product_id_to_edit')
    if not product_id:
        await message.answer("Произошла ошибка. Пожалуйста, попробуйте снова, начиная с кнопки 'Редактировать товар'.")
        await state.clear()
        return
    async with aiosqlite.connect("products.db") as db:
        await db.execute("UPDATE products SET name = ? WHERE id = ?", (new_name, product_id))
        await db.commit()
    await message.answer(f"Название товара успешно обновлено на: **{new_name}**", parse_mode="Markdown")
    await state.clear()

@router.callback_query(F.data == "edit_price", AdminStates.edit_product_menu, F.from_user.id == YOUR_ADMIN_ID)
async def edit_product_price_prompt(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current_price = data.get('current_price', 0) / 100
    await state.set_state(AdminStates.edit_product_price_input)
    await callback.message.answer(f"Введите новую цену для товара в рублях (текущая: **{current_price:.2f}₽**):", parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.edit_product_price_input)
async def edit_product_price_process(message: types.Message, state: FSMContext):
    try:
        new_price_float = float(message.text)
        new_price_int = int(new_price_float * 100)
        if new_price_int < 0:
            raise ValueError("Цена не может быть отрицательной.")
        data = await state.get_data()
        product_id = data.get('product_id_to_edit')
        if not product_id:
            await message.answer("Произошла ошибка. Пожалуйста, попробуйте снова, начиная с кнопки 'Редактировать товар'.")
            await state.clear()
            return
        async with aiosqlite.connect("products.db") as db:
            await db.execute("UPDATE products SET price = ? WHERE id = ?", (new_price_int, product_id))
            await db.commit()
        await message.answer(f"Цена товара успешно обновлена на: **{new_price_float:.2f}₽**", parse_mode="Markdown")
        await state.clear()
    except ValueError:
        await message.answer("Пожалуйста, введите корректную цену (число, например 123.45).")

@router.callback_query(F.data == "edit_description", AdminStates.edit_product_menu, F.from_user.id == YOUR_ADMIN_ID)
async def edit_product_description_prompt(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current_description = data.get('current_description', 'Нет описания')
    await state.set_state(AdminStates.edit_product_description_input)
    await callback.message.answer(f"Введите новое описание для товара (текущее: **{current_description}**):", parse_mode="Markdown")
    await callback.answer()

@router.message(AdminStates.edit_product_description_input)
async def edit_product_description_process(message: types.Message, state: FSMContext):
    new_description = message.text
    data = await state.get_data()
    product_id = data.get('product_id_to_edit')
    if not product_id:
        await message.answer("Произошла ошибка. Пожалуйста, попробуйте снова, начиная с кнопки 'Редактировать товар'.")
        await state.clear()
        return
    async with aiosqlite.connect("products.db") as db:
        await db.execute("UPDATE products SET description = ? WHERE id = ?", (new_description, product_id))
        await db.commit()
    await message.answer(f"Описание товара успешно обновлено на: **{new_description}**", parse_mode="Markdown")
    await state.clear()

@router.callback_query(F.data == "cancel_edit", AdminStates.edit_product_menu, F.from_user.id == YOUR_ADMIN_ID)
async def cancel_edit_product(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Редактирование товара отменено.")
    await callback.answer()

# --- Запуск бота ---
async def main():
    await init_db()
    await asyncio.gather(
        dp.start_polling(bot),
        clear_old_carts_task()
    )

if __name__ == '__main__':
    asyncio.run(main())