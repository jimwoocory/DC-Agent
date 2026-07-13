from pathlib import Path


def test_desktop_binding_secret_loads_from_keychain_without_a_default() -> None:
    source = (Path(__file__).resolve().parents[1] / "start-all.sh").read_text(
        encoding="utf-8"
    )

    assert "security find-generic-password" in source
    assert "com.dianchi.desktop.binding" in source
    assert "export DESKTOP_BINDING_SECRET" in source
    assert (
        "DESKTOP_BINDING_SECRET="
        not in source.split('if [ -z "${DESKTOP_BINDING_SECRET:-}" ]', 1)[0]
    )
