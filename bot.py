import asyncio
import os
from aiogram.types import FSInputFile
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InputMediaPhoto,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BufferedInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from data import CITIES, SPECIALISTS, PAYMENT_DETAILS
from config import BOT_TOKEN, ADMIN_ID
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class OrderStates(StatesGroup):
    choosing_city = State()
    choosing_specialist_type = State()
    browsing_specialists = State()
    waiting_address = State()
    waiting_payment_screenshot = State()

pending_payments: dict[str, dict] = {}  # key: "{user_id}_{specialist_id}"

def black_square_image(size=300) -> bytes:
    """Generate a black square as PNG bytes without Pillow."""
    import struct, zlib

    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        length = len(data)
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", length) + chunk_type + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    ihdr = png_chunk(b"IHDR", ihdr_data)

    raw_rows = b""
    row = b"\x00" + b"\x00\x00\x00" * size   # filter byte + RGB black pixels
    raw_rows = row * size
    compressed = zlib.compress(raw_rows)
    idat = png_chunk(b"IDAT", compressed)
    iend = png_chunk(b"IEND", b"")

    return signature + ihdr + idat + iend


def cities_keyboard():
    buttons = []
    row = []
    for i, city in enumerate(CITIES):
        row.append(InlineKeyboardButton(text=city, callback_data=f"city:{city}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def specialist_types_keyboard(city: str):
    types = list({s["type"] for s in SPECIALISTS})
    buttons = [[InlineKeyboardButton(text=t, callback_data=f"type:{t}")] for t in types]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back:cities")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def specialist_card_keyboard(idx: int, total: int, spec_id: str):
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"spec_nav:{idx-1}"))
    nav.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data="noop"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"spec_nav:{idx+1}"))

    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="✅ Выбрать модель", callback_data=f"hire:{spec_id}")],
        [InlineKeyboardButton(text="◀️ К категориям", callback_data="back:types")],
    ])


def admin_confirm_keyboard(payment_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"confirm:{payment_key}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{payment_key}"),
    ]])


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer_photo(
        photo = FSInputFile("Photos/welcome.png"),
        caption = (
        "👋 <b>Вас приветствует модельное агенство Iconic Agency!</b>\n\n"
        "Здесь вы можете заказать модель\n\n"
        "Выберите ваш город:"
        ),
        parse_mode="HTML",
        reply_markup=cities_keyboard()
    )
    await state.set_state(OrderStates.choosing_city)


@dp.callback_query(F.data.startswith("city:"))
async def city_chosen(call: CallbackQuery, state: FSMContext):
    city = call.data.split(":", 1)[1]
    await state.update_data(city=city)
    await call.message.edit_caption(
        caption=f"📍 Город: <b>{city}</b>\n\nВыберите вариант отдыха:\n\n"
        f"🔞<b>Интим</b> — секс и буря эмоций\n\n"
        f"☺️<b>Просто отдых</b> — приятное общение с веселой и общительной девушкой",
        parse_mode="HTML",
        reply_markup=specialist_types_keyboard(city)
    )
    await state.set_state(OrderStates.choosing_specialist_type)
    await call.answer()


@dp.callback_query(F.data.startswith("type:"))
async def type_chosen(call: CallbackQuery, state: FSMContext):
    spec_type = call.data.split(":", 1)[1]
    data = await state.get_data()
    city = data.get("city", "")

    filtered = [s for s in SPECIALISTS if s["type"] == spec_type]
    if not filtered:
        await call.answer("Нет моделей в этой категории.", show_alert=True)
        return

    await state.update_data(spec_type=spec_type, spec_list=[s["id"] for s in filtered], spec_idx=0)
    await show_specialist_card(call.message, filtered[0], 0, len(filtered), edit=True)
    await state.set_state(OrderStates.browsing_specialists)
    await call.answer()


async def show_specialist_card(message: Message, spec: dict, idx: int, total: int, edit=False):
    caption = (
        f"👤 <b>{spec['name']}</b>\n"
        f"🎂 Возраст: {spec['age']} лет\n"
        f"💰 Стоимость: <b>{spec['price']} ₽/час</b>\n\n"
    )
    kb = specialist_card_keyboard(idx, total, spec["id"])
    photo_path = spec.get("photo")
    if photo_path and os.path.exists(photo_path):
        photo = FSInputFile(photo_path)
    elif photo_path and photo_path.startswith("http"):
        photo = photo_path
    else:
        photo_bytes = black_square_image(300)
        photo = BufferedInputFile(photo_bytes, filename="photo.png")

    if edit:
        try:
            await message.edit_media(
                media=InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                reply_markup=kb
            )
        except Exception:
            await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer_photo(photo=photo, caption=caption, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("spec_nav:"))
async def spec_nav(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    spec_ids = data.get("spec_list", [])
    if idx < 0 or idx >= len(spec_ids):
        await call.answer()
        return
    spec = next((s for s in SPECIALISTS if s["id"] == spec_ids[idx]), None)
    if not spec:
        await call.answer()
        return
    await state.update_data(spec_idx=idx)
    await show_specialist_card(call.message, spec, idx, len(spec_ids), edit=True)
    await call.answer()


@dp.callback_query(F.data == "back:cities")
async def back_to_cities(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.message.answer_photo(
        photo=FSInputFile("Photos/welcome.png"),
        caption = "Выберите ваш город:",
        reply_markup=cities_keyboard()
    )
    await state.set_state(OrderStates.choosing_city)
    await call.answer()
@dp.callback_query(F.data == "back:types")
async def back_to_types(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city = data.get("city", "")
    await call.message.delete()
    await call.message.answer_photo(
        photo=FSInputFile("Photos/welcome.png"),
        caption= f"📍 Город: <b>{city}</b>\n\nВыберите вариант отдыха:\n\n"
        f"🔞<b>Интим</b> — секс и буря эмоций\n\n"
        f"☺️<b>Просто отдых</b> — приятное общение с веселой и общительной девушкой",
        parse_mode="HTML",
        reply_markup=specialist_types_keyboard(city)
    )
    await state.set_state(OrderStates.choosing_specialist_type)
    await call.answer()


@dp.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("hire:"))
async def hire_specialist(call: CallbackQuery, state: FSMContext):
    spec_id = call.data.split(":", 1)[1]
    spec = next((s for s in SPECIALISTS if s["id"] == spec_id), None)
    if not spec:
        await call.answer("Модель не найдена.", show_alert=True)
        return

    pd = PAYMENT_DETAILS
    text = (
        f"✅ Вы выбрали: <b>{spec['name']}</b>\n"
        f"💰 Стоимость: <b>{spec['price']} ₽/час</b>\n\n"
        f"<b>Презервативы входят в стоимость</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Реквизиты для оплаты:</b>\n\n"
        f"🏦 Банк: <b>{pd['bank']}</b>\n"
        f"💳 Карта: <code>{pd['card']}</code>\n"
        f"👤 Получатель: <b>{pd['name']}</b>\n"
        f"📱 По номеру: <code>{pd['phone']}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"После оплаты <b>отправьте скриншот</b> чека в этот чат 📸"
    )
    await call.message.answer(
    f"✅ Вы выбрали: <b>{spec['name']}</b>\n\n"
    f"📍 Напишите ваш адрес (улица, дом, квартира):",
    parse_mode="HTML"
)
    await state.update_data(selected_spec_id=spec_id)
    await state.set_state(OrderStates.waiting_address)
    await call.answer()


@dp.message(OrderStates.waiting_payment_screenshot, F.photo)
async def receive_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    spec_id = data.get("selected_spec_id")
    city = data.get("city", "Не указан")
    spec = next((s for s in SPECIALISTS if s["id"] == spec_id), None)

    user = message.from_user
    payment_key = f"{user.id}_{spec_id}"
    pending_payments[payment_key] = {
        "user_id": user.id,
        "spec_id": spec_id,
        "city": city,
    }

    await message.answer(
        "📨 Чек отправлен администратору на проверку.\n"
        "Ожидайте подтверждения оплаты ⏳"
    )

    spec_name = spec["name"] if spec else spec_id
    caption = (
        f"💰 <b>Новый платёж!</b>\n\n"
        f"👤 Клиент: {user.full_name} (@{user.username or '—'})\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"📍 Город: {city}\n"
        f"🔧 Модель: <b>{spec_name}</b>\n"
        f"💵 Сумма: {spec['price']} ₽/час" if spec else f"🔧 Мастер ID: {spec_id}"
    )
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=admin_confirm_keyboard(payment_key)
    )

    await state.clear()


@dp.message(OrderStates.waiting_payment_screenshot)
async def wrong_format(message: Message):
    await message.answer("📸 Пожалуйста, отправьте <b>скриншот</b> (фото) чека об оплате.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("confirm:"))
async def admin_confirm(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return

    payment_key = call.data.split(":", 1)[1]
    payment = pending_payments.pop(payment_key, None)
    if not payment:
        await call.answer("Платёж не найден или уже обработан.", show_alert=True)
        return

    spec = next((s for s in SPECIALISTS if s["id"] == payment["spec_id"]), None)
    spec_name = spec["name"] if spec else payment["spec_id"]

    await bot.send_message(
        chat_id=payment["user_id"],
        text=(
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"Модель <b>{spec_name}</b> свяжется с вами в ближайшее время.\n"
            f"Спасибо за заказ! 🎉"
        ),
        parse_mode="HTML"
    )

    await call.message.edit_caption(
        caption=call.message.caption + "\n\n✅ <b>ПОДТВЕРЖДЕНО</b>",
        parse_mode="HTML"
    )
    await call.answer("Оплата подтверждена!")


@dp.callback_query(F.data.startswith("reject:"))
async def admin_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return

    payment_key = call.data.split(":", 1)[1]
    payment = pending_payments.pop(payment_key, None)
    if not payment:
        await call.answer("Платёж не найден или уже обработан.", show_alert=True)
        return

    await bot.send_message(
        chat_id=payment["user_id"],
        text=(
            "❌ <b>Оплата не подтверждена.</b>\n\n"
            "Возможно, платёж не прошёл или чек нечитаем.\n"
            "Пожалуйста, свяжитесь с поддержкой или попробуйте снова /start"
        ),
        parse_mode="HTML"
    )

    await call.message.edit_caption(
        caption=call.message.caption + "\n\n❌ <b>ОТКЛОНЕНО</b>",
        parse_mode="HTML"
    )
    await call.answer("Платёж отклонён.")


async def main():
    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="start", description="Заказать модель÷")
    ])
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
