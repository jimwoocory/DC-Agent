from dc_engines.pet_live.integrations import pet_live_enabled


def test_pet_live_enabled_defaults_to_true(monkeypatch) -> None:
    monkeypatch.delenv("PET_LIVE_ENABLED", raising=False)

    assert pet_live_enabled() is True


def test_pet_live_enabled_accepts_false_values(monkeypatch) -> None:
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("PET_LIVE_ENABLED", value)

        assert pet_live_enabled() is False


def test_pet_live_enabled_accepts_trueish_values(monkeypatch) -> None:
    monkeypatch.setenv("PET_LIVE_ENABLED", "preview")

    assert pet_live_enabled() is True
