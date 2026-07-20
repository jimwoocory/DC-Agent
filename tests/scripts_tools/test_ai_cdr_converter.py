from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from harness.evaluator.kb_import_contract import load_contract, validate_contract

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts-tools" / "ai_cdr_converter" / "ai_cdr_converter.py"
SPEC = importlib.util.spec_from_file_location("ai_cdr_converter", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
converter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = converter
SPEC.loader.exec_module(converter)


class SuccessfulConverter:
    """Create deterministic conversion artifacts without desktop applications."""

    def __init__(self) -> None:
        """Track local source paths passed to the conversion engine."""
        self.sources: list[Path] = []

    def convert(self, source: Path, bridge_pdf: Path, output_cdr: Path) -> None:
        """Write representative PDF and CDR artifacts.

        Args:
            source: AI source copied into the active job directory.
            bridge_pdf: Illustrator bridge PDF destination.
            output_cdr: CorelDRAW document destination.
        """
        assert source.exists()
        self.sources.append(source)
        bridge_pdf.write_bytes(b"%PDF-1.7\ncolor-managed bridge\n")
        output_cdr.write_bytes(b"RIFF" + b"cdr-test-data" * 16)
        output_cdr.with_name(f"{output_cdr.stem}.preview.png").write_bytes(
            b"png-preview-data"
        )


class FailingConverter:
    """Raise a deterministic conversion error for quarantine tests."""

    def convert(self, source: Path, bridge_pdf: Path, output_cdr: Path) -> None:
        """Fail after leaving a diagnostic bridge artifact.

        Args:
            source: AI source copied into the active job directory.
            bridge_pdf: Illustrator bridge PDF destination.
            output_cdr: CorelDRAW document destination.

        Raises:
            RuntimeError: Always raised to exercise failure handling.
        """
        assert source.exists()
        bridge_pdf.write_bytes(b"partial bridge")
        raise RuntimeError("CorelDRAW save failed")


def test_contract_is_valid() -> None:
    contract = load_contract(
        REPO_ROOT / "harness" / "contracts" / "ai_cdr_conversion_service.json"
    )
    assert validate_contract(contract) == []


def test_initialize_workspace(tmp_path: Path) -> None:
    config_path = converter.initialize_workspace(tmp_path)

    assert config_path == tmp_path / "config.json"
    assert config_path.exists()
    for directory in converter.QUEUE_DIRECTORIES:
        assert (tmp_path / directory).is_dir()

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["illustrator_app"] == "com.adobe.illustrator"
    assert config["corel_app"] == "com.corel.coreldrawsuite.2026.coreldraw"
    assert config["local_work_root"].startswith("~/Library/Caches/")


def test_dry_run_does_not_mutate_queue(tmp_path: Path) -> None:
    converter.initialize_workspace(tmp_path)
    source = tmp_path / "inbox" / "货架.ai"
    source.write_bytes(b"illustrator source")
    config = replace(
        converter.load_config(tmp_path),
        settle_seconds=0,
        local_work_root=str(tmp_path / "mini4-work"),
    )

    class UnexpectedConverter:
        def convert(self, source: Path, bridge_pdf: Path, output_cdr: Path) -> None:
            raise AssertionError("dry-run invoked the desktop converter")

    results = converter.process_pending(
        tmp_path,
        config,
        dry_run=True,
        desktop_converter=UnexpectedConverter(),
    )

    assert [result.status for result in results] == ["planned"]
    assert source.exists()
    assert list((tmp_path / "processing").iterdir()) == []
    assert list((tmp_path / "reports").iterdir()) == []


def test_existing_queue_is_upgraded_with_new_directories(tmp_path: Path) -> None:
    converter.initialize_workspace(tmp_path)
    (tmp_path / "progress").rmdir()
    (tmp_path / "color_profiles").rmdir()
    config = replace(converter.load_config(tmp_path), settle_seconds=0)

    results = converter.process_pending(
        tmp_path,
        config,
        dry_run=True,
        desktop_converter=SuccessfulConverter(),
    )

    assert results == []
    assert (tmp_path / "progress").is_dir()
    assert (tmp_path / "color_profiles").is_dir()


def test_pending_jobs_are_processed_in_fifo_upload_order(tmp_path: Path) -> None:
    converter.initialize_workspace(tmp_path)
    inbox = tmp_path / "inbox"
    newer_name_that_sorts_first = inbox / "A-后上传.ai"
    older_name_that_sorts_last = inbox / "Z-先上传.ai"
    newer_name_that_sorts_first.write_bytes(b"newer")
    older_name_that_sorts_last.write_bytes(b"older")
    os.utime(older_name_that_sorts_last, (100, 100))
    os.utime(newer_name_that_sorts_first, (200, 200))
    config = replace(
        converter.load_config(tmp_path),
        settle_seconds=0,
        minimum_cdr_bytes=16,
        local_work_root=str(tmp_path / "mini4-work"),
    )
    engine = SuccessfulConverter()

    converter.process_pending(tmp_path, config, desktop_converter=engine)

    assert [source.name for source in engine.sources] == [
        older_name_that_sorts_last.name,
        newer_name_that_sorts_first.name,
    ]


def test_successful_job_publishes_artifacts(tmp_path: Path) -> None:
    converter.initialize_workspace(tmp_path)
    source = tmp_path / "inbox" / "高空目视牌.ai"
    source.write_bytes(b"illustrator source")
    config = replace(
        converter.load_config(tmp_path),
        settle_seconds=0,
        minimum_cdr_bytes=16,
        local_work_root=str(tmp_path / "mini4-work"),
    )
    engine = SuccessfulConverter()

    results = converter.process_pending(
        tmp_path,
        config,
        desktop_converter=engine,
    )

    assert len(results) == 1
    result = results[0]
    assert result.status == "succeeded"
    assert (tmp_path / "outbox" / "高空目视牌.cdr").exists()
    assert (tmp_path / "outbox" / "previews" / "高空目视牌.png").exists()
    assert (tmp_path / "outbox" / "originals" / source.name).exists()
    assert (tmp_path / "outbox" / "bridge_pdf" / "高空目视牌.pdf").exists()
    assert list((tmp_path / "processing").iterdir()) == []
    assert len(engine.sources) == 1
    assert engine.sources[0].is_relative_to(tmp_path / "mini4-work")
    assert list((tmp_path / "mini4-work").iterdir()) == []

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "succeeded"
    assert report["source_name"] == source.name
    assert report["output_cdr"].endswith("高空目视牌.cdr")
    assert report["preview_png"].endswith("高空目视牌.png")
    assert report["source_sha256"]
    assert report["local_work_root"] == str((tmp_path / "mini4-work").resolve())
    progress = json.loads(
        (tmp_path / "progress" / f"{result.job_id}.json").read_text(encoding="utf-8")
    )
    assert progress["status"] == "succeeded"
    assert progress["stage"] == "completed"
    assert progress["percentage"] == 100


def test_successful_job_archives_color_calibration(tmp_path: Path) -> None:
    converter.initialize_workspace(tmp_path)
    source = tmp_path / "inbox" / "企业标识.ai"
    source.write_bytes(b"illustrator source")
    sidecar = source.with_suffix(".color.json")
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "source_name": source.name,
                "mappings": [
                    {
                        "label": "企业蓝",
                        "source": {"c": 100, "m": 70, "y": 0, "k": 0},
                        "target": {"c": 100, "m": 68, "y": 0, "k": 12},
                        "apply_to": "all",
                        "tolerance": 0.1,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = replace(
        converter.load_config(tmp_path),
        settle_seconds=0,
        minimum_cdr_bytes=16,
        local_work_root=str(tmp_path / "mini4-work"),
    )

    result = converter.process_pending(
        tmp_path,
        config,
        desktop_converter=SuccessfulConverter(),
    )[0]

    assert result.status == "succeeded"
    archived_sidecar = tmp_path / "outbox" / "originals" / sidecar.name
    assert archived_sidecar.is_file()
    assert not sidecar.exists()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["color_calibration"] == {
        "sidecar": str(archived_sidecar),
        "mapping_count": 1,
        "icc_profile": None,
        "icc_sha256": None,
        "icc_mode": "registered_only",
        "audit": None,
    }


def test_failed_job_is_quarantined(tmp_path: Path) -> None:
    converter.initialize_workspace(tmp_path)
    source = tmp_path / "inbox" / "图片制作文件.ai"
    source.write_bytes(b"illustrator source")
    config = replace(
        converter.load_config(tmp_path),
        settle_seconds=0,
        local_work_root=str(tmp_path / "mini4-work"),
    )

    results = converter.process_pending(
        tmp_path,
        config,
        desktop_converter=FailingConverter(),
    )

    assert len(results) == 1
    result = results[0]
    assert result.status == "failed"
    failed_job = tmp_path / "failed" / result.job_id
    assert (failed_job / source.name).exists()
    assert (failed_job / "local-work" / "bridge.pdf").exists()
    assert list((tmp_path / "mini4-work").iterdir()) == []

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error"] == "CorelDRAW save failed"


def test_illustrator_script_preserves_color_inputs(tmp_path: Path) -> None:
    config = converter.ConverterConfig(
        outline_text=True,
        embed_linked_images=True,
    )
    script = converter.build_illustrator_jsx(
        tmp_path / "source.ai",
        tmp_path / "bridge.pdf",
        config,
    )

    assert "ColorConversion.None" in script
    assert "ColorDestination.None" in script
    assert "ColorProfile.LEAVEPROFILEUNCHANGED" in script
    assert script.count("DownsampleMethod.NODOWNSAMPLE") == 3
    assert ".embed()" in script
    assert ".createOutline()" in script
    assert "documentRef.pageItems.length === 0" in script
    assert "documentRef.visibleBounds" in script
    assert "intersectsArtboard" in script
    assert ".artboardRect = artworkBounds" in script
    assert "var bridgeDocument = app.open(bridgeFile)" in script
    assert "bridgeDocument.pageItems.length === 0" in script
    assert "SaveOptions.DONOTSAVECHANGES" in script


def test_color_calibration_validates_mapping_and_icc(tmp_path: Path) -> None:
    profile = tmp_path / "color_profiles" / "press.icc"
    profile.parent.mkdir()
    profile_bytes = bytearray(128)
    profile_bytes[36:40] = b"acsp"
    profile.write_bytes(profile_bytes)
    sidecar = tmp_path / "inbox" / "标牌.color.json"
    sidecar.parent.mkdir()
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "source_name": "标牌.ai",
                "icc_profile": "color_profiles/press.icc",
                "mappings": [
                    {
                        "label": "专色替代",
                        "source": {"c": 1, "m": 2, "y": 3, "k": 4},
                        "target": {"c": 5, "m": 6, "y": 7, "k": 8},
                        "apply_to": "fill",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    calibration = converter.load_color_calibration(sidecar, queue_root=tmp_path)

    assert calibration.source_name == "标牌.ai"
    assert calibration.icc_profile_path == profile
    assert calibration.icc_sha256 == hashlib.sha256(profile_bytes).hexdigest()
    assert calibration.mappings[0].source == (1.0, 2.0, 3.0, 4.0)
    assert calibration.mappings[0].target == (5.0, 6.0, 7.0, 8.0)
    assert calibration.mappings[0].apply_to == "fill"


def test_color_calibration_rejects_out_of_range_cmyk(tmp_path: Path) -> None:
    sidecar = tmp_path / "invalid.color.json"
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "source_name": "invalid.ai",
                "mappings": [
                    {
                        "source": {"c": 101, "m": 0, "y": 0, "k": 0},
                        "target": {"c": 0, "m": 0, "y": 0, "k": 0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        converter.load_color_calibration(sidecar)
    except ValueError as exc:
        assert "source.c must be 0 to 100" in str(exc)
    else:
        raise AssertionError("invalid CMYK calibration was accepted")


def test_illustrator_script_applies_and_audits_cmyk_mapping(tmp_path: Path) -> None:
    calibration = converter.ColorCalibration(
        version=1,
        source_name="source.ai",
        mappings=(
            converter.CMYKColorMapping(
                label="企业蓝",
                source=(100, 70, 0, 0),
                target=(100, 68, 0, 12),
                apply_to="all",
                tolerance=0.1,
            ),
        ),
        icc_profile=None,
        icc_profile_path=None,
        icc_sha256=None,
    )
    audit = tmp_path / "color-calibration-audit.json"

    script = converter.build_illustrator_jsx(
        tmp_path / "source.ai",
        tmp_path / "bridge.pdf",
        converter.ConverterConfig(),
        calibration,
        audit,
    )

    assert 'color.typename !== "CMYKColor"' in script
    assert '"source": [100, 70, 0, 0]' in script
    assert '"target": [100, 68, 0, 12]' in script
    assert "pageItem.fillColor = calibratedCMYKColor" in script
    assert "pageItem.strokeColor = calibratedCMYKColor" in script
    assert "mappingMatchCounts[requiredIndex] === 0" in script
    assert "Color calibration did not match any object" in script
    assert str(audit.resolve()) in script


def test_corel_automation_uses_internal_save_api(tmp_path: Path) -> None:
    worker = converter.build_corel_javascript(
        tmp_path / "bridge.pdf",
        tmp_path / "result.cdr",
    )
    lowered = worker.lower()

    assert "clipboard" not in lowered
    assert "pbpaste" not in lowered
    assert "pbcopy" not in lowered
    assert "keystroke outputdirectory" not in lowered
    assert "keystroke outputfilename" not in lowered
    assert "host.OpenDocument" in worker
    assert "host.CreateStructSaveAsOptions" in worker
    assert "saveOptions.Filter = 1795" in worker
    assert "saveOptions.EmbedICCProfile = true" in worker
    assert "documentRef.SaveAs" in worker
    assert str((tmp_path / "bridge.pdf").resolve()) in worker
    assert str((tmp_path / "result.cdr").resolve()) in worker
    assert hasattr(converter, "run_corel_worker_with_accessibility")


def test_corel_worker_validates_shapes_before_final_save(tmp_path: Path) -> None:
    worker = converter.build_corel_javascript(
        tmp_path / "bridge.pdf",
        tmp_path / "result.cdr",
    )

    validation_cdr = tmp_path / ".result.validating.cdr"
    preview_pdf = tmp_path / "result.preview.pdf"
    assert str(validation_cdr.resolve()) in worker
    assert str(preview_pdf.resolve()) in worker
    assert "documentRef.Pages.Count" in worker
    assert "documentRef.Pages.Item(pageIndex).Shapes.Count" in worker
    assert "PDF import contains no CorelDRAW shapes" in worker
    assert "let validationDocument = host.OpenDocument" in worker
    assert "validationDocument.Pages.Count" in worker
    assert "validationDocument.Pages.Item(pageIndex).Shapes.Count" in worker
    assert "validated CDR contains no CorelDRAW shapes" in worker
    assert "validationDocument.PublishToPDF" in worker
    assert "validationDocument.Export" not in worker
    assert "validationDocument.SaveAs" in worker


def test_corel_snapshot_uses_internal_export_api(tmp_path: Path) -> None:
    worker = converter.build_corel_snapshot_javascript(
        tmp_path / "result.cdr",
        tmp_path / "result.corel-proof.pdf",
    )
    lowered = worker.lower()

    assert "clipboard" not in lowered
    assert "host.OpenDocument" in worker
    assert "documentRef.PublishToPDF" in worker
    assert "documentRef.Export" not in worker
    assert "documentRef.Pages.Count" in worker
    assert "documentRef.Pages.Item(pageIndex).Shapes.Count" in worker
    assert "CDR snapshot source contains no shapes" in worker
    assert str((tmp_path / "result.cdr").resolve()) in worker
    assert str((tmp_path / "result.corel-proof.pdf").resolve()) in worker


def test_snapshot_does_not_preopen_cdr_before_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_cdr = tmp_path / "result.cdr"
    output_png = tmp_path / "result.png"
    source_cdr.write_bytes(b"RIFFcdr-test-data")
    config = replace(
        converter.ConverterConfig(),
        corel_scripts_root=str(tmp_path / "scripts"),
        corel_bootstrap_document=str(tmp_path / "missing-bootstrap.cdr"),
        automation_timeout_seconds=1,
        corel_open_wait_seconds=0,
        corel_dialog_wait_seconds=0,
    )
    popen_commands: list[list[str]] = []
    worker_names: list[str] = []

    monkeypatch.setattr(converter.sys, "platform", "darwin")
    monkeypatch.setattr(
        converter.subprocess,
        "Popen",
        lambda command, **kwargs: popen_commands.append(command),
    )

    def fake_run(command, **kwargs):
        """Mock process checks and render the Corel PDF proof.

        Args:
            command: Subprocess command under test.
            **kwargs: Ignored subprocess options.

        Returns:
            Successful subprocess result.
        """
        if command[0] == "/usr/bin/sips":
            output_png.write_bytes(b"png-data")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(converter.subprocess, "run", fake_run)
    monkeypatch.setattr(converter.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        converter,
        "run_corel_worker_with_accessibility",
        lambda process_name, worker_name, wait_seconds, expected_output: (
            worker_names.append(worker_name),
            expected_output.write_bytes(b"%PDF-proof"),
        ),
    )

    result = converter.export_cdr_snapshot(source_cdr, output_png, config)

    assert result == output_png.resolve()
    assert len(popen_commands) == 1
    assert str(source_cdr.resolve()) not in popen_commands[0]
    assert worker_names == ["AI-CDR-Worker"]
    assert (tmp_path / "scripts" / "AI-CDR-Worker.js").is_file()


def test_conversion_reopens_bootstrap_after_corel_process_starts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.ai"
    bridge_pdf = tmp_path / "bridge.pdf"
    output_cdr = tmp_path / "result.cdr"
    bootstrap_cdr = tmp_path / "bootstrap.cdr"
    source.write_bytes(b"illustrator-data")
    bootstrap_cdr.write_bytes(b"RIFFbootstrap")
    config = replace(
        converter.ConverterConfig(),
        corel_scripts_root=str(tmp_path / "scripts"),
        corel_bootstrap_document=str(bootstrap_cdr),
        automation_timeout_seconds=1,
        corel_open_wait_seconds=0,
        corel_dialog_wait_seconds=0,
        minimum_cdr_bytes=4,
    )
    popen_commands: list[list[str]] = []
    run_commands: list[list[str]] = []
    progress_updates: list[tuple[str, int, str]] = []

    def fake_run(command, **kwargs):
        """Create the bridge when the mocked Illustrator driver runs.

        Args:
            command: Subprocess command under test.
            **kwargs: Ignored subprocess options.

        Returns:
            Successful subprocess result.
        """
        run_commands.append(command)
        if command[0] == "/usr/bin/osascript":
            bridge_pdf.write_bytes(b"%PDF-1.7\nartwork")
        if command[0] == "/usr/bin/sips":
            output_cdr.with_name("result.preview.png").write_bytes(b"png-data")
        return SimpleNamespace(returncode=0)

    def fake_corel_worker(
        process_name,
        worker_name,
        wait_seconds,
        expected_output,
    ):
        """Create validated outputs when CorelDRAW automation is invoked.

        Args:
            process_name: CorelDRAW process name under test.
            worker_name: Generated CorelDRAW worker name.
            wait_seconds: Accessibility UI delay.
            expected_output: Validation artifact expected from CorelDRAW.
        """
        output_cdr.write_bytes(b"RIFFcdr-data")
        output_cdr.with_name("result.preview.pdf").write_bytes(b"%PDF-proof")

    monkeypatch.setattr(converter.sys, "platform", "darwin")
    monkeypatch.setattr(converter.subprocess, "run", fake_run)
    monkeypatch.setattr(
        converter.subprocess,
        "Popen",
        lambda command, **kwargs: popen_commands.append(command),
    )
    monkeypatch.setattr(converter.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        converter,
        "run_corel_worker_with_accessibility",
        fake_corel_worker,
    )

    desktop_converter = converter.MacOSDesktopConverter(config)
    desktop_converter.progress_callback = lambda stage, percentage, message: (
        progress_updates.append((stage, percentage, message))
    )
    desktop_converter.convert(
        source,
        bridge_pdf,
        output_cdr,
    )

    illustrator_driver = (tmp_path / "run-illustrator.applescript").read_text(
        encoding="utf-8"
    )
    assert "do javascript file jsxFile" in illustrator_driver

    illustrator_commands = [
        command
        for command in popen_commands
        if command[:2] == ["/usr/bin/open", "-b"] and config.illustrator_app in command
    ]
    assert len(illustrator_commands) == 1
    corel_commands = [
        command
        for command in popen_commands
        if command[0] == "/usr/bin/open" and config.corel_app in command
    ]
    assert len(corel_commands) == 1
    assert all(str(bootstrap_cdr) in command for command in corel_commands)
    assert any(
        command[:2] == ["/usr/bin/osascript", "-e"]
        and str(bootstrap_cdr.resolve()) in command[2]
        for command in run_commands
    )
    assert [stage for stage, _, _ in progress_updates] == [
        "illustrator",
        "pdf_bridge",
        "coreldraw",
        "validated",
    ]
    assert [percentage for _, percentage, _ in progress_updates] == [30, 55, 62, 88]


def test_launch_agent_uses_python_watch_mode(tmp_path: Path) -> None:
    service = converter.build_launch_agent(
        tmp_path,
        python_executable=Path("/usr/bin/python3"),
    )

    assert service["Label"] == "com.dianchi.ai-cdr-converter"
    assert service["RunAtLoad"] is True
    assert service["KeepAlive"] is True
    assert service["ProcessType"] == "Interactive"
    assert service["ProgramArguments"][0] == "/usr/bin/python3"
    assert service["ProgramArguments"][2:] == [
        "watch",
        "--root",
        str(tmp_path.resolve()),
    ]
    assert service["StandardOutPath"] == str(
        Path.home() / "Library" / "Logs" / "AI-CDR-Converter" / "service.stdout.log"
    )
    assert service["StandardErrorPath"] == str(
        Path.home() / "Library" / "Logs" / "AI-CDR-Converter" / "service.stderr.log"
    )
    assert not service["StandardOutPath"].startswith(str(tmp_path))
