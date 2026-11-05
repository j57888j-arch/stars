from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

USER_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🌍 Raqamlar katalogi", style="primary"), KeyboardButton(text="📦 Buyurtmalarim", style="primary")],
        [KeyboardButton(text="⭐ Bonuslarim", style="success"), KeyboardButton(text="👤 Profilim", style="primary")],
        [KeyboardButton(text="🎟 Promo-kod", style="success"), KeyboardButton(text="👥 Referal", style="success")],
        [KeyboardButton(text="💬 Yordam", style="primary")],
    ], resize_keyboard=True, is_persistent=True, input_field_placeholder="Kerakli bo‘limni tanlang…",
)

ADMIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Dashboard", style="primary"), KeyboardButton(text="📦 Buyurtmalar", style="primary")],
        [KeyboardButton(text="👥 Foydalanuvchilar", style="primary"), KeyboardButton(text="⭐ Stars to‘lovlari", style="success")],
        [KeyboardButton(text="🌍 Davlatlar", style="primary"), KeyboardButton(text="🧩 Xizmatlar", style="primary")],
        [KeyboardButton(text="📦 Zaxira", style="primary"), KeyboardButton(text="🎟 Promo yaratish", style="success")],
        [KeyboardButton(text="📈 Hisobotlar", style="primary"), KeyboardButton(text="📢 Xabar yuborish", style="primary")],
        [KeyboardButton(text="💬 Support", style="primary"), KeyboardButton(text="🛡 Admin rollari", style="primary")],
        [KeyboardButton(text="🔙 Bosh menyu", style="danger")],
    ], resize_keyboard=True, is_persistent=True, input_field_placeholder="Admin boshqaruvi…",
)


def countries_keyboard(countries) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{c.emoji} {c.name}", callback_data=f"country:{c.id}", style="primary")] for c in countries]
    rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_keyboard(services) -> InlineKeyboardMarkup:
    rows = []
    for service, available in services:
        rows.append([InlineKeyboardButton(
            text=f"🧩 {service.name} · ⭐ {service.stars_price} · {available} dona",
            callback_data=f"service:{service.id}", style="primary",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Davlatlar", callback_data="catalog", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_actions(service_id: int, country_id: int, *, stars_available: bool) -> InlineKeyboardMarkup:
    rows = []
    if stars_available:
        rows.append([InlineKeyboardButton(text="⭐ Stars orqali xarid qilish", callback_data=f"buystars:{service_id}", style="success")])
    rows.append([InlineKeyboardButton(text="⬅️ Xizmatlarga qaytish", callback_data=f"country:{country_id}", style="primary")])
    rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="home", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_actions(order_id: int, *, can_manage: bool = False) -> InlineKeyboardMarkup:
    if not can_manage:
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Qabul qilish", callback_data=f"orderclaim:{order_id}", style="success")],
        [InlineKeyboardButton(text="🎉 Bajarildi", callback_data=f"orderdone:{order_id}", style="success"),
         InlineKeyboardButton(text="✖️ Rad etish", callback_data=f"orderreject:{order_id}", style="danger")],
    ])


def stars_pay_button(payment_id: int, order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Stars invoice tayyorlash", callback_data=f"starsinvoice:{payment_id}:{order_id}")
    ]])


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Orqaga", style="primary"), KeyboardButton(text="❌ Bekor qilish", style="danger")]],
        resize_keyboard=True,
    )


def choose_country_keyboard(countries) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{c.emoji} {c.name}", callback_data=f"admincountry:{c.id}", style="primary")]
        for c in countries
    ])


def admin_service_keyboard(services) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{s.name} · #{s.id}", callback_data=f"adminstock:{s.id}", style="primary")]
        for s in services
    ])
