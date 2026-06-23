from pathlib import Path


def test_nas_mount_falls_back_to_local_password_backup() -> None:
    script = Path("nas_sync/mount.sh").read_text(encoding="utf-8")

    assert "NAS_PASSWORD_BACKUP" in script
    assert "security find-generic-password" in script
    assert '[[ -r "$NAS_PASSWORD_BACKUP" ]]' in script
    assert "Keychain 不可读" in script


def test_nas_watchdog_surfaces_mount_failures_to_scheduler() -> None:
    script = Path("nas_sync/watchdog.sh").read_text(encoding="utf-8")

    assert "挂载阶段将使用备份密码兜底" in script
    assert (
        'record_failure "凭据缺失：Keychain 无密码且备份文件不可用"\n    exit 1'
        in script
    )
    assert 'record_failure "$REASON"\nexit 1' in script
