import asyncio
from app.config import Settings
from app.database.models import Referral, User
from app.services.user_service import register_user


class FakeRegistrationSession:
    def __init__(self, referrer: User | None):
        self.referrer = referrer
        self.scalar_calls = 0
        self.added = []
        self.flush_calls = 0

    async def scalar(self, _statement):
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return None  # the invited user is not registered yet
        return self.referrer

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_calls += 1
        for value in self.added:
            if isinstance(value, User) and value.id is None:
                value.id = 42


def test_referral_bonus_is_granted_to_new_invited_user_immediately():
    async def run():
        inviter = User(id=7, telegram_id=7007, referral_code="INVITER7", first_name="Inviter")
        session = FakeRegistrationSession(inviter)
        settings = Settings(_env_file=None, REFERRAL_BONUS_STARS=15)

        invited, created = await register_user(
            session,
            telegram_id=4242,
            username="new_user",
            first_name="New",
            last_name="User",
            referral_code="inviter7",
            settings=settings,
        )

        assert created is True
        assert invited.id == 42
        assert invited.referred_by_id == inviter.id
        assert invited.referral_bonus_stars == 15
        referral = next(item for item in session.added if isinstance(item, Referral))
        assert referral.referrer_user_id == inviter.id
        assert referral.referred_user_id == invited.id
        assert referral.status == "rewarded"
        assert referral.bonus_minor == 15

    asyncio.run(run())


def test_self_referral_is_ignored():
    async def run():
        own_user = User(id=9, telegram_id=9090, referral_code="OWNCODE", first_name="Self")
        session = FakeRegistrationSession(own_user)
        settings = Settings(_env_file=None, REFERRAL_BONUS_STARS=15)

        invited, created = await register_user(
            session,
            telegram_id=9090,
            username=None,
            first_name="Self",
            last_name="",
            referral_code="OWNCODE",
            settings=settings,
        )

        assert created is True
        assert invited.referred_by_id is None
        assert int(invited.referral_bonus_stars or 0) == 0
        assert not any(isinstance(item, Referral) for item in session.added)

    asyncio.run(run())
