from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, desc, func, or_, select

from app.config import Settings
from app.core.money import format_money, parse_amount
from app.core.order_states import ensure_transition
from app.core.crypto import encrypt_resource
from app.database.models import (
    AdminRole, AdminUser, AdminUserRole, BroadcastCampaign, Country, Inventory, InventoryItem, InventoryMovement, Order,
    Payment, PromoCode, Service, SupportTicket, User, UserNote, OrderEvent, OutboxEvent,
)
from app.handlers.utils import session_scope
from app.keyboards.main import (
    ADMIN_MENU, USER_MENU, admin_service_keyboard, choose_country_keyboard,
    order_actions, cancel_keyboard,
)
from app.services.admin_service import has_permission, is_admin
from app.services.broadcast_service import queue_broadcast
from app.services.common import audit
from app.services.catalog_service import (
    create_country, create_service, set_available_stock, set_country_active,
    set_service_active, update_service_price, update_service_stars_price,
)
from app.services.order_service import (
    OrderError, claim_order, complete_order, reject_order,
)
from app.services.promo_service import create_promo
from app.services.statistics_service import dashboard
from app.services.reports_service import collect_order_report, csv_bytes, xlsx_bytes, pdf_bytes
from app.states.forms import (
    AdminBroadcastStates, AdminCountryStates, AdminInventoryImportStates, AdminPromoStates,
    AdminSearchStates, AdminServiceStates, AdminStockStates, AdminEditServiceStates,
)

router = Router(name="admin")


async def _allowed(telegram_id: int, settings: Settings, permission: str | None = None) -> bool:
    async with session_scope() as session:
        if permission:
            return await has_permission(session, telegram_id, permission, settings.admin_ids)
        return await is_admin(session, telegram_id, settings.admin_ids)


async def _deny(message: Message | CallbackQuery) -> None:
    if isinstance(message, CallbackQuery):
        await message.answer("⛔ Bu bo‘lim siz uchun yopiq.", show_alert=True)
    else:
        await message.answer("⛔ Admin huquqi talab qilinadi.")


@router.message(Command("admin"))
async def admin_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings):
        await _deny(message)
        return
    await message.answer(
        "╭━━━ 🛡 ADMIN CONTROL ━━━╮\n\n"
        "WeraqamBot boshqaruv markazi.\n"
        "Amallar audit jurnaliga yoziladi.\n"
        "Kerakli modulni tanlang.",
        reply_markup=ADMIN_MENU,
    )


@router.message(F.text == "🔙 Bosh menyu")
async def back_to_user(message: Message) -> None:
    await message.answer("🏠 Foydalanuvchi menyusi", reply_markup=USER_MENU)


@router.message(F.text.in_({"🔙 Orqaga", "❌ Bekor qilish"}))
async def admin_back_or_cancel(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Bekor qilish":
        await state.clear()
        await message.answer("✅ Joriy admin amali bekor qilindi.", reply_markup=ADMIN_MENU)
    else:
        await state.set_state(None)
        await message.answer("🛡 Admin menyusiga qaytdingiz.", reply_markup=ADMIN_MENU)


@router.message(F.text == "📊 Dashboard")
async def dashboard_handler(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "statistics.view"):
        await _deny(message)
        return
    async with session_scope() as session:
        data = await dashboard(session)
    await message.answer(
        "╭━━ 📊 DASHBOARD ━━╮\n\n"
        f"👥 Jami foydalanuvchilar: {data['users']}\n"
        f"🆕 Bugungi foydalanuvchilar: {data['new_users_today']}\n"
        f"🟢 30 kunda faol: {data['active_users_30d']}\n"
        f"📦 Jami buyurtmalar: {data['orders']}\n"
        f"📅 Bugungi buyurtmalar: {data['orders_today']}\n"
        f"✅ Bajarilgan: {data['completed']}\n"
        f"⏳ Kutilayotgan: {data['pending']}\n"
        f"❌ Bekor/refund: {data['cancelled']}\n"
        f"⭐ Jami Stars tushumi: {data['stars_total']} XTR\n"
        f"📅 Bugungi Stars: {data['stars_today']} XTR\n"
        f"🗓 Oylik Stars: {data['stars_month']} XTR\n"
        f"📈 Oxirgi 7 kun buyurtmalari: {data['week_orders']}",
        reply_markup=ADMIN_MENU,
    )


@router.message(F.text == "📈 Hisobotlar")
async def reports_menu(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "statistics.view"):
        await _deny(message)
        return
    await message.answer(
        "📈 HISOBOTLAR\n\n"
        "Buyurtmalar va to‘lovlar hisobotini sana oralig‘i bo‘yicha eksport qiling.\n"
        "Formatlar: CSV, XLSX, PDF.\n\n"
        "Buyruq: /report YYYY-MM-DD YYYY-MM-DD csv|xlsx|pdf\n"
        "Misol: /report 2026-10-01 2026-11-04 xlsx\n\n"
        "Sana oralig‘i Asia/Tashkent vaqt zonasi bo‘yicha talqin qilinadi.",
        reply_markup=ADMIN_MENU,
    )


@router.message(Command("report"))
async def report_export_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "statistics.view"):
        await _deny(message)
        return
    parts = (message.text or "").split()
    if len(parts) != 4:
        await message.answer("Foydalanish: /report YYYY-MM-DD YYYY-MM-DD csv|xlsx|pdf")
        return
    try:
        date_from = date.fromisoformat(parts[1])
        date_to = date.fromisoformat(parts[2])
        file_type = parts[3].lower()
        if date_to < date_from or (date_to - date_from).days > 365:
            raise ValueError("Sana oralig‘i 1–366 kun bo‘lishi kerak.")
        if file_type not in {"csv", "xlsx", "pdf"}:
            raise ValueError("Format csv, xlsx yoki pdf bo‘lishi kerak.")
        tz = ZoneInfo("Asia/Tashkent")
        start_utc = datetime.combine(date_from, time.min, tzinfo=tz).astimezone(timezone.utc)
        end_utc = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
        async with session_scope() as session:
            records, summary = await collect_order_report(session, start_utc=start_utc, end_utc=end_utc)
        if file_type == "csv":
            data = csv_bytes(records)
        elif file_type == "xlsx":
            data = xlsx_bytes(records, summary)
        else:
            data = pdf_bytes(records, summary, f"WeraqamBot hisobot: {date_from} — {date_to}")
        filename = f"weraqambot_report_{date_from}_{date_to}.{file_type}"
        await message.answer_document(
            BufferedInputFile(data, filename=filename),
            caption=f"📈 Hisobot tayyor\nBuyurtmalar: {summary['orders']}\nDavr: {date_from} — {date_to}",
        )
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
    except Exception as exc:
        await message.answer(f"⚠️ Hisobot yaratilmadi: {type(exc).__name__}")


@router.message(F.text == "📦 Buyurtmalar")
async def admin_orders(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "orders.view"):
        await _deny(message)
        return
    async with session_scope() as session:
        orders = (await session.scalars(
            select(Order).where(Order.status.in_(["paid", "processing"])).order_by(desc(Order.created_at)).limit(20)
        )).all()
        users = {o.id: await session.get(User, o.user_id) for o in orders}
        services = {o.id: await session.get(Service, o.service_id) for o in orders}
        order_ids = [o.id for o in orders]
        payments = (await session.scalars(
            select(Payment).where(Payment.order_id.in_(order_ids)).order_by(Payment.id.desc())
        )).all() if order_ids else []
        payments_by_order = {}
        for payment in payments:
            payments_by_order.setdefault(payment.order_id, payment)
    if not orders:
        await message.answer("📦 Hozir bajarilishi kerak bo‘lgan buyurtma yo‘q.", reply_markup=ADMIN_MENU)
        return
    await message.answer(f"╭━━ 📦 BUYURTMALAR ({len(orders)}) ━━╮")
    for order in orders:
        user = users.get(order.id)
        service = services.get(order.id)
        status = "🟡 Operator navbatida" if order.status == "paid" else "🔵 Bajarilmoqda"
        text = (
            f"📦 #{order.order_number} · ID {order.id}\n"
            f"🧩 {service.name if service else 'Xizmat'} × {order.quantity}\n"
            f"👤 User: {user.telegram_id if user else '—'}\n"
            f"⭐ {payments_by_order[order.id].amount_minor if order.id in payments_by_order else '—'} XTR\n"
            f"{status}"
        )
        if order.status == "paid":
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Qabul qilish", callback_data=f"orderclaim:{order.id}", style="success")]])
        else:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎉 Bajarildi", callback_data=f"orderdone:{order.id}", style="success"),
                InlineKeyboardButton(text="✖️ Rad etish", callback_data=f"orderreject:{order.id}", style="danger"),
            ]])
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("orderclaim:"))
async def order_claim_callback(call: CallbackQuery, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "orders.manage"):
        await _deny(call)
        return
    order_id = int(call.data.split(":")[1])
    try:
        async with session_scope() as session:
            order = await claim_order(session, order_id=order_id, admin_telegram_id=call.from_user.id, bootstrap_ids=settings.admin_ids)
        await call.message.edit_text(call.message.text + "\n\n✅ Siz buyurtmani qabul qildingiz.")
        await call.answer("Buyurtma sizga biriktirildi")
    except OrderError as exc:
        await call.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("orderdone:"))
async def order_complete_callback(call: CallbackQuery, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "orders.manage"):
        await _deny(call)
        return
    order_id = int(call.data.split(":")[1])
    try:
        async with session_scope() as session:
            order = await complete_order(session, order_id=order_id, admin_telegram_id=call.from_user.id, settings=settings)
        await call.message.edit_text(call.message.text + "\n\n🎉 Buyurtma yakunlandi.")
        await call.answer("Bajarildi")
    except OrderError as exc:
        await call.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("orderreject:"))
async def order_reject_callback(call: CallbackQuery, settings: Settings, bot) -> None:
    if not await _allowed(call.from_user.id, settings, "orders.manage"):
        await _deny(call)
        return
    order_id = int(call.data.split(":")[1])
    refund_context: tuple[int, str, int, str] | None = None
    try:
        # Mark the order/payment as refund_pending in a short DB transaction before
        # calling Telegram. This prevents a concurrent operator from completing it.
        async with session_scope() as session:
            order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
            if not order or order.status not in {"paid", "processing", "awaiting_payment"}:
                raise OrderError("Bu buyurtmani rad etib bo‘lmaydi.")
            if order.payment_method == "stars" and order.status in {"paid", "processing"}:
                payment = await session.scalar(
                    select(Payment).where(Payment.order_id == order.id, Payment.status == "paid").with_for_update()
                )
                user = await session.get(User, order.user_id)
                if not payment or not payment.external_id or not user:
                    raise OrderError("Stars refund uchun to‘lov charge ID topilmadi.")
                if order.status == "processing":
                    admin = await session.get(AdminUser, order.claimed_by_admin_id) if order.claimed_by_admin_id else None
                    actor_user_id = await session.scalar(select(User.id).where(User.telegram_id == call.from_user.id))
                    if not admin or admin.user_id != actor_user_id:
                        raise OrderError("Bu buyurtma boshqa admin tomonidan qabul qilingan.")
                previous_status = order.status
                ensure_transition(previous_status, "refund_pending")
                order.status = "refund_pending"
                payment.status = "refund_pending"
                payment.rejection_reason = previous_status
                session.add(OrderEvent(
                    order_id=order.id, old_status=previous_status, new_status="refund_pending",
                    actor_telegram_id=call.from_user.id, comment="Telegram Stars refund boshlandi",
                ))
                refund_context = (user.telegram_id, payment.external_id, payment.id, previous_status)
            else:
                await reject_order(
                    session, order_id=order_id, admin_telegram_id=call.from_user.id,
                    settings=settings, stars_refund_confirmed=False,
                )

        if refund_context:
            tg_user_id, charge_id, payment_id, previous_status = refund_context
            try:
                await bot.refund_star_payment(
                    user_id=tg_user_id, telegram_payment_charge_id=charge_id
                )
            except Exception:
                # Restore the previous state when Telegram refuses the refund.
                async with session_scope() as session:
                    payment = await session.scalar(
                        select(Payment).where(Payment.id == payment_id).with_for_update()
                    )
                    order = await session.scalar(
                        select(Order).where(Order.id == order_id).with_for_update()
                    )
                    if payment and payment.status == "refund_pending":
                        payment.status = "paid"
                        payment.rejection_reason = ""
                    if order and order.status == "refund_pending":
                        order.status = previous_status
                        session.add(OrderEvent(
                            order_id=order.id, old_status="refund_pending", new_status=previous_status,
                            actor_telegram_id=call.from_user.id, comment="Telegram refund muvaffaqiyatsiz; holat tiklandi",
                        ))
                raise

            # Telegram refund succeeded; now release stock and close the order atomically.
            async with session_scope() as session:
                payment = await session.scalar(
                    select(Payment).where(Payment.id == payment_id).with_for_update()
                )
                if not payment or payment.status != "refund_pending":
                    raise OrderError("Refund holati o‘zgargan. Moliyaviy tekshiruv talab qilinadi.")
                await reject_order(
                    session, order_id=order_id, admin_telegram_id=call.from_user.id,
                    settings=settings, stars_refund_confirmed=True,
                )
                payment.status = "refunded"
                payment.rejection_reason = ""
        await call.message.edit_text((call.message.text or "") + "\n\n✖️ Buyurtma rad etildi.")
        await call.answer("Buyurtma rad etildi")
    except Exception as exc:
        await call.answer(str(exc)[:180], show_alert=True)


@router.message(F.text == "🌍 Davlatlar")
async def countries_admin(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "catalog.manage"):
        await _deny(message)
        return
    async with session_scope() as session:
        countries = (await session.scalars(select(Country).order_by(Country.name))).all()
    lines = ["╭━━ 🌍 DAVLATLAR ━━╮"]
    lines += [f"{c.emoji} {c.name} · ID {c.id} · {'Faol' if c.is_active else 'O‘chiq'}" for c in countries]
    rows = [
        [InlineKeyboardButton(
            text=f"{'🟢' if c.is_active else '⚪'} {c.emoji} {c.name}",
            callback_data=f"countrytoggle:{c.id}", style="success" if c.is_active else "danger",
        )]
        for c in countries
    ]
    rows.append([InlineKeyboardButton(text="➕ Davlat qo‘shish", callback_data="country:new", style="success")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("\n".join(lines) if len(lines) > 1 else "Davlatlar hali yo‘q.", reply_markup=markup)


@router.callback_query(F.data.startswith("countrytoggle:"))
async def country_toggle_callback(call: CallbackQuery, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "catalog.manage"):
        await _deny(call)
        return
    country_id = int(call.data.split(":", 1)[1])
    async with session_scope() as session:
        country = await session.scalar(select(Country).where(Country.id == country_id))
        if not country:
            await call.answer("Davlat topilmadi", show_alert=True)
            return
        updated = await set_country_active(
            session, country_id=country_id, is_active=not country.is_active,
            admin_telegram_id=call.from_user.id,
        )
        status = "faollashtirildi" if updated.is_active else "o‘chirildi"
    await call.answer(f"{status.title()}")
    await call.message.answer(f"✅ {country.name}: {status}.")


@router.callback_query(F.data == "country:new")
async def country_new(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "catalog.manage"):
        await _deny(call)
        return
    await state.set_state(AdminCountryStates.name)
    await call.message.answer("Davlat nomini kiriting:")
    await call.answer()


@router.message(AdminCountryStates.name)
async def country_name(message: Message, state: FSMContext) -> None:
    if not (message.text or "").strip():
        await message.answer("Nom bo‘sh bo‘lmasin. Qayta kiriting:")
        return
    await state.update_data(country_name=message.text.strip())
    await state.set_state(AdminCountryStates.emoji)
    await message.answer("Davlat emoji belgisi (masalan 🇺🇿):")


@router.message(AdminCountryStates.emoji)
async def country_emoji(message: Message, state: FSMContext) -> None:
    await state.update_data(country_emoji=(message.text or "🌍").strip()[:16])
    await state.set_state(AdminCountryStates.description)
    await message.answer("Qisqa tavsif kiriting yoki — yuboring:")


@router.message(AdminCountryStates.description)
async def country_description(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    try:
        async with session_scope() as session:
            country = await create_country(
                session, name=data["country_name"], emoji=data["country_emoji"],
                description="" if message.text == "—" else message.text or "",
                admin_telegram_id=message.from_user.id,
            )
        await state.clear()
        await message.answer(f"✅ Davlat qo‘shildi: {country.emoji} {country.name} · ID {country.id}", reply_markup=ADMIN_MENU)
    except Exception as exc:
        await message.answer(f"⚠️ Davlat yaratilmadi: {exc}")


@router.message(F.text == "🧩 Xizmatlar")
async def services_admin(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "catalog.manage"):
        await _deny(message)
        return
    async with session_scope() as session:
        services = (await session.scalars(select(Service).order_by(Service.id.desc()).limit(30))).all()
    lines = ["╭━━ ✨ XIZMATLAR ━━╮", "\nFoydalanuvchiga faqat Stars narxi kiritilgan faol xizmatlar ko‘rinadi."]
    for service in services:
        stars_label = f"{service.stars_price} ⭐" if service.stars_price else "⚠️ Stars narxi yo‘q"
        lines.append(f"\n#{service.id} · {service.name} · {stars_label} · {'🟢 Faol' if service.is_active else '⚪ O‘chiq'}")
    rows = []
    for service in services:
        rows.append([
            InlineKeyboardButton(text=f"⭐ #{service.id} Stars narxi", callback_data=f"serviceeditstars:{service.id}", style="primary"),
            InlineKeyboardButton(
                text="🟢 Faol" if service.is_active else "⚪ O‘chiq",
                callback_data=f"servicetoggle:{service.id}", style="success" if service.is_active else "danger",
            ),
        ])
    rows.append([InlineKeyboardButton(text="➕ Xizmat qo‘shish", callback_data="service:new", style="success")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("\n".join(lines) if services else "Xizmatlar hali yaratilmagan.", reply_markup=markup)


@router.callback_query(F.data.startswith("servicetoggle:"))
async def service_toggle_callback(call: CallbackQuery, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "catalog.manage"):
        await _deny(call)
        return
    service_id = int(call.data.split(":", 1)[1])
    async with session_scope() as session:
        service = await session.scalar(select(Service).where(Service.id == service_id))
        if not service:
            await call.answer("Xizmat topilmadi", show_alert=True)
            return
        updated = await set_service_active(
            session, service_id=service_id, is_active=not service.is_active,
            admin_telegram_id=call.from_user.id,
        )
        status = "faollashtirildi" if updated.is_active else "o‘chirildi"
    await call.answer(f"Xizmat {status}")
    await call.message.answer(f"✅ {updated.name}: {status}.")


@router.callback_query(F.data.startswith("serviceedit:"))
async def service_edit_start(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "catalog.manage"):
        await _deny(call)
        return
    service_id = int(call.data.split(":", 1)[1])
    async with session_scope() as session:
        service = await session.get(Service, service_id)
    if not service:
        await call.answer("Xizmat topilmadi", show_alert=True)
        return
    await state.update_data(edit_service_id=service_id)
    await state.set_state(AdminEditServiceStates.price)
    await call.message.answer(
        f"✏️ {service.name}\nJoriy narx: {format_money(service.price_minor, service.currency, settings.money_scale)}\nYangi narxni kiriting:"
    )
    await call.answer()


@router.message(AdminEditServiceStates.price)
async def service_edit_price(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        price_minor = parse_amount(message.text or "", settings.money_scale)
        data = await state.get_data()
        async with session_scope() as session:
            service = await update_service_price(
                session, service_id=int(data["edit_service_id"]), price_minor=price_minor,
                admin_telegram_id=message.from_user.id,
            )
        await state.clear()
        await message.answer(
            f"✅ {service.name} narxi yangilandi: "
            f"{format_money(service.price_minor, service.currency, settings.money_scale)}",
            reply_markup=ADMIN_MENU,
        )
    except (ValueError, KeyError) as exc:
        await message.answer(f"⚠️ {exc or 'Narx noto‘g‘ri.'}")


@router.callback_query(F.data.startswith("serviceeditstars:"))
async def service_edit_stars_start(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "catalog.manage"):
        await _deny(call)
        return
    try:
        service_id = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer("Xizmat ID noto‘g‘ri.", show_alert=True)
        return
    async with session_scope() as session:
        service = await session.get(Service, service_id)
    if not service:
        await call.answer("Xizmat topilmadi.", show_alert=True)
        return
    await state.update_data(edit_service_id=service_id)
    await state.set_state(AdminEditServiceStates.stars_price)
    await call.message.answer(
        f"⭐ {service.name}\nJoriy narx: {service.stars_price or 'kiritilmagan'} Stars\n"
        "Yangi Stars narxini 1–100000 oralig‘ida kiriting:",
        reply_markup=cancel_keyboard(),
    )
    await call.answer()


@router.message(AdminEditServiceStates.stars_price)
async def service_edit_stars_price(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "catalog.manage"):
        await state.clear()
        await _deny(message)
        return
    try:
        stars_price = int((message.text or "").strip())
        data = await state.get_data()
        async with session_scope() as session:
            service = await update_service_stars_price(
                session, service_id=int(data["edit_service_id"]), stars_price=stars_price,
                admin_telegram_id=message.from_user.id,
            )
        await state.clear()
        await message.answer(
            f"✅ {service.name} Stars narxi {service.stars_price} ⭐ qilib saqlandi.\n"
            "Xizmat endi foydalanuvchi katalogida ko‘rinadi.", reply_markup=ADMIN_MENU,
        )
    except (ValueError, KeyError) as exc:
        await message.answer(f"⚠️ {exc}")


@router.message(Command("setstars"))
async def set_stars_price_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "catalog.manage"):
        await _deny(message)
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Foydalanish: /setstars SERVICE_ID STARS_AMOUNT")
        return
    try:
        async with session_scope() as session:
            service = await update_service_stars_price(
                session, service_id=int(parts[1]), stars_price=int(parts[2]),
                admin_telegram_id=message.from_user.id,
            )
        await message.answer(f"✅ {service.name}: {service.stars_price} ⭐")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")


@router.callback_query(F.data == "service:new")
async def service_new(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "catalog.manage"):
        await _deny(call)
        return
    async with session_scope() as session:
        countries = (await session.scalars(select(Country).where(Country.is_active.is_(True)).order_by(Country.name))).all()
    if not countries:
        await call.answer("Avval davlat yarating.", show_alert=True)
        return
    await call.message.answer("Xizmat qaysi davlatga tegishli?", reply_markup=choose_country_keyboard(countries))
    await call.answer()


@router.callback_query(F.data.startswith("admincountry:"))
async def service_choose_country(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "catalog.manage"):
        await _deny(call)
        return
    country_id = int(call.data.split(":")[1])
    await state.update_data(country_id=country_id)
    await state.set_state(AdminServiceStates.name)
    await call.message.answer("Xizmat nomini kiriting:")
    await call.answer()


@router.message(AdminServiceStates.name)
async def service_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 160:
        await message.answer("Nom 1–160 belgidan iborat bo‘lsin.")
        return
    await state.update_data(service_name=name)
    await state.set_state(AdminServiceStates.description)
    await message.answer("Xizmat tavsifini kiriting:")


@router.message(AdminServiceStates.description)
async def service_description(message: Message, state: FSMContext) -> None:
    await state.update_data(service_description=(message.text or "")[:1000])
    await state.set_state(AdminServiceStates.price)
    await message.answer("Narxni kiriting (UZS, masalan 25000):")


@router.message(AdminServiceStates.price)
async def service_price(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        price = parse_amount(message.text or "", settings.money_scale)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(price_minor=price)
    await state.set_state(AdminServiceStates.stock)
    await message.answer("Boshlang‘ich zaxira sonini kiriting (0 ham mumkin):")


@router.message(AdminServiceStates.stock)
async def service_stock(message: Message, state: FSMContext) -> None:
    try:
        stock = int((message.text or "").strip())
        if stock < 0 or stock > 1_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Zaxirani 0 dan katta butun son sifatida kiriting.")
        return
    await state.update_data(initial_stock=stock)
    await state.set_state(AdminServiceStates.stars_price)
    await message.answer("Telegram Stars narxini 1–100000 oralig‘idagi butun son bilan kiriting:")


@router.message(AdminServiceStates.stars_price)
async def service_stars_price(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        stars_price = int((message.text or "").strip())
        if stars_price < 1 or stars_price > 100000:
            raise ValueError
    except ValueError:
        await message.answer("Stars narxini 1–100000 oralig‘idagi butun son bilan kiriting.")
        return
    data = await state.get_data()
    try:
        async with session_scope() as session:
            service = await create_service(
                session, country_id=data["country_id"], name=data["service_name"],
                description=data["service_description"], price_minor=data["price_minor"],
                currency=settings.default_currency, initial_stock=data["initial_stock"],
                stars_price=stars_price, is_digital=True, admin_telegram_id=message.from_user.id,
            )
        await state.clear()
        await message.answer(
            f"╭━━ ✅ XIZMAT YARATILDI ━━╮\n\n"
            f"🧩 {service.name}\n🆔 ID: {service.id}\n⭐ Narx: {service.stars_price} Stars\n"
            f"📦 Boshlang‘ich zaxira: {data['initial_stock']} dona\n\n"
            "Xizmat Stars narxi bilan saqlandi va mavjud zaxira bo‘lsa katalogda ko‘rinadi.",
            reply_markup=ADMIN_MENU,
        )
    except Exception as exc:
        await message.answer(f"⚠️ Xizmat yaratilmadi: {type(exc).__name__}: {str(exc)[:250]}")


@router.message(F.text == "📦 Zaxira")
async def inventory_admin(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "inventory.manage"):
        await _deny(message)
        return
    async with session_scope() as session:
        services = (await session.scalars(select(Service).order_by(Service.name))).all()
    await message.answer(
        "📦 ZAXIRA BOSHQARUVI\n\n"
        "Quyidagi tugma orqali xizmatning umumiy AVAILABLE sonini o‘zgartiring.\n"
        "Alohida resurs/raqamni shifrlab qo‘shish: /inventory_add SERVICE_ID RESURS\n"
        "Ko‘rish: /inventory_list SERVICE_ID\n"
        "CSV import: /inventory_import SERVICE_ID\n"
        "Masked CSV eksport: /inventory_export SERVICE_ID\n"
        "Sotuvdan chiqarish: /inventory_retire ITEM_ID\n\n"
        "Individual resurs uchun PHONE_ENCRYPTION_KEY majburiy.",
        reply_markup=admin_service_keyboard(services),
    )


@router.callback_query(F.data.startswith("adminstock:"))
async def inventory_choose(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "inventory.manage"):
        await _deny(call)
        return
    service_id = int(call.data.split(":")[1])
    await state.update_data(stock_service_id=service_id)
    await state.set_state(AdminStockStates.quantity)
    await call.message.answer("Yangi AVAILABLE zaxira sonini kiriting. RESERVED va SOLD miqdorlari saqlanadi:")
    await call.answer()


@router.message(AdminStockStates.quantity)
async def inventory_set(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        quantity = int((message.text or "").strip())
        if quantity < 0 or quantity > 1_000_000:
            raise ValueError
        data = await state.get_data()
        async with session_scope() as session:
            inventory = await set_available_stock(session, service_id=data["stock_service_id"], available_quantity=quantity, admin_telegram_id=message.from_user.id)
        await state.clear()
        await message.answer(f"✅ Zaxira yangilandi. Available: {inventory.available_quantity} · Reserved: {inventory.reserved_quantity} · Sold: {inventory.sold_quantity}", reply_markup=ADMIN_MENU)
    except (ValueError, KeyError) as exc:
        await message.answer(f"⚠️ {exc or 'Butun son kiriting.'}")


@router.message(Command("inventory_add"))
async def inventory_item_add_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "inventory.manage"):
        await _deny(message)
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Foydalanish: /inventory_add SERVICE_ID RESURS\nMisol: /inventory_add 12 +998901234567")
        return
    encryption_key = settings.phone_encryption_key.get_secret_value()
    if not encryption_key:
        await message.answer("🔐 Individual raqam qo‘shish uchun PHONE_ENCRYPTION_KEY sozlang. Kalitsiz resurs saqlanmaydi.")
        return
    try:
        ciphertext, fingerprint, mask = encrypt_resource(parts[2], encryption_key)
        service_id = int(parts[1])
        async with session_scope() as session:
            inventory = await session.scalar(select(Inventory).where(Inventory.service_id == service_id).with_for_update())
            service = await session.get(Service, service_id)
            if not service or not inventory:
                raise ValueError("Xizmat yoki uning zaxira yozuvi topilmadi.")
            tracked_count = int(await session.scalar(select(func.count(InventoryItem.id)).where(
                InventoryItem.service_id == service_id
            )) or 0)
            if tracked_count == 0 and inventory.available_quantity > 0:
                raise ValueError(
                    "Bu xizmatda umumiy zaxira bor. Individual raqamlar rejimiga o‘tish uchun "
                    "avval AVAILABLE zaxirani 0 qiling. RESERVED/SOLD tarixi saqlanadi."
                )
            duplicate = await session.scalar(select(InventoryItem.id).where(
                InventoryItem.service_id == service_id, InventoryItem.resource_fingerprint == fingerprint
            ))
            if duplicate:
                raise ValueError("Bu resurs ushbu xizmat zaxirasida avvaldan mavjud.")
            item = InventoryItem(
                service_id=service_id, resource_ciphertext=ciphertext,
                resource_fingerprint=fingerprint, resource_mask=mask,
                status="available", created_by=message.from_user.id,
            )
            session.add(item)
            inventory.available_quantity += 1
            inventory.total_quantity += 1
            session.add(InventoryMovement(
                inventory_id=inventory.id, movement_type="item_added", quantity=1,
                admin_telegram_id=message.from_user.id, note=f"Individual resurs #{item.id} qo‘shildi",
            ))
            await audit(session, message.from_user.id, "inventory.item_added", "service", service_id, {"mask": mask})
        await message.answer(f"✅ Individual resurs zaxiraga qo‘shildi. Xizmat: {service_id} · Resurs: {mask}")
    except Exception as exc:
        await message.answer(f"⚠️ Resurs qo‘shilmadi: {exc}")


@router.message(Command("inventory_list"))
async def inventory_item_list_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "inventory.manage"):
        await _deny(message)
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Foydalanish: /inventory_list SERVICE_ID")
        return
    async with session_scope() as session:
        items = (await session.scalars(select(InventoryItem).where(
            InventoryItem.service_id == int(parts[1])
        ).order_by(InventoryItem.id.desc()).limit(50))).all()
    if not items:
        await message.answer("Bu xizmat uchun individual resurslar topilmadi.")
        return
    lines = [f"📱 XIZMAT #{parts[1]} — INDIVIDUAL ZAXIRA"]
    for item in items:
        lines.append(f"#{item.id} · {item.resource_mask} · {item.status}")
    lines.append("\nOlib tashlash: /inventory_retire ITEM_ID (faqat available resurslar)")
    await message.answer("\n".join(lines))


@router.message(Command("inventory_retire"))
async def inventory_item_retire_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "inventory.manage"):
        await _deny(message)
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Foydalanish: /inventory_retire ITEM_ID")
        return
    async with session_scope() as session:
        item = await session.scalar(select(InventoryItem).where(InventoryItem.id == int(parts[1])).with_for_update())
        if not item or item.status != "available":
            await message.answer("Faqat mavjud (available) resursni sotuvdan chiqarish mumkin.")
            return
        inventory = await session.scalar(select(Inventory).where(Inventory.service_id == item.service_id).with_for_update())
        if not inventory or inventory.available_quantity < 1:
            await message.answer("Zaxira balansi nomuvofiq. Operator tekshiruvi kerak.")
            return
        item.status = "retired"
        inventory.available_quantity -= 1
        inventory.total_quantity -= 1
        session.add(InventoryMovement(
            inventory_id=inventory.id, movement_type="item_retired", quantity=-1,
            admin_telegram_id=message.from_user.id, note=f"Individual resurs #{item.id} sotuvdan chiqarildi",
        ))
        await audit(session, message.from_user.id, "inventory.item_retired", "inventory_item", item.id)
    await message.answer("✅ Resurs sotuvdan chiqarildi.")


@router.message(Command("inventory_import"))
async def inventory_import_start(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "inventory.manage"):
        await _deny(message)
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Foydalanish: /inventory_import SERVICE_ID\nCSV faylda har qatorda bitta resurs qiymati bo‘lsin.")
        return
    if not settings.phone_encryption_key.get_secret_value():
        await message.answer("🔐 Import uchun PHONE_ENCRYPTION_KEY sozlanishi shart.")
        return
    await state.update_data(inventory_import_service_id=int(parts[1]))
    await state.set_state(AdminInventoryImportStates.file)
    await message.answer("CSV fayl yuboring (maksimal 1 MB; birinchi qatorda ixtiyoriy `resource` sarlavhasi bo‘lishi mumkin).")


@router.message(AdminInventoryImportStates.file, F.document)
async def inventory_import_file(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "inventory.manage"):
        await state.clear()
        await _deny(message)
        return
    document = message.document
    if not document or (document.file_size or 0) > 1_000_000:
        await message.answer("CSV fayl 1 MB dan kichik bo‘lishi kerak.")
        return
    if document.file_name and not document.file_name.lower().endswith(".csv"):
        await message.answer("Faqat .csv fayl yuboring.")
        return
    try:
        telegram_file = await message.bot.get_file(document.file_id)
        buffer = io.BytesIO()
        await message.bot.download_file(telegram_file.file_path, destination=buffer)
        content = buffer.getvalue().decode("utf-8-sig")
        parsed = list(csv.reader(io.StringIO(content)))
        values: list[str] = []
        for index, row in enumerate(parsed):
            if not row or not row[0].strip():
                continue
            candidate = row[0].strip()
            if index == 0 and candidate.casefold() in {"resource", "raqam", "number", "resurs"}:
                continue
            values.append(candidate)
        if not values or len(values) > 500:
            raise ValueError("CSV ichida 1–500 ta resurs bo‘lishi kerak.")
        encryption_key = settings.phone_encryption_key.get_secret_value()
        prepared = [encrypt_resource(value, encryption_key) for value in values]
        fingerprints = [item[1] for item in prepared]
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("CSV fayl ichida takroriy resurslar bor.")
        data = await state.get_data()
        service_id = int(data["inventory_import_service_id"])
        async with session_scope() as session:
            inventory = await session.scalar(select(Inventory).where(Inventory.service_id == service_id).with_for_update())
            service = await session.get(Service, service_id)
            if not service or not inventory:
                raise ValueError("Xizmat yoki zaxira yozuvi topilmadi.")
            tracked_count = int(await session.scalar(select(func.count(InventoryItem.id)).where(InventoryItem.service_id == service_id)) or 0)
            if tracked_count == 0 and inventory.available_quantity > 0:
                raise ValueError("Individual raqamlar rejimiga o‘tishdan oldin umumiy AVAILABLE zaxirani 0 qiling.")
            existing = set((await session.scalars(select(InventoryItem.resource_fingerprint).where(
                InventoryItem.service_id == service_id, InventoryItem.resource_fingerprint.in_(fingerprints)
            ))).all())
            if existing:
                raise ValueError(f"Bazadagi mavjud resurslar takrorlandi: {len(existing)} ta. Hech narsa import qilinmadi.")
            for ciphertext, fingerprint, mask in prepared:
                session.add(InventoryItem(
                    service_id=service_id, resource_ciphertext=ciphertext,
                    resource_fingerprint=fingerprint, resource_mask=mask,
                    status="available", created_by=message.from_user.id,
                ))
            inventory.available_quantity += len(prepared)
            inventory.total_quantity += len(prepared)
            session.add(InventoryMovement(
                inventory_id=inventory.id, movement_type="csv_import", quantity=len(prepared),
                admin_telegram_id=message.from_user.id, note="Individual resurslar CSV orqali import qilindi",
            ))
            await audit(session, message.from_user.id, "inventory.csv_imported", "service", service_id, {"count": len(prepared)})
        await state.clear()
        await message.answer(f"✅ {len(prepared)} ta individual resurs shifrlanib import qilindi.", reply_markup=ADMIN_MENU)
    except Exception as exc:
        await message.answer(f"⚠️ Import bajarilmadi: {exc}")


@router.message(Command("inventory_export"))
async def inventory_export_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "inventory.manage"):
        await _deny(message)
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Foydalanish: /inventory_export SERVICE_ID")
        return
    async with session_scope() as session:
        items = (await session.scalars(select(InventoryItem).where(
            InventoryItem.service_id == int(parts[1])
        ).order_by(InventoryItem.id))).all()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["item_id", "service_id", "masked_resource", "status", "created_at"])
    for item in items:
        writer.writerow([item.id, item.service_id, item.resource_mask, item.status, item.created_at.isoformat()])
    data = output.getvalue().encode("utf-8-sig")
    await message.answer_document(
        BufferedInputFile(data, filename=f"inventory_service_{parts[1]}.csv"),
        caption="CSV eksport. Maxfiy resurs qiymatlari xavfsizlik uchun eksport qilinmaydi.",
    )


@router.message(F.text == "⭐ Stars to‘lovlari")
async def payments_admin(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "payments.review"):
        await _deny(message)
        return
    async with session_scope() as session:
        payments = (await session.scalars(
            select(Payment).where(Payment.provider == "telegram_stars")
            .order_by(desc(Payment.created_at)).limit(25)
        )).all()
        orders = {p.order_id: await session.get(Order, p.order_id) for p in payments if p.order_id}
        users = {p.id: await session.get(User, p.user_id) for p in payments}
    if not payments:
        await message.answer("⭐ Hozircha Stars tranzaksiyalari yo‘q.", reply_markup=ADMIN_MENU)
        return
    for payment in payments:
        user = users.get(payment.id)
        order = orders.get(payment.order_id)
        caption = (
            f"⭐ Stars to‘lov #{payment.id}\n"
            f"💰 {payment.amount_minor} XTR · {payment.status}\n"
            f"👤 User: {user.telegram_id if user else '—'}\n"
            f"📦 Buyurtma: {order.order_number if order else '—'}\n"
            f"🔖 Charge: {payment.external_id or '—'}"
        )
        markup = None
        if order and order.status in {"paid", "processing"}:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="↩️ Stars refund + bekor qilish", callback_data=f"orderreject:{order.id}", style="danger")
            ]])
        await message.answer(caption, reply_markup=markup)


@router.message(F.text == "👥 Foydalanuvchilar")
async def users_admin(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "users.manage"):
        await _deny(message)
        return
    await state.set_state(AdminSearchStates.query)
    await message.answer("Foydalanuvchi Telegram ID yoki username'ini kiriting:")


@router.message(AdminSearchStates.query)
async def user_search(message: Message, state: FSMContext, settings: Settings) -> None:
    query = (message.text or "").strip().lstrip("@")
    async with session_scope() as session:
        stmt = select(User)
        if query.isdigit():
            stmt = stmt.where(User.telegram_id == int(query))
        else:
            stmt = stmt.where(
                or_(func.lower(User.username) == query.lower(), User.full_name.ilike(f"%{query}%"))
            )
        user = await session.scalar(stmt)
        if not user:
            await message.answer("Foydalanuvchi topilmadi. Qayta kiriting:")
            return
        orders = int(await session.scalar(select(func.count(Order.id)).where(Order.user_id == user.id)) or 0)
        note_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Buyurtmalar tarixi", callback_data=f"userorders:{user.id}", style="primary")],
            [InlineKeyboardButton(text="💳 To‘lovlar tarixi", callback_data=f"userpayments:{user.id}", style="primary")],
            [InlineKeyboardButton(text="🔒 Bloklash" if not user.is_blocked else "🔓 Blokdan chiqarish", callback_data=f"userblock:{user.id}", style="danger" if not user.is_blocked else "success")],
        ])
        text = (
            f"╭━━ 👥 MIJOZ ━━╮\n\n"
            f"🆔 {user.telegram_id}\n👤 {user.full_name}\n"
            f"🔗 @{user.username or '—'}\n"
            f"🎁 Ichki bonus: {user.referral_bonus_stars or 0} ⭐\n"
            f"📦 Buyurtmalar: {orders}\n"
            f"🚦 Holat: {'Bloklangan' if user.is_blocked else 'Faol'}"
        )
    await state.clear()
    await message.answer(text, reply_markup=note_markup)


@router.callback_query(F.data.startswith("userorders:"))
async def user_orders_history(call: CallbackQuery, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "users.manage"):
        await _deny(call)
        return
    user_id = int(call.data.split(":")[1])
    async with session_scope() as session:
        rows = (await session.execute(
            select(Order, Service).join(Service, Service.id == Order.service_id)
            .where(Order.user_id == user_id).order_by(Order.created_at.desc()).limit(12)
        )).all()
    if not rows:
        await call.answer("Buyurtmalar tarixi bo‘sh.", show_alert=True)
        return
    lines = ["📦 MIJOZ BUYURTMALARI"]
    for order, service in rows:
        lines.append(f"\n#{order.order_number} · {service.name}\n{order.status} · {format_money(order.total_minor, order.currency, settings.money_scale)}")
    await call.message.answer("\n".join(lines))
    await call.answer()


@router.callback_query(F.data.startswith("userpayments:"))
async def user_payments_history(call: CallbackQuery, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "users.manage"):
        await _deny(call)
        return
    user_id = int(call.data.split(":")[1])
    async with session_scope() as session:
        payments = (await session.scalars(
            select(Payment).where(Payment.user_id == user_id).order_by(Payment.created_at.desc()).limit(12)
        )).all()
    if not payments:
        await call.answer("To‘lovlar tarixi bo‘sh.", show_alert=True)
        return
    lines = ["💳 MIJOZ TO‘LOVLARI"]
    for payment in payments:
        lines.append(f"\n#{payment.id} · {payment.provider}\n{payment.status} · {payment.amount_minor} {payment.currency}")
    await call.message.answer("\n".join(lines))
    await call.answer()


@router.callback_query(F.data.startswith("userblock:"))
async def user_block_callback(call: CallbackQuery, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "users.manage"):
        await _deny(call)
        return
    user_id = int(call.data.split(":")[1])
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
        if not user:
            await call.answer("Foydalanuvchi topilmadi", show_alert=True)
            return
        user.is_blocked = not user.is_blocked
        status = "bloklandi" if user.is_blocked else "blokdan chiqarildi"
        await audit(
            session, call.from_user.id, "user.block_toggled", "user", user.id,
            {"is_blocked": user.is_blocked},
        )
    await call.message.edit_text(call.message.text + f"\n\n✅ Foydalanuvchi {status}.")
    await call.answer(status)


@router.message(F.text == "🎟 Promo yaratish")
async def promo_create_start(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "catalog.manage"):
        await _deny(message)
        return
    await state.set_state(AdminPromoStates.code)
    await message.answer("Yangi promo-kod nomini kiriting:")


@router.message(AdminPromoStates.code)
async def promo_create_code(message: Message, state: FSMContext) -> None:
    await state.update_data(promo_code=message.text.strip().upper())
    await state.set_state(AdminPromoStates.discount_type)
    await message.answer("Chegirma turi: percent yoki fixed deb yozing:")


@router.message(AdminPromoStates.discount_type)
async def promo_create_type(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip().lower()
    if value not in {"percent", "fixed"}:
        await message.answer("Faqat percent yoki fixed:")
        return
    await state.update_data(discount_type=value)
    await state.set_state(AdminPromoStates.discount_value)
    await message.answer("Chegirma qiymatini kiriting (foiz yoki UZS summa):")


@router.message(AdminPromoStates.discount_value)
async def promo_create_value(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        data = await state.get_data()
        raw = (message.text or "").strip()
        value = int(raw) if data["discount_type"] == "percent" else parse_amount(raw, settings.money_scale)
        if value <= 0:
            raise ValueError("Qiymat noldan katta bo‘lsin.")
        await state.update_data(discount_value=value)
        await state.set_state(AdminPromoStates.max_uses)
        await message.answer("Umumiy foydalanish limiti (0 = cheksiz):")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")


@router.message(AdminPromoStates.max_uses)
async def promo_create_max(message: Message, state: FSMContext) -> None:
    try:
        max_uses = int((message.text or "").strip())
        if max_uses < 0:
            raise ValueError
        await state.update_data(max_uses=max_uses or None)
        await state.set_state(AdminPromoStates.expiry_days)
        await message.answer("Amal qilish muddati kunlarda (0 = cheksiz):")
    except ValueError:
        await message.answer("Butun son kiriting.")


@router.message(AdminPromoStates.expiry_days)
async def promo_create_expiry(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        days = int((message.text or "").strip())
        if days < 0 or days > 3650:
            raise ValueError
        data = await state.get_data()
        async with session_scope() as session:
            promo = await create_promo(
                session, code=data["promo_code"], discount_type=data["discount_type"],
                discount_value=data["discount_value"], max_uses=data["max_uses"],
                per_user_limit=1, min_order_minor=0,
                expires_in_days=days or None, created_by=message.from_user.id,
            )
        await state.clear()
        await message.answer(f"✅ Promo yaratildi: {promo.code}\nTuri: {promo.discount_type}\nQiymati: {promo.discount_value}", reply_markup=ADMIN_MENU)
    except Exception as exc:
        await message.answer(f"⚠️ Promo yaratilmadi: {exc}")


@router.message(F.text == "📢 Xabar yuborish")
async def broadcast_start(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "broadcast.create"):
        await _deny(message)
        return
    await state.set_state(AdminBroadcastStates.message)
    await message.answer("📢 Broadcast uchun matn, rasm, video yoki hujjat yuboring. Bot xabarni asl ko‘rinishida nusxalaydi.")


async def _finish_broadcast(message: Message, state: FSMContext, *, button_text: str | None = None, button_url: str | None = None) -> None:
    data = await state.get_data()
    try:
        async with session_scope() as session:
            campaign = await queue_broadcast(
                session, message=data["broadcast_caption"], created_by=message.from_user.id,
                source_chat_id=data["source_chat_id"], source_message_id=data["source_message_id"],
                button_text=button_text, button_url=button_url,
            )
        await state.clear()
        await message.answer(
            f"✅ Broadcast navbatga qo‘yildi.\nCampaign: #{campaign.id}\nQabul qiluvchilar: {campaign.total_count}",
            reply_markup=ADMIN_MENU,
        )
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")


@router.message(AdminBroadcastStates.message)
async def broadcast_receive_content(message: Message, state: FSMContext) -> None:
    allowed_types = {"text", "photo", "video", "document", "animation"}
    if message.content_type not in allowed_types:
        await message.answer("Matn, rasm, video yoki hujjat yuboring.")
        return
    text = message.text or message.caption or f"[{message.content_type}]"
    await state.update_data(
        broadcast_caption=text[:4000], source_chat_id=message.chat.id,
        source_message_id=message.message_id,
    )
    await state.set_state(AdminBroadcastStates.button_text)
    await message.answer("Inline URL tugmasi kerak bo‘lsa uning matnini kiriting. Tugmasiz yuborish uchun - deb yozing:")


@router.message(AdminBroadcastStates.button_text)
async def broadcast_button_text(message: Message, state: FSMContext) -> None:
    value = (message.text or "-").strip()
    if value == "-":
        await _finish_broadcast(message, state)
        return
    if not value or len(value) > 64:
        await message.answer("Tugma matni 1–64 belgidan iborat bo‘lsin yoki - deb yozing.")
        return
    await state.update_data(button_text=value)
    await state.set_state(AdminBroadcastStates.button_url)
    await message.answer("Tugma uchun https:// yoki http:// URL yuboring:")


@router.message(AdminBroadcastStates.button_url)
async def broadcast_button_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url.startswith(("https://", "http://")) or len(url) > 500:
        await message.answer("To‘g‘ri http:// yoki https:// URL yuboring.")
        return
    data = await state.get_data()
    await _finish_broadcast(message, state, button_text=data.get("button_text"), button_url=url)


@router.message(F.text == "🛡 Admin rollari")
async def roles_handler(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "roles.manage"):
        await _deny(message)
        return
    async with session_scope() as session:
        roles = (await session.scalars(select(AdminRole).order_by(AdminRole.id))).all()
    lines = ["╭━━ 🛡 ADMIN ROLLARI ━━╮"]
    descriptions = {
        "Super Admin": "Barcha huquqlar", "Manager": "Xizmatlar, zaxira, statistika",
        "Operator": "Buyurtmalar", "Finance": "To‘lovlar va moliya", "Support": "Mijozlar va murojaatlar",
        "Order Manager": "Buyurtmalarni boshqarish", "Finance Admin": "To‘lovlar va hisobotlar",
        "Support Admin": "Mijozlar va support", "Catalogue Manager": "Davlatlar, xizmatlar va zaxira",
    }
    lines += [f"\n🔐 {role.name}\n{descriptions.get(role.name, role.description)}" for role in roles]
    lines.append(
        "\nRol biriktirish: /set_role TELEGRAM_ID ROLE_NAME"
        "\nMisol: /set_role 123456789 'Catalogue Manager'"
        "\nSuper Admin ID'lari ADMIN_IDS orqali belgilanadi."
    )
    await message.answer("\n".join(lines), reply_markup=ADMIN_MENU)


@router.message(Command("reply_ticket"))
async def reply_ticket_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "support.manage"):
        await _deny(message)
        return
    from app.services.support_service import reply_ticket
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Foydalanish: /reply_ticket TICKET_ID javob matni")
        return
    try:
        async with session_scope() as session:
            await reply_ticket(session, ticket_id=int(parts[1]), admin_telegram_id=message.from_user.id, message=parts[2])
        await message.answer("✅ Javob foydalanuvchiga yuborish navbatiga qo‘yildi.")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")


@router.message(Command("set_role"))
async def set_role_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "roles.manage"):
        await _deny(message)
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Foydalanish: /set_role TELEGRAM_ID 'Manager'|Operator|Finance|Support")
        return
    role_name = parts[2].strip().strip("'\"")
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.telegram_id == int(parts[1])))
        role = await session.scalar(select(AdminRole).where(func.lower(AdminRole.name) == role_name.lower()))
        if not user or not role:
            await message.answer("Foydalanuvchi yoki rol topilmadi. /admin rollarini tekshiring.")
            return
        admin = await session.scalar(select(AdminUser).where(AdminUser.user_id == user.id))
        if admin is None:
            admin = AdminUser(user_id=user.id, is_active=True)
            session.add(admin)
            await session.flush()
        else:
            admin.is_active = True
        await session.execute(delete(AdminUserRole).where(AdminUserRole.admin_id == admin.id))
        session.add(AdminUserRole(admin_id=admin.id, role_id=role.id))
        await audit(session, message.from_user.id, "admin.role_assigned", "admin_user", admin.id,
                    {"target_telegram_id": int(parts[1]), "role": role.name})
    await message.answer(f"✅ {parts[1]} foydalanuvchisiga {role.name} roli biriktirildi.")


@router.message(Command("note"))
async def add_user_note(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "users.manage"):
        await _deny(message)
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Foydalanish: /note TELEGRAM_ID ichki izoh")
        return
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.telegram_id == int(parts[1])))
        if not user:
            await message.answer("Foydalanuvchi topilmadi.")
            return
        session.add(UserNote(user_id=user.id, admin_telegram_id=message.from_user.id, note=parts[2][:1000]))
        await audit(session, message.from_user.id, "crm.note_added", "user", user.id)
    await message.answer("✅ Ichki CRM izohi saqlandi.")




@router.message(Command("outbox_status"))
async def outbox_status_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "broadcast.create") and not await _allowed(message.from_user.id, settings, "support.manage"):
        await _deny(message)
        return
    async with session_scope() as session:
        rows = (await session.execute(
            select(OutboxEvent.status, func.count(OutboxEvent.id)).group_by(OutboxEvent.status)
        )).all()
        recent_errors = (await session.scalars(
            select(OutboxEvent).where(OutboxEvent.status == "dead").order_by(OutboxEvent.id.desc()).limit(5)
        )).all()
    counts = {status: int(count) for status, count in rows}
    text = (
        "🧰 XABAR NAVBATI\n\n"
        f"⏳ Navbatda: {counts.get('pending', 0)}\n"
        f"⚙️ Ishlanmoqda: {counts.get('processing', 0)}\n"
        f"✅ Yuborilgan: {counts.get('sent', 0)}\n"
        f"❌ Xato bilan to‘xtagan: {counts.get('dead', 0)}"
    )
    if recent_errors:
        text += "\n\nSo‘nggi xatolar:"
        for event in recent_errors:
            text += f"\n#{event.id} {event.event_type}: {(event.last_error or 'xato tafsiloti yo‘q')[:160]}"
    await message.answer(text, reply_markup=ADMIN_MENU)


@router.message(Command("broadcast_status"))
async def broadcast_status_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "broadcast.create"):
        await _deny(message)
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Foydalanish: /broadcast_status CAMPAIGN_ID")
        return
    async with session_scope() as session:
        campaign = await session.get(BroadcastCampaign, int(parts[1]))
    if not campaign:
        await message.answer("Broadcast kampaniyasi topilmadi.")
        return
    await message.answer(
        f"📢 BROADCAST #{campaign.id}\n\n"
        f"Holat: {campaign.status}\n"
        f"Jami: {campaign.total_count}\n"
        f"✅ Yetkazildi: {campaign.sent_count}\n"
        f"❌ Yetkazilmadi: {campaign.failed_count}\n"
        f"⏳ Kutilmoqda: {max(0, campaign.total_count - campaign.sent_count - campaign.failed_count)}",
        reply_markup=ADMIN_MENU,
    )


@router.message(F.text == "💬 Support")
@router.message(Command("tickets"))
async def open_tickets_command(message: Message, settings: Settings) -> None:
    if not await _allowed(message.from_user.id, settings, "support.manage"):
        await _deny(message)
        return
    async with session_scope() as session:
        tickets = (await session.scalars(select(SupportTicket).where(SupportTicket.status == "open").order_by(SupportTicket.created_at).limit(20))).all()
        users = {ticket.id: await session.get(User, ticket.user_id) for ticket in tickets}
    if not tickets:
        await message.answer("📭 Ochiq support murojaatlari yo‘q.")
        return
    for ticket in tickets:
        user = users[ticket.id]
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yopish", callback_data=f"ticketclose:{ticket.id}", style="success")
        ]])
        await message.answer(
            f"📩 Ticket #{ticket.id}\nUser: {user.telegram_id if user else '—'}\n"
            f"Yaratilgan: {ticket.created_at:%d.%m.%Y %H:%M}\n"
            f"Javob: /reply_ticket {ticket.id} matn",
            reply_markup=markup,
        )


@router.callback_query(F.data.startswith("ticketclose:"))
async def close_ticket_callback(call: CallbackQuery, settings: Settings) -> None:
    if not await _allowed(call.from_user.id, settings, "support.manage"):
        await _deny(call)
        return
    from app.services.support_service import close_ticket
    ticket_id = int(call.data.split(":")[1])
    try:
        async with session_scope() as session:
            await close_ticket(session, ticket_id=ticket_id, admin_telegram_id=call.from_user.id)
        await call.message.edit_text((call.message.text or "") + "\n\n✅ Ticket yopildi.")
        await call.answer("Ticket yopildi")
    except ValueError as exc:
        await call.answer(str(exc), show_alert=True)
