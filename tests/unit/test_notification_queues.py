import asyncio

from app.database.models import BroadcastCampaign, BroadcastRecipient, OutboxEvent, SupportTicket, User
from app.services.broadcast_service import queue_broadcast
from app.services.support_service import create_ticket


class FakeScalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, users=None):
        self.users = users or []
        self.added = []
        self.next_id = 1

    async def scalars(self, _statement):
        return FakeScalars(self.users)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if hasattr(value, "id") and getattr(value, "id") is None:
                value.id = self.next_id
                self.next_id += 1



def test_support_ticket_and_admin_notifications_are_created_together():
    async def run():
        user = User(id=3, telegram_id=303, username="customer", first_name="Customer")
        session = FakeSession()
        ticket = await create_ticket(session, user=user, message="Buyurtmam bo‘yicha yordam kerak", admin_ids={101, 202})

        assert isinstance(ticket, SupportTicket)
        assert ticket.id is not None
        events = [x for x in session.added if isinstance(x, OutboxEvent)]
        assert len(events) == 2
        assert {event.payload["chat_id"] for event in events} == {101, 202}
        assert all(event.event_type == "send_message" for event in events)
        assert all(f"#{ticket.id}" in event.payload["text"] for event in events)

    asyncio.run(run())


def test_broadcast_campaign_creates_one_outbox_event_per_user():
    async def run():
        users = [
            User(id=1, telegram_id=101, first_name="One"),
            User(id=2, telegram_id=202, first_name="Two"),
        ]
        session = FakeSession(users)
        campaign = await queue_broadcast(
            session, message="Yangi e’lon", created_by=999,
            source_chat_id=999, source_message_id=77,
        )

        assert isinstance(campaign, BroadcastCampaign)
        assert campaign.id is not None
        assert campaign.total_count == 2
        recipients = [x for x in session.added if isinstance(x, BroadcastRecipient)]
        events = [x for x in session.added if isinstance(x, OutboxEvent)]
        assert len(recipients) == 2
        assert len(events) == 2
        assert all(event.event_type == "broadcast_message" for event in events)
        assert {event.payload["chat_id"] for event in events} == {101, 202}
        assert all(event.payload["campaign_id"] == campaign.id for event in events)

    asyncio.run(run())
