from app.config import Settings


def test_old_card_and_click_environment_values_are_ignored():
    settings = Settings(
        _env_file=None,
        TELEGRAM_STARS_ENABLED=True,
        PAYMENT_MODE="ENABLED",
        PAYMENT_CARD_NUMBER="4111111111111111",
        PAYMENT_CARD_HOLDER="TEST USER",
        CLICK_ENABLED=True,
        CLICK_SECRET_KEY="must-not-be-used",
    )

    assert settings.telegram_stars_enabled is True
    assert not hasattr(settings, "payment_mode")
    assert not hasattr(settings, "payment_card_number")
    assert not hasattr(settings, "click_secret_key")


def test_stars_referral_bonus_is_configurable():
    settings = Settings(_env_file=None, REFERRAL_BONUS_STARS=25)
    assert settings.referral_bonus_stars == 25
