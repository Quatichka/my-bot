import hashlib
import html
import io
import json
import logging
import os
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("nailbot")

# ───────────────────────── НАСТРОЙКИ САЛОНА ─────────────────────────
# Меняйте здесь названия, цены, адрес и рабочее время.

SALON = "Nail Studio"
ADDRESS = "ул. Примерная, 1"
CONTACT_PHONE = "+7 900 000-00-00"
WORK_HOURS_TEXT = "Пн–Сб, 10:00–20:00"

# ключ: (название, цена в ₽, длительность, описание)
SERVICES = {
    "classic": ("Классический маникюр", 1500, "60 мин", "Обработка кутикулы, форма, уход."),
    "gel": ("Маникюр + гель-лак", 2200, "90 мин", "Стойкое покрытие до 3 недель."),
    "design": ("Дизайн ногтей", 500, "+30 мин", "Френч, точки, линии, минимализм."),
    "extension": ("Наращивание", 3500, "150 мин", "Гель, любая длина и форма."),
    "pedicure": ("Педикюр", 2500, "90 мин", "Аппаратный педикюр с покрытием."),
}

WORK_DAYS = {0, 1, 2, 3, 4, 5}  # 0 = понедельник … 6 = воскресенье
SLOTS = ["10:00", "12:00", "14:00", "16:00", "18:00"]
DAYS_AHEAD = 7            # сколько рабочих дней показывать для записи
MIN_LEAD = timedelta(hours=1)        # за сколько минимум до начала можно записаться
RESEND_COOLDOWN = timedelta(minutes=30)  # как часто клиент может напомнить мастеру

# ───────────────────────── ОКРУЖЕНИЕ ─────────────────────────

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))
ADMIN_ID = int(os.environ["ADMIN_ID"]) if os.environ.get("ADMIN_ID") else None
DATA_FILE = os.environ.get("DATA_FILE", "bookings.json")

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
ACTIVE = ("pending", "confirmed")
STATUS = {
    "pending": "⏳ ждёт подтверждения",
    "confirmed": "✅ подтверждена",
    "declined": "❌ отклонена",
    "cancelled": "🚫 отменена",
}

# ───────────────────────── ХРАНИЛИЩЕ ─────────────────────────


def load_db() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"next_id": 1, "bookings": {}}


DB = load_db()
DB.setdefault("closed_days", [])   # дни, закрытые мастером целиком
DB.setdefault("closed_slots", {})  # {"2026-10-01": ["10:00", ...]} — окна, закрытые мастером


def save_db() -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


def now() -> datetime:
    return datetime.now(TZ)


def booking_at(day: str, slot: str) -> dict | None:
    return next(
        (b for b in DB["bookings"].values() if b["date"] == day and b["time"] == slot and b["status"] in ACTIVE),
        None,
    )


def is_closed(day: str, slot: str) -> bool:
    return day in DB["closed_days"] or slot in DB["closed_slots"].get(day, [])


def is_busy(day: str, slot: str) -> bool:
    return is_closed(day, slot) or booking_at(day, slot) is not None


def is_past(day: date, slot: str) -> bool:
    h, m = map(int, slot.split(":"))
    return datetime.combine(day, time(h, m), TZ) < now() + MIN_LEAD


def free_slots(day: date) -> list[str]:
    return [s for s in SLOTS if not is_past(day, s) and not is_busy(day.isoformat(), s)]


def open_days(count: int = DAYS_AHEAD) -> list[date]:
    today = now().date()
    days = []
    for i in range(count * 2):
        day = today + timedelta(days=i)
        if day.weekday() in WORK_DAYS:
            days.append(day)
        if len(days) == count:
            break
    return days


def fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m}"


def booking_text(b: dict) -> str:
    name, price, _, _ = SERVICES[b["service"]]
    return f"{name} · {price} ₽\n{fmt_day(b['date'])} в {b['time']}"


# ───────────────────────── МИНИМАЛИСТИЧНЫЕ КАРТИНКИ ─────────────────────────
# Рисуются прямо в коде, поэтому не нужно загружать файлы картинок.

PALETTES = {
    "welcome": ("#F6ECE8", "#D39C97"),
    "classic": ("#F5EFEA", "#E3BCAE"),
    "gel": ("#F4E9EE", "#C0697D"),
    "design": ("#EDEFF3", "#7F8FAE"),
    "extension": ("#F5EFE6", "#C49A6C"),
    "pedicure": ("#ECF2ED", "#8FB098"),
    "calendar": ("#F3F0EB", "#B5A393"),
    "done": ("#ECF3EE", "#86B292"),
    "info": ("#F1EEF3", "#A393B5"),
}


def _mix(color, t: float) -> tuple:
    """Смешивает цвет ("#RRGGBB" или (r, g, b)) с белым: t=0 — исходный, t=1 — белый."""
    if isinstance(color, str):
        color = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b = color
    return tuple(round(c + (255 - c) * t) for c in (r, g, b))


def _nail(d: ImageDraw.ImageDraw, cx: int, bottom: int, w: int, h: int, color: str, dots: bool) -> None:
    d.rounded_rectangle((cx - w // 2, bottom - h, cx + w // 2, bottom), radius=w // 2, fill=color)
    # блик
    hw = w // 6
    d.rounded_rectangle(
        (cx - w // 4 - hw // 2, bottom - h + w // 3, cx - w // 4 + hw // 2, bottom - h + w // 3 + h // 3),
        radius=hw // 2, fill=_mix(color, 0.55),
    )
    if dots:
        r = w // 10
        for k in range(3):
            y = bottom - h // 3 - k * (r * 3)
            d.ellipse((cx + w // 8 - r, y - r, cx + w // 8 + r, y + r), fill=_mix(color, 0.85))


@lru_cache(maxsize=None)
def render(key: str) -> bytes:
    bg, accent = PALETTES[key]
    s = 2  # рисуем в 2× и уменьшаем — так края получаются гладкими
    W, H = 800 * s, 450 * s
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # мягкий круг на фоне
    R = 190 * s
    d.ellipse((W // 2 - R, H // 2 - R, W // 2 + R, H // 2 + R), fill=_mix(accent, 0.75))

    if key == "calendar":
        cell, gap = 46 * s, 14 * s
        x0 = W // 2 - (7 * cell + 6 * gap) // 2
        y0 = H // 2 - (4 * cell + 3 * gap) // 2
        for row in range(4):
            for col in range(7):
                x, y = x0 + col * (cell + gap), y0 + row * (cell + gap)
                fill = accent if (row, col) == (1, 4) else _mix(accent, 0.45 if col < 6 else 0.9)
                d.rounded_rectangle((x, y, x + cell, y + cell), radius=12 * s, fill=fill)
    elif key == "done":
        r = 110 * s
        d.ellipse((W // 2 - r, H // 2 - r, W // 2 + r, H // 2 + r), fill=accent)
        d.line(
            [(W // 2 - 48 * s, H // 2 + 2 * s), (W // 2 - 12 * s, H // 2 + 38 * s), (W // 2 + 52 * s, H // 2 - 36 * s)],
            fill="white", width=18 * s, joint="curve",
        )
    elif key == "info":
        r = 26 * s
        for i in range(3):
            cx = W // 2 + (i - 1) * 90 * s
            d.ellipse((cx - r, H // 2 - r, cx + r, H // 2 + r), fill=_mix(accent, 0.4 * (2 - i) / 2))
    else:
        if key == "pedicure":
            fingers = [(-2, 0.95, 1.25), (-1, 0.75, 0.9), (0, 0.72, 0.85), (1, 0.68, 0.78), (2, 0.62, 0.7)]
            base_w, base_h, step = 58 * s, 70 * s, 78 * s
        else:
            tall = 1.35 if key == "extension" else 1.0
            fingers = [(-1.5, 0.85, 0.9 * tall), (-0.5, 1.0, 1.05 * tall), (0.5, 0.97, 1.0 * tall), (1.5, 0.8, 0.82 * tall)]
            base_w, base_h, step = 70 * s, 130 * s, 100 * s
        for offset, wk, hk in fingers:
            cx = W // 2 + int(offset * step)
            bottom = H // 2 + 110 * s - int(abs(offset) * 14 * s)
            color = accent if key != "classic" else _mix(accent, 0.1)
            _nail(d, cx, bottom, int(base_w * wk), int(base_h * hk), color, dots=(key == "design"))

    img = img.resize((W // s, H // s), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


PHOTO_IDS: dict[str, str] = {}  # после первой отправки Telegram отдаёт file_id — дальше шлём его


def photo(key: str):
    return PHOTO_IDS.get(key) or render(key)


def remember(key: str, msg) -> None:
    if msg is not None and getattr(msg, "photo", None):
        PHOTO_IDS[key] = msg.photo[-1].file_id


# ───────────────────────── ЭКРАНЫ ─────────────────────────

Btn = InlineKeyboardButton
BACK_MENU = [Btn("⌂ Главное меню", callback_data="menu")]


async def show(update: Update, key: str, caption: str, rows: list) -> None:
    """Показывает экран: картинка + текст + кнопки. Редактирует текущее сообщение, если можно."""
    markup = InlineKeyboardMarkup(rows)
    q = update.callback_query
    if q and q.message and q.message.photo:
        try:
            media = InputMediaPhoto(photo(key), caption=caption, parse_mode="HTML")
            remember(key, await q.edit_message_media(media, reply_markup=markup))
            return
        except BadRequest as e:
            if "not modified" in str(e):
                return
            log.warning("Не удалось отредактировать сообщение: %s", e)
    msg = await update.effective_chat.send_photo(photo(key), caption=caption, parse_mode="HTML", reply_markup=markup)
    remember(key, msg)


async def screen_menu(update: Update) -> None:
    caption = (
        f"<b>{SALON}</b>\n\n"
        "Привет! Чем могу помочь?\n"
        "Здесь можно посмотреть услуги и записаться на удобное время."
    )
    rows = [
        [Btn("🗓 Записаться", callback_data="book")],
        [Btn("💅 Услуги и цены", callback_data="services")],
        [Btn("📋 Мои записи", callback_data="my"), Btn("📍 Контакты", callback_data="contacts")],
    ]
    if ADMIN_ID is not None and update.effective_user.id == ADMIN_ID:
        rows.append([Btn("⚙️ Админ-панель", callback_data="adm")])
    await show(update, "welcome", caption, rows)


async def screen_services(update: Update, booking: bool) -> None:
    lines = [f"{name} — <b>{price} ₽</b>" for name, price, _, _ in SERVICES.values()]
    title = "Выберите услугу:" if booking else "<b>Услуги и цены</b>"
    prefix = "date" if booking else "svc"
    rows = [[Btn(name, callback_data=f"{prefix}:{key}")] for key, (name, *_ ) in SERVICES.items()]
    rows.append(BACK_MENU)
    await show(update, "welcome", title + "\n\n" + "\n".join(lines), rows)


async def screen_service(update: Update, key: str) -> None:
    name, price, duration, about = SERVICES[key]
    caption = f"<b>{name}</b>\n\n{about}\n\n{price} ₽ · {duration}"
    rows = [
        [Btn("🗓 Записаться", callback_data=f"date:{key}")],
        [Btn("← Все услуги", callback_data="services")],
    ]
    await show(update, key, caption, rows)


async def screen_dates(update: Update, key: str) -> None:
    buttons = []
    for day in open_days():
        count = len(free_slots(day))
        if count:
            buttons.append(Btn(f"{fmt_day(day.isoformat())} · {count}", callback_data=f"time:{key}:{day.isoformat()}"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn("← Услуги", callback_data="book")])
    name = SERVICES[key][0]
    if buttons:
        caption = f"<b>{name}</b>\n\nВыберите удобный день.\nЦифра — количество свободных окон."
    else:
        caption = f"<b>{name}</b>\n\nНа ближайшие дни свободных окон нет. Напишите нам: {CONTACT_PHONE}"
    await show(update, "calendar", caption, rows)


async def screen_times(update: Update, key: str, day: str) -> None:
    slots = free_slots(date.fromisoformat(day))
    buttons = [Btn(t, callback_data=f"slot:{key}:{day}:{t}") for t in slots]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([Btn("← Другой день", callback_data=f"date:{key}")])
    caption = f"<b>{SERVICES[key][0]}</b>\n{fmt_day(day)}\n\n"
    caption += "Выберите удобное время:" if slots else "На этот день всё занято — выберите другой."
    await show(update, "calendar", caption, rows)


async def screen_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data["draft"]
    caption = (
        "<b>Проверьте заявку</b>\n\n"
        f"{booking_text(draft)}\n"
        f"Телефон: {html.escape(context.user_data['phone'])}"
    )
    rows = [
        [Btn("✅ Отправить заявку", callback_data="confirm")],
        [Btn("🕐 Другое время", callback_data=f"date:{draft['service']}"), Btn("📱 Другой номер", callback_data="phone")],
        BACK_MENU,
    ]
    await show(update, draft["service"], caption, rows)


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_phone"] = True
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await update.effective_chat.send_message(
        "Оставьте номер телефона, чтобы мастер мог связаться с вами.\n"
        "Нажмите кнопку ниже или напишите номер сообщением.",
        reply_markup=keyboard,
    )


async def screen_my(update: Update) -> None:
    uid = update.effective_user.id
    today = now().date().isoformat()
    items = sorted(
        (b for b in DB["bookings"].values() if b["user_id"] == uid and b["status"] in ACTIVE and b["date"] >= today),
        key=lambda b: (b["date"], b["time"]),
    )
    rows = []
    if items:
        parts = []
        for b in items:
            parts.append(f"<b>#{b['id']}</b> {booking_text(b)}\n{STATUS[b['status']]}")
            buttons = []
            if b["status"] == "pending":
                buttons.append(Btn(f"🔁 Напомнить #{b['id']}", callback_data=f"resend:{b['id']}"))
            buttons.append(Btn(f"Отменить #{b['id']}", callback_data=f"cancel:{b['id']}"))
            rows.append(buttons)
        caption = "<b>Мои записи</b>\n\n" + "\n\n".join(parts)
    else:
        caption = "<b>Мои записи</b>\n\nУ вас пока нет записей."
        rows.append([Btn("🗓 Записаться", callback_data="book")])
    rows.append(BACK_MENU)
    await show(update, "info", caption, rows)


async def screen_contacts(update: Update) -> None:
    caption = (
        f"<b>{SALON}</b>\n\n"
        f"📍 {ADDRESS}\n"
        f"📞 {CONTACT_PHONE}\n"
        f"🕐 {WORK_HOURS_TEXT}"
    )
    await show(update, "info", caption, [BACK_MENU])


# ───────────────────────── ЗАЯВКИ МАСТЕРУ ─────────────────────────


def admin_card(b: dict, resent: bool) -> tuple[str, InlineKeyboardMarkup]:
    who = html.escape(b["name"])
    if b.get("username"):
        who += f" (@{b['username']})"
    head = "🔁 <b>Повторно: заявка" if resent else "🆕 <b>Новая заявка"
    text = (
        f"{head} #{b['id']}</b>\n\n"
        f"{booking_text(b)}\n\n"
        f"👤 {who}\n"
        f"📱 {html.escape(b['phone'])}\n"
        f'<a href="tg://user?id={b["user_id"]}">Написать клиенту</a>'
    )
    markup = InlineKeyboardMarkup([[
        Btn("✅ Подтвердить", callback_data=f"adm_ok:{b['id']}"),
        Btn("❌ Отклонить", callback_data=f"adm_no:{b['id']}"),
    ]])
    return text, markup


async def send_to_admin(context: ContextTypes.DEFAULT_TYPE, b: dict, resent: bool = False) -> bool:
    if not ADMIN_ID:
        log.warning("ADMIN_ID не задан — заявка #%s никуда не отправлена", b["id"])
        return False
    text, markup = admin_card(b, resent)
    try:
        await context.bot.send_message(ADMIN_ID, text, parse_mode="HTML", reply_markup=markup)
    except TelegramError as e:
        log.error("Не удалось отправить заявку мастеру: %s", e)
        return False
    b["sent_at"] = now().isoformat()
    save_db()
    return True


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if ADMIN_ID:
        try:
            await context.bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        except TelegramError as e:
            log.error("Не удалось написать мастеру: %s", e)


async def create_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    draft = context.user_data.get("draft")
    if not draft or "phone" not in context.user_data:
        return "Заявка устарела, начните заново."
    if is_busy(draft["date"], draft["time"]):
        return "Это время уже заняли. Выберите другое."

    user = update.effective_user
    bid = DB["next_id"]
    DB["next_id"] += 1
    booking = {
        "id": bid,
        "user_id": user.id,
        "name": user.full_name,
        "username": user.username,
        "phone": context.user_data["phone"],
        **draft,
        "status": "pending",
        "created": now().isoformat(),
    }
    DB["bookings"][str(bid)] = booking
    save_db()
    context.user_data.pop("draft", None)
    await send_to_admin(context, booking)

    caption = (
        f"<b>Заявка #{bid} отправлена!</b>\n\n"
        f"{booking_text(booking)}\n\n"
        "Мастер подтвердит запись в ближайшее время — я пришлю уведомление."
    )
    rows = [[Btn("📋 Мои записи", callback_data="my")], BACK_MENU]
    await show(update, "done", caption, rows)
    return None


async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, bid: str) -> None:
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Это кнопка для мастера.", show_alert=True)
        return
    b = DB["bookings"].get(bid)
    if not b or b["status"] != "pending":
        await q.answer("Заявка уже обработана или отменена.", show_alert=True)
        await q.edit_message_reply_markup(None)
        return

    confirmed = action == "adm_ok"
    b["status"] = "confirmed" if confirmed else "declined"
    save_db()
    await q.answer("Готово")
    await q.edit_message_text(
        q.message.text_html + f"\n\n<b>{STATUS[b['status']]}</b>", parse_mode="HTML", reply_markup=None
    )

    if confirmed:
        text = f"✅ <b>Запись #{b['id']} подтверждена!</b>\n\n{booking_text(b)}\n📍 {ADDRESS}\n\nЖдём вас!"
        markup = InlineKeyboardMarkup([BACK_MENU])
    else:
        text = (
            f"К сожалению, мастер не сможет принять вас "
            f"{fmt_day(b['date'])} в {b['time']}. Выберите, пожалуйста, другое время."
        )
        markup = InlineKeyboardMarkup([[Btn("🕐 Выбрать другое время", callback_data=f"date:{b['service']}")]])
    try:
        await context.bot.send_message(b["user_id"], text, parse_mode="HTML", reply_markup=markup)
    except TelegramError as e:
        log.error("Не удалось уведомить клиента: %s", e)


# ───────────────────────── АДМИН-ПАНЕЛЬ ─────────────────────────

ADMIN_DAYS_AHEAD = 14  # сколько рабочих дней видно в админке


def is_admin(update: Update) -> bool:
    return ADMIN_ID is not None and update.effective_user.id == ADMIN_ID


async def admin_panel(update: Update) -> None:
    buttons = []
    for day in open_days(ADMIN_DAYS_AHEAD):
        iso = day.isoformat()
        if iso in DB["closed_days"]:
            label = f"🚫 {fmt_day(iso)}"
        else:
            label = f"{fmt_day(iso)} · {len(free_slots(day))}/{len(SLOTS)}"
        buttons.append(Btn(label, callback_data=f"admday:{iso}"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    pending = sum(b["status"] == "pending" for b in DB["bookings"].values())
    rows.append([Btn(f"📨 Необработанные заявки ({pending})", callback_data="admpending")])
    rows.append(BACK_MENU)
    caption = (
        "<b>Админ-панель</b>\n\n"
        "Выберите день, чтобы закрыть или открыть окна.\n"
        "Цифры — свободно / всего, 🚫 — день закрыт."
    )
    await show(update, "calendar", caption, rows)


async def admin_day(update: Update, iso: str) -> None:
    day = date.fromisoformat(iso)
    day_closed = iso in DB["closed_days"]
    buttons = []
    for slot in SLOTS:
        if booking_at(iso, slot):
            label = f"👤 {slot}"
        elif is_closed(iso, slot):
            label = f"🔒 {slot}"
        elif is_past(day, slot):
            label = f"· {slot}"
        else:
            label = f"🟢 {slot}"
        buttons.append(Btn(label, callback_data=f"admslot:{iso}:{slot}"))
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([Btn("✅ Открыть весь день" if day_closed else "🚫 Закрыть весь день", callback_data=f"admclose:{iso}")])
    rows.append([Btn("← Все дни", callback_data="adm")])

    booked = sorted(
        (b for b in DB["bookings"].values() if b["date"] == iso and b["status"] in ACTIVE),
        key=lambda b: b["time"],
    )
    lines = [
        f"{b['time']} — {html.escape(b['name'])}, {SERVICES[b['service']][0]} ({STATUS[b['status']]})"
        for b in booked
    ]
    caption = f"<b>{fmt_day(iso)}</b>"
    if day_closed:
        caption += " — день закрыт 🚫"
    caption += "\n\n🟢 свободно · 🔒 закрыто вами · 👤 запись\nНажмите на окно, чтобы закрыть или открыть его."
    if lines:
        caption += "\n\n<b>Записи:</b>\n" + "\n".join(lines)
    await show(update, "calendar", caption, rows)


def toggle_slot(iso: str, slot: str) -> str | None:
    """Закрывает/открывает окно. Возвращает текст подсказки для мастера."""
    b = booking_at(iso, slot)
    if b:
        return f"Здесь запись #{b['id']}: {b['name']}, {b['phone']}"
    if iso in DB["closed_days"]:
        return "Весь день закрыт — сначала откройте день."
    slots = DB["closed_slots"].setdefault(iso, [])
    if slot in slots:
        slots.remove(slot)
        toast = f"{slot} открыто"
    else:
        slots.append(slot)
        toast = f"{slot} закрыто"
    if not slots:
        del DB["closed_slots"][iso]
    save_db()
    return toast


def toggle_day(iso: str) -> str:
    if iso in DB["closed_days"]:
        DB["closed_days"].remove(iso)
        toast = "День открыт"
    else:
        DB["closed_days"].append(iso)
        toast = "День закрыт"
        if any(booking_at(iso, s) for s in SLOTS):
            toast += ". Внимание: на этот день уже есть записи!"
    save_db()
    return toast


async def resend_pending(context: ContextTypes.DEFAULT_TYPE) -> int:
    pending = [b for b in DB["bookings"].values() if b["status"] == "pending"]
    for b in sorted(pending, key=lambda b: (b["date"], b["time"])):
        await send_to_admin(context, b, resent=True)
    return len(pending)


# ───────────────────────── ОБРАБОТЧИКИ ─────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_phone", None)
    await screen_menu(update)


async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await screen_my(update)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Ваш ID: {update.effective_user.id}")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zayavki — мастер получает все необработанные заявки заново."""
    if not is_admin(update):
        return
    if not await resend_pending(context):
        await update.message.reply_text("Необработанных заявок нет 👌")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Админ-панель доступна только мастеру.")
        return
    await admin_panel(update)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    action, *args = q.data.split(":")

    if action in ("adm_ok", "adm_no"):
        await admin_decision(update, context, action, args[0])
        return

    toast = None
    if action.startswith("adm") and not is_admin(update):
        toast = "Это кнопка для мастера."
    elif action == "adm":
        await admin_panel(update)
    elif action == "admday":
        await admin_day(update, args[0])
    elif action == "admslot":
        toast = toggle_slot(args[0], ":".join(args[1:]))
        await admin_day(update, args[0])
    elif action == "admclose":
        toast = toggle_day(args[0])
        await admin_day(update, args[0])
    elif action == "admpending":
        count = await resend_pending(context)
        toast = f"Отправлено заявок: {count}" if count else "Необработанных заявок нет 👌"
    elif action == "menu":
        await screen_menu(update)
    elif action == "services":
        await screen_services(update, booking=False)
    elif action == "book":
        await screen_services(update, booking=True)
    elif action == "svc":
        await screen_service(update, args[0])
    elif action == "date":
        await screen_dates(update, args[0])
    elif action == "time":
        await screen_times(update, args[0], args[1])
    elif action == "slot":
        key, day, slot = args[0], args[1], ":".join(args[2:])
        if is_busy(day, slot):
            toast = "Это время только что заняли 😔"
            await screen_times(update, key, day)
        else:
            context.user_data["draft"] = {"service": key, "date": day, "time": slot}
            if "phone" in context.user_data:
                await screen_confirm(update, context)
            else:
                await ask_phone(update, context)
    elif action == "phone":
        await ask_phone(update, context)
    elif action == "confirm":
        error = await create_booking(update, context)
        if error:
            toast = error
            await screen_services(update, booking=True)
    elif action == "my":
        await screen_my(update)
    elif action == "contacts":
        await screen_contacts(update)
    elif action in ("resend", "cancel"):
        b = DB["bookings"].get(args[0])
        if not b or b["user_id"] != update.effective_user.id or b["status"] not in ACTIVE:
            toast = "Запись не найдена."
        elif action == "cancel":
            b["status"] = "cancelled"
            save_db()
            toast = "Запись отменена"
            await notify_admin(context, f"🚫 Клиент отменил запись #{b['id']}\n\n{booking_text(b)}")
        elif b["status"] != "pending":
            toast = "Запись уже подтверждена."
        elif now() - datetime.fromisoformat(b.get("sent_at", b["created"])) < RESEND_COOLDOWN:
            toast = "Мастер уже получил заявку недавно. Напомнить можно раз в 30 минут."
        elif await send_to_admin(context, b, resent=True):
            toast = "Заявка отправлена мастеру повторно ✓"
        else:
            toast = "Не получилось отправить. Попробуйте позже."
        await screen_my(update)

    await q.answer(toast, show_alert=bool(toast and len(toast) > 40))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not context.user_data.get("awaiting_phone"):
        await msg.reply_text("Нажмите /start, чтобы открыть меню.")
        return

    if msg.contact:
        phone = msg.contact.phone_number
    else:
        phone = (msg.text or "").strip()
        if sum(ch.isdigit() for ch in phone) < 10:
            await msg.reply_text("Похоже, это не номер телефона. Попробуйте ещё раз, например: +7 900 123-45-67")
            return

    context.user_data["phone"] = phone
    context.user_data["awaiting_phone"] = False
    await msg.reply_text("Спасибо, номер сохранён ✓", reply_markup=ReplyKeyboardRemove())
    if "draft" in context.user_data:
        await screen_confirm(update, context)
    else:
        await screen_menu(update)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("my", "Мои записи"),
    ])
    if ADMIN_ID:
        try:  # мастеру в меню команд дополнительно видны админские команды
            await app.bot.set_my_commands(
                [
                    BotCommand("start", "Главное меню"),
                    BotCommand("admin", "Админ-панель"),
                    BotCommand("zayavki", "Необработанные заявки"),
                ],
                scope=BotCommandScopeChat(ADMIN_ID),
            )
        except TelegramError as e:
            log.warning("Не удалось задать команды мастера: %s", e)
    else:
        log.warning("ADMIN_ID не задан: заявки не будут приходить мастеру. Узнайте свой ID командой /myid.")


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите токен бота в переменной окружения BOT_TOKEN")

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("zayavki", cmd_pending))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), on_message))

    # На Render задана переменная RENDER_EXTERNAL_URL — там работаем через вебхук,
    # чтобы Telegram сам "будил" бесплатный сервер при новом сообщении.
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if external_url:
        log.info("Бот запущен в режиме вебхука.")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path="webhook",
            webhook_url=f"{external_url}/webhook",
            secret_token=hashlib.sha256(token.encode()).hexdigest()[:32],
        )
    else:
        log.info("Бот запущен. Нажмите Ctrl+C для остановки.")
        app.run_polling()


if __name__ == "__main__":
    main()
