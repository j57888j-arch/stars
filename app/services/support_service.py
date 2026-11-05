from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SupportMessage, SupportTicket, User
from app.services.common import audit, enqueue_message


async def create_ticket(session: AsyncSession, *, user: User, message: str, admin_ids: set[int]) -> SupportTicket:
    text = message.strip()
    if not text or len(text) > 4000:
        raise ValueError("Murojaat 1–4000 belgidan iborat bo‘lishi kerak.")
    ticket = SupportTicket(user_id=user.id, subject="Foydalanuvchi murojaati", status="open")
    session.add(ticket)
    await session.flush()
    session.add(SupportMessage(ticket_id=ticket.id, sender_telegram_id=user.telegram_id, message=text))
    # admin_ids is the union of bootstrap admins and ADMIN_CHAT_ID. Queue every
    # recipient inside the same transaction as the ticket so the message cannot
    # be acknowledged without a durable notification record.
    for admin_id in sorted(set(admin_ids)):
        await enqueue_message(
            session, admin_id,
            f"╭━━ 📩 YANGI SUPPORT ━━╮\n\n"
            f"🎫 Ticket: #{ticket.id}\n👤 Telegram ID: {user.telegram_id}\n"
            f"🔗 Username: @{user.username or '—'}\n\n{text[:3000]}\n\n"
            f"Javob: /reply_ticket {ticket.id} matn\nOchiq murojaatlar: /tickets",
        )
    await audit(session, user.telegram_id, "support.created", "ticket", ticket.id)
    return ticket


async def reply_ticket(session: AsyncSession, *, ticket_id: int, admin_telegram_id: int, message: str) -> SupportTicket:
    ticket = await session.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update())
    if not ticket or ticket.status != "open":
        raise ValueError("Ochiq ticket topilmadi.")
    user = await session.get(User, ticket.user_id)
    if not user:
        raise ValueError("Ticket foydalanuvchisi topilmadi.")
    session.add(SupportMessage(ticket_id=ticket.id, sender_telegram_id=admin_telegram_id, message=message[:4000]))
    await enqueue_message(session, user.telegram_id, f"💬 Support javobi · #{ticket.id}\n\n{message[:3500]}")
    ticket.assigned_admin_id = admin_telegram_id
    await audit(session, admin_telegram_id, "support.replied", "ticket", ticket.id)
    return ticket


async def close_ticket(session: AsyncSession, *, ticket_id: int, admin_telegram_id: int) -> None:
    ticket = await session.scalar(select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update())
    if not ticket or ticket.status != "open":
        raise ValueError("Ochiq ticket topilmadi.")
    ticket.status = "closed"
    ticket.closed_at = datetime.now(timezone.utc)
    await audit(session, admin_telegram_id, "support.closed", "ticket", ticket.id)
