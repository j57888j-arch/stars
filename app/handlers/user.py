from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import desc, func, select

from app.config import Settings
from app.core.dates import as_utc
from app.core.texts import SEPARATOR, WELCOME, service_card
from app.database.models import Order, Payment, PromoCode, Referral, Service, User
from app.handlers.utils import get_or_register, session_scope
from app.keyboards.main import (
    ADMIN_MENU, USER_MENU, cancel_keyboard, countries_keyboard,
    service_actions, services_keyboard,
)
from app.services.admin_service import is_admin
from app.services.catalog_service import active_countries, get_service, services_for_country
from app.services.common import enqueue_message
from app.services.order_service import (
    OrderError, cancel_unpaid_order_after_invoice_error,
    create_stars_order, settle_stars_payment,
)
from app.services.support_service import create_ticket
from app.services.user_service import count_user_orders, get_user
from app.states.forms import PromoStates, SupportStates

router = Router(name="user")

USER_NAV_TEXTS = {
    "🌍 Raqamlar katalogi", "📦 Buyurtmalarim", "⭐ Bonuslarim", "💳 Hisobim",
    "👤 Profilim", "🎟 Promo-kod", "👥 Referal", "💬 Yordam",
    "🔙 Orqaga", "❌ Bekor qilish",
}


def _render_price(stars: int) -> str:
    return f"{stars} ⭐ Stars"


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    payload = ""
    if message.text and len(message.text.split(maxsplit=1)) == 2:
        payload = message.text.split(maxsplit=1)[1].strip()
    referral_code = payload.removeprefix("ref_") if payload.startswith("ref_") else None
    async with session_scope() as session:
        user, is_new = await get_or_register(session, message.from_user, referral_code, settings)
        if user.is_blocked:
            await message.answer("⛔ Hisobingiz vaqtincha cheklangan. Support bilan bog‘laning.")
            return
        referred = bool(is_new and user.referred_by_id and settings.referral_bonus_stars > 0)
        admin = await is_admin(session, message.from_user.id, settings.admin_ids)
    welcome_text = WELCOME
    if referred:
        welcome_text += (
            f"\n\n🎁 Siz taklif havolasi orqali qo‘shildingiz. "
            f"Hisobingizga {settings.referral_bonus_stars} ⭐ ichki chegirma bonusi qo‘shildi. "
            "U keyingi Stars xaridingizda avtomatik qo‘llanadi."
        )
    await message.answer(welcome_text, reply_markup=ADMIN_MENU if admin else USER_MENU)


@router.message(F.text == "🔙 Orqaga")
async def user_back(message: Message, state: FSMContext, settings: Settings) -> None:
    # This router is included before the admin router, so route the shared Back
    # button according to the sender's actual role. For customers, retain a
    # validated promo code in FSM data while leaving the input state.
    async with session_scope() as session:
        admin = await is_admin(session, message.from_user.id, settings.admin_ids)
    if admin:
        await state.clear()
        await message.answer("🛡 Admin menyusiga qaytdingiz.", reply_markup=ADMIN_MENU)
        return
    await state.set_state(None)
    await message.answer(
        "🏠 Asosiy menyuga qaytdingiz. Promo-kodingiz saqlangan bo‘lsa, xaridda qo‘llanadi.",
        reply_markup=USER_MENU,
    )


@router.message(F.text == "❌ Bekor qilish")
async def user_cancel(message: Message, state: FSMContext, settings: Settings) -> None:
    async with session_scope() as session:
        admin = await is_admin(session, message.from_user.id, settings.admin_ids)
    await state.clear()
    await message.answer(
        "✅ Joriy amal bekor qilindi.",
        reply_markup=ADMIN_MENU if admin else USER_MENU,
    )


@router.message(F.text.in_({"🌍 Raqamlar katalogi", "🌍 Xizmatlar"}))
async def catalog_message(message: Message, settings: Settings, state: FSMContext) -> None:
    await state.set_state(None)
    if not settings.telegram_stars_enabled:
        await message.answer("⭐ Stars to‘lovi vaqtincha o‘chirilgan. Keyinroq urinib ko‘ring.", reply_markup=USER_MENU)
        return
    async with session_scope() as session:
        countries = await active_countries(session)
    if not countries:
        await message.answer("🌍 Hozircha katalogda faol davlatlar yo‘q.", reply_markup=USER_MENU)
        return
    await message.answer(
        "╭━━ 🌍 DAVLATLAR ━━╮\n\nXizmat ko‘rsatiladigan davlatni tanlang:",
        reply_markup=countries_keyboard(countries),
    )


@router.callback_query(F.data == "home")
async def home_callback(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await call.message.answer(WELCOME, reply_markup=USER_MENU)
    await call.answer()


@router.callback_query(F.data == "catalog")
async def catalog_callback(call: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    await state.set_state(None)
    if not settings.telegram_stars_enabled:
        await call.answer("Stars to‘lovi vaqtincha o‘chirilgan.", show_alert=True)
        return
    async with session_scope() as session:
        countries = await active_countries(session)
    await call.message.edit_text(
        "╭━━ 🌍 DAVLATLAR ━━╮\n\nXizmat ko‘rsatiladigan davlatni tanlang:",
        reply_markup=countries_keyboard(countries),
    )
    await call.answer()


@router.callback_query(F.data.regexp(r"^country:\d+$"))
async def country_callback(call: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    await state.set_state(None)
    if not settings.telegram_stars_enabled:
        await call.answer("Stars to‘lovi vaqtincha o‘chirilgan.", show_alert=True)
        return
    country_id = int(call.data.split(":", 1)[1])
    async with session_scope() as session:
        services = await services_for_country(session, country_id)
    if not services:
        await call.answer("Bu davlatda Stars narxi sozlangan faol xizmat yo‘q.", show_alert=True)
        return
    await call.message.edit_text(
        "╭━━ ✨ XIZMATLAR ━━╮\n\nXarid qilish uchun xizmatni tanlang:",
        reply_markup=services_keyboard(services),
    )
    await call.answer()


@router.callback_query(F.data.regexp(r"^service:\d+$"))
async def service_callback(call: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    await state.set_state(None)
    if not settings.telegram_stars_enabled:
        await call.answer("Stars to‘lovi vaqtincha o‘chirilgan.", show_alert=True)
        return
    service_id = int(call.data.split(":", 1)[1])
    async with session_scope() as session:
        item = await get_service(session, service_id)
    if not item:
        await call.answer("Xizmat topilmadi yoki sotuvdan olingan.", show_alert=True)
        return
    service, available = item
    if not service.stars_price or service.stars_price < 1:
        await call.answer("Bu xizmat uchun Stars narxi hali belgilanmagan.", show_alert=True)
        return
    card = service_card(service.name, service.description, service.stars_price, available)
    markup = service_actions(service.id, service.country_id, stars_available=available > 0)
    await call.message.edit_text(card, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("buystars:"))
async def buy_stars_callback(call: CallbackQuery, state: FSMContext, bot, settings: Settings) -> None:
    if not settings.telegram_stars_enabled:
        await call.answer("Telegram Stars to‘lovi o‘chirilgan.", show_alert=True)
        return
    try:
        service_id = int(call.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await call.answer("Xizmat identifikatori noto‘g‘ri.", show_alert=True)
        return
    promo_code = (await state.get_data()).get("promo_code")
    order_id: int | None = None
    try:
        async with session_scope() as session:
            user = await session.scalar(select(User).where(User.telegram_id == call.from_user.id))
            if not user:
                raise OrderError("Avval /start orqali ro‘yxatdan o‘ting.")
            order, payment = await create_stars_order(
                session, user=user, service_id=service_id, quantity=1,
                promo_code=promo_code, settings=settings,
            )
            order_id = order.id
            service_name = (await session.get(Service, service_id)).name
            service_description = (await session.get(Service, service_id)).description
        # Exactly one LabeledPrice is required for Telegram Stars invoices.
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=(service_name or "WeraqamBot xizmati")[:32],
            description=(service_description or "Telegram Stars orqali xizmat xaridi")[:255],
            payload=payment.payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=(service_name or "Xizmat")[:32], amount=payment.amount_minor)],
            start_parameter=f"order_{order.id}",
        )
        # Promo is attached to the created order; a later retry won't apply it twice.
        await state.update_data(promo_code=None)
        await call.answer("⭐ Stars invoice yuborildi")
    except (OrderError, ValueError) as exc:
        await call.answer(str(exc), show_alert=True)
    except TelegramAPIError as exc:
        if order_id is not None:
            async with session_scope() as session:
                await cancel_unpaid_order_after_invoice_error(
                    session, order_id=order_id, reason=f"Telegram invoice xatosi: {type(exc).__name__}",
                )
        await call.answer("Invoice yuborilmadi. Buyurtma va zaxira bekor qilindi; qayta urinib ko‘ring.", show_alert=True)


@router.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery, settings: Settings) -> None:
    ok = False
    reason = "To‘lov ma’lumotlari noto‘g‘ri yoki buyurtma muddati tugagan. Qayta urinib ko‘ring."
    now = datetime.now(timezone.utc)
    try:
        if settings.telegram_stars_enabled and query.currency == "XTR":
            async with session_scope() as session:
                payment = await session.scalar(
                    select(Payment).where(Payment.payload == query.invoice_payload).with_for_update()
                )
                buyer = await session.scalar(select(User).where(User.telegram_id == query.from_user.id))
                if (
                    payment and payment.provider == "telegram_stars" and payment.status == "pending"
                    and payment.currency == "XTR" and payment.amount_minor == query.total_amount
                    and buyer and not buyer.is_blocked and payment.user_id == buyer.id
                    and payment.order_id is not None
                ):
                    order = await session.scalar(select(Order).where(Order.id == payment.order_id).with_for_update())
                    if (
                        order and order.payment_method == "stars" and order.status == "awaiting_payment"
                        and (order.expires_at is None or as_utc(order.expires_at) > now)
                    ):
                        # Give Telegram's payment confirmation webhook a generous
                        # reconciliation window after pre-checkout is approved.
                        order.expires_at = now + timedelta(hours=1)
                        ok = True
    except Exception:
        # Telegram requires an answer to pre_checkout_query quickly. Fail closed;
        # do not approve a charge if the database could not validate its payload.
        import logging
        logging.getLogger(__name__).exception("Stars pre-checkout validation failed")
        ok = False
    await query.answer(ok=ok, error_message=None if ok else reason)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message, settings: Settings) -> None:
    successful = message.successful_payment
    if not successful:
        return
    try:
        async with session_scope() as session:
            payment_before = await session.scalar(
                select(Payment).where(Payment.payload == successful.invoice_payload).with_for_update()
            )
            first_confirmation = bool(payment_before and payment_before.status == "pending")
            payment, order = await settle_stars_payment(
                session, payload=successful.invoice_payload,
                telegram_charge_id=successful.telegram_payment_charge_id,
                user_telegram_id=message.from_user.id,
                paid_amount=successful.total_amount, currency=successful.currency,
            )
            if first_confirmation:
                alert = (
                    f"⭐ YANGI STARS BUYURTMA\n"
                    f"📦 Buyurtma: {order.order_number}\n"
                    f"🧾 ID: {order.id}\n"
                    f"💰 To‘lov: {successful.total_amount} XTR\n"
                    f"👤 Foydalanuvchi: {message.from_user.id}\n"
                    "🛠 Admin paneldan buyurtmani qabul qiling."
                )
                for admin_id in settings.admin_recipients:
                    await enqueue_message(session, admin_id, alert)
    except OrderError as exc:
        await message.answer(
            "⚠️ Telegram to‘lov xabari kelib tushdi, lekin buyurtmani qayd etishda muammo yuz berdi. "
            f"Supportga murojaat qiling. Kod: {str(exc)[:160]}"
        )


@router.message(F.text == "📦 Buyurtmalarim")
async def my_orders_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    async with session_scope() as session:
        user = await get_user(session, message.from_user.id)
        rows = (await session.scalars(
            select(Order).where(Order.user_id == user.id).order_by(desc(Order.created_at)).limit(10)
        )).all() if user else []
        service_ids = {row.service_id for row in rows}
        order_ids = {row.id for row in rows}
        services = {sid: await session.get(Service, sid) for sid in service_ids}
        payments = (await session.scalars(select(Payment).where(Payment.order_id.in_(order_ids)))).all() if order_ids else []
        payment_by_order = {item.order_id: item for item in payments}
    if not rows:
        await message.answer("📦 Sizda hali buyurtmalar yo‘q.", reply_markup=USER_MENU)
        return
    status_names = {
        "awaiting_payment": "⭐ Stars to‘lovi kutilmoqda", "paid": "🕒 Admin navbatida",
        "processing": "🛠 Bajarilmoqda", "completed": "✅ Bajarildi",
        "cancelled": "Bekor qilindi", "rejected": "Rad etildi", "refunded": "Qaytarildi",
        "refund_pending": "Refund jarayonida",
    }
    lines = ["╭━━ 📦 BUYURTMALARIM ━━╮"]
    for order in rows:
        service = services.get(order.service_id)
        lines.append(
            f"\n🔹 {order.order_number}\n🧩 {service.name if service else 'Xizmat'} × {order.quantity}"
            f"\n⭐ {payment_by_order[order.id].amount_minor if order.id in payment_by_order else (order.stars_subtotal or '—')} XTR\n📌 {status_names.get(order.status, order.status)}"
        )
    await message.answer("\n".join(lines), reply_markup=USER_MENU)


@router.message(F.text.in_({"⭐ Bonuslarim", "💳 Hisobim"}))
async def bonus_balance_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    async with session_scope() as session:
        user = await get_user(session, message.from_user.id)
        balance = int(user.referral_bonus_stars or 0) if user else 0
        payments = (await session.scalars(
            select(Payment).where(
                Payment.user_id == user.id, Payment.provider == "telegram_stars"
            ).order_by(desc(Payment.created_at)).limit(5)
        )).all() if user else []
    lines = [
        "╭━━ ⭐ BONUSLARIM ━━╮\n",
        f"🎁 Ichki Stars chegirmasi: {balance} ⭐",
        "\nBu bonus keyingi Stars xaridingiz invoice summasidan avtomatik chegiriladi.",
        "Eslatma: bu Telegram akkauntingizga yuboriladigan Stars emas, bot ichidagi xarid chegirmasidir.",
        f"\n{SEPARATOR}\n🧾 SO‘NGGI TO‘LOVLAR",
    ]
    if payments:
        for item in payments:
            lines.append(f"#{item.id} · {item.amount_minor} ⭐ · {item.status}")
    else:
        lines.append("Hali to‘lovlar mavjud emas.")
    await message.answer("\n".join(lines), reply_markup=USER_MENU)


@router.message(F.text == "👤 Profilim")
async def profile_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    async with session_scope() as session:
        user = await get_user(session, message.from_user.id)
        order_count = await count_user_orders(session, user.id) if user else 0
        bonus = int(user.referral_bonus_stars or 0) if user else 0
    if not user:
        await message.answer("Avval /start bosing.", reply_markup=USER_MENU)
        return
    await message.answer(
        "╭━━━ 👤 PROFIL ━━━╮\n\n"
        f"🆔 ID: {user.telegram_id}\n"
        f"👤 Ism: {user.full_name or user.first_name}\n"
        f"🔗 Username: @{user.username or '—'}\n"
        f"📦 Buyurtmalar: {order_count}\n"
        f"🎁 Bonus: {bonus} ⭐\n"
        f"📅 Ro‘yxatdan o‘tgan: {user.created_at:%d.%m.%Y}\n"
        f"{SEPARATOR}\nMa’lumotlaringiz himoyalangan.",
        reply_markup=USER_MENU,
    )


@router.message(F.text.in_({"👥 Referal", "🎟 Referal"}))
async def referral_handler(message: Message, bot, state: FSMContext, settings: Settings) -> None:
    await state.set_state(None)
    async with session_scope() as session:
        user = await get_user(session, message.from_user.id)
        total = int(await session.scalar(
            select(func.count(Referral.id)).where(Referral.referrer_user_id == user.id)
        ) or 0) if user else 0
        rewarded = int(await session.scalar(
            select(func.count(Referral.id)).where(
                Referral.referrer_user_id == user.id, Referral.status == "rewarded"
            )
        ) or 0) if user else 0
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user.referral_code}" if user else ""
    await message.answer(
        "╭━━ 👥 REFERAL ━━╮\n\n"
        f"👤 Siz taklif qilganlar: {total}\n"
        f"🎁 Bonus olgan yangi foydalanuvchilar: {rewarded}\n"
        f"🎁 Sizdagi ichki Stars bonus: {user.referral_bonus_stars if user else 0} ⭐\n\n"
        f"🔗 Shaxsiy havolangiz:\n{link}\n\n"
        f"🎁 Havola orqali yangi qo‘shilgan foydalanuvchiga {settings.referral_bonus_stars} ⭐ ichki chegirma bonusi darhol beriladi. "
        "Bu Telegram Stars transferi emas; faqat bot ichidagi keyingi xaridni arzonlashtiradi.",
        reply_markup=USER_MENU,
    )


@router.message(F.text == "🎟 Promo-kod")
async def promo_start(message: Message, state: FSMContext) -> None:
    await state.update_data(promo_code=None)
    await state.set_state(PromoStates.code)
    await message.answer(
        "🏷 Promo-kodni kiriting.\n\nOrqaga qaytish uchun 🔙 Orqaga tugmasini bosing.",
        reply_markup=cancel_keyboard(),
    )


@router.message(PromoStates.code, F.text.not_in(USER_NAV_TEXTS))
async def promo_receive(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().upper()
    if not code or len(code) > 48:
        await message.answer("Promo-kod 1–48 belgidan iborat bo‘lishi kerak.")
        return
    async with session_scope() as session:
        promo = await session.scalar(select(PromoCode).where(PromoCode.code == code, PromoCode.is_active.is_(True)))
    if not promo:
        await message.answer("❌ Promo-kod topilmadi yoki faol emas. Qayta kiriting:")
        return
    now = datetime.now(timezone.utc)
    if promo.starts_at and as_utc(promo.starts_at) > now:
        await message.answer("⏳ Bu promo-kod hali kuchga kirmagan.")
        return
    if promo.expires_at and as_utc(promo.expires_at) <= now:
        await message.answer("⌛ Promo-kod muddati tugagan.")
        return
    await state.update_data(promo_code=code)
    await state.set_state(None)
    discount = f"{promo.discount_value}%" if promo.discount_type == "percent" else f"{promo.discount_value} UZS ichki chegirma"
    await message.answer(
        f"✅ PROMO FAOLLASHTIRILDI\n\n🏷 Kod: {code}\n🎁 Chegirma: {discount}\n\nEndi katalogdan xizmat tanlang.",
        reply_markup=USER_MENU,
    )


@router.message(F.text == "💬 Yordam")
async def support_start(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.set_state(None)
    if not settings.admin_recipients:
        await message.answer("⚠️ Support hozircha sozlanmagan. Keyinroq urinib ko‘ring.", reply_markup=USER_MENU)
        return
    await state.set_state(SupportStates.message)
    await message.answer(
        "💬 Murojaatingizni bitta xabarda yozing. Operatorlar javob beradi.\n\n"
        "Orqaga qaytish uchun 🔙 Orqaga tugmasini bosing.",
        reply_markup=cancel_keyboard(),
    )


@router.message(SupportStates.message, F.text.not_in(USER_NAV_TEXTS))
async def support_receive(message: Message, state: FSMContext, settings: Settings) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Iltimos, murojaatingizni matn ko‘rinishida yuboring.")
        return
    try:
        async with session_scope() as session:
            user = await get_user(session, message.from_user.id)
            if not user:
                raise ValueError("Avval /start orqali ro‘yxatdan o‘ting.")
            ticket = await create_ticket(
                session, user=user, message=text, admin_ids=settings.admin_recipients,
            )
        await state.set_state(None)
        await message.answer(
            f"✅ Murojaatingiz qabul qilindi.\n🎫 Ticket: #{ticket.id}\n\nOperator javob berganda sizga shu bot orqali xabar keladi.",
            reply_markup=USER_MENU,
        )
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")


@router.message(Command("paysupport"))
async def pay_support(message: Message, settings: Settings) -> None:
    username = f"@{settings.support_username}" if settings.support_username else "bot ichidagi 💬 Yordam bo‘limi"
    await message.answer(
        "⭐ Telegram Stars to‘lovi bo‘yicha yordam\n\n"
        f"Buyurtma/to‘lov savollari uchun: {username}.\n"
        "To‘lov ID yoki buyurtma raqamini murojaatda ko‘rsating."
    )


@router.message(SupportStates.message)
async def support_non_text(message: Message) -> None:
    await message.answer("💬 Murojaatni matn ko‘rinishida yuboring yoki 🔙 Orqaga tugmasini bosing.")


@router.message(PromoStates.code)
async def promo_non_text(message: Message) -> None:
    await message.answer("🏷 Promo-kodni matn ko‘rinishida yuboring yoki 🔙 Orqaga tugmasini bosing.")
