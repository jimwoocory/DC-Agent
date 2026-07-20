#!/usr/bin/env python3
"""Queue-based Adobe Illustrator to CorelDRAW converter for macOS."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

QUEUE_DIRECTORIES = (
    "inbox",
    "processing",
    "outbox",
    "outbox/originals",
    "outbox/bridge_pdf",
    "outbox/previews",
    "color_profiles",
    "progress",
    "failed",
    "reports",
)
DEFAULT_SERVICE_LABEL = "com.dianchi.ai-cdr-converter"


class ConversionError(RuntimeError):
    """Raised when a desktop application cannot complete a conversion."""


class DesktopConverter(Protocol):
    """Convert one AI source into a bridge PDF and CDR document."""

    def convert(self, source: Path, bridge_pdf: Path, output_cdr: Path) -> None:
        """Convert a single source document.

        Args:
            source: AI source inside the active job directory.
            bridge_pdf: PDF path that Illustrator must create.
            output_cdr: CDR path that CorelDRAW must create.

        Raises:
            ConversionError: If either desktop application fails.
        """


@dataclass(frozen=True)
class ConverterConfig:
    """Runtime settings for the macOS desktop conversion engines."""

    illustrator_app: str = "com.adobe.illustrator"
    corel_app: str = "com.corel.coreldrawsuite.2026.coreldraw"
    corel_process_name: str = "CorelDRW"
    corel_scripts_root: str = (
        "~/Library/Application Support/Corel/CorelDRAW Graphics Suite 2026/Draw/Scripts"
    )
    corel_bootstrap_document: str = (
        "~/Documents/Corel/Corel Content/Images/Starter pack/CGS03556.cdr"
    )
    corel_worker_name: str = "AI-CDR-Worker"
    local_work_root: str = "~/Library/Caches/com.dianchi.ai-cdr-converter/work"
    poll_seconds: float = 2.0
    settle_seconds: float = 3.0
    automation_timeout_seconds: float = 240.0
    illustrator_open_wait_seconds: float = 12.0
    corel_open_wait_seconds: float = 8.0
    corel_dialog_wait_seconds: float = 1.5
    corel_save_wait_seconds: float = 8.0
    corel_import_confirm_presses: int = 1
    corel_post_save_confirm_presses: int = 2
    outline_text: bool = True
    embed_linked_images: bool = True
    preserve_bridge_pdf: bool = True
    minimum_cdr_bytes: int = 1024


@dataclass(frozen=True)
class JobResult:
    """Final or planned state of one queue item."""

    job_id: str
    source_name: str
    status: str
    report_path: Path
    output_cdr: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class CMYKColorMapping:
    """One exact process-CMYK replacement requested by a designer."""

    label: str
    source: tuple[float, float, float, float]
    target: tuple[float, float, float, float]
    apply_to: str
    tolerance: float


@dataclass(frozen=True)
class ColorCalibration:
    """Validated per-file color calibration sidecar."""

    version: int
    source_name: str
    mappings: tuple[CMYKColorMapping, ...]
    icc_profile: str | None
    icc_profile_path: Path | None
    icc_sha256: str | None


def load_color_calibration(
    sidecar: Path,
    *,
    queue_root: Path | None = None,
) -> ColorCalibration:
    """Load and validate one designer-authored color calibration sidecar.

    Args:
        sidecar: JSON file stored beside the AI source.
        queue_root: Queue root used to resolve and validate an ICC profile.

    Returns:
        Normalized CMYK mappings and optional ICC profile metadata.

    Raises:
        ValueError: If the schema, CMYK values, scope, or ICC file is invalid.
        OSError: If the sidecar or referenced ICC profile cannot be read.
    """
    with sidecar.open(encoding="utf-8") as calibration_file:
        raw = json.load(calibration_file)
    if not isinstance(raw, dict):
        raise ValueError("color calibration must contain a JSON object")
    if raw.get("version") != 1:
        raise ValueError("color calibration version must be 1")

    source_name = raw.get("source_name", "")
    if not isinstance(source_name, str) or not source_name.strip():
        raise ValueError("color calibration source_name is required")
    source_name = source_name.strip()

    raw_mappings = raw.get("mappings", [])
    if not isinstance(raw_mappings, list) or len(raw_mappings) > 64:
        raise ValueError("color calibration mappings must be a list of at most 64")
    mappings: list[CMYKColorMapping] = []
    for index, raw_mapping in enumerate(raw_mappings, start=1):
        if not isinstance(raw_mapping, dict):
            raise ValueError(f"color mapping {index} must be an object")
        label = raw_mapping.get("label") or f"mapping-{index}"
        if not isinstance(label, str):
            raise ValueError(f"color mapping {index} label must be text")
        apply_to = raw_mapping.get("apply_to", "all")
        if apply_to not in {"all", "fill", "stroke"}:
            raise ValueError(
                f"color mapping {index} apply_to must be all, fill, or stroke"
            )
        tolerance = raw_mapping.get("tolerance", 0.1)
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, int | float)
            or not 0 <= tolerance <= 5
        ):
            raise ValueError(f"color mapping {index} tolerance must be 0 to 5")

        normalized_colors: list[tuple[float, float, float, float]] = []
        for field_name in ("source", "target"):
            raw_color = raw_mapping.get(field_name)
            if not isinstance(raw_color, dict):
                raise ValueError(
                    f"color mapping {index} {field_name} must be an object"
                )
            values: list[float] = []
            for component in ("c", "m", "y", "k"):
                value = raw_color.get(component)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not 0 <= value <= 100
                ):
                    raise ValueError(
                        f"color mapping {index} {field_name}.{component} "
                        "must be 0 to 100"
                    )
                values.append(float(value))
            normalized_colors.append(tuple(values))
        mappings.append(
            CMYKColorMapping(
                label=label.strip() or f"mapping-{index}",
                source=normalized_colors[0],
                target=normalized_colors[1],
                apply_to=apply_to,
                tolerance=float(tolerance),
            )
        )

    icc_profile = raw.get("icc_profile")
    if icc_profile is not None and (
        not isinstance(icc_profile, str) or not icc_profile.strip()
    ):
        raise ValueError("color calibration icc_profile must be text")
    icc_profile_path: Path | None = None
    icc_sha256: str | None = None
    if isinstance(icc_profile, str):
        icc_profile = icc_profile.strip()
        relative_profile = Path(icc_profile)
        if relative_profile.is_absolute() or ".." in relative_profile.parts:
            raise ValueError("icc_profile must be relative to the queue root")
        if queue_root is not None:
            icc_profile_path = (queue_root / relative_profile).resolve()
            resolved_root = queue_root.resolve()
            if not icc_profile_path.is_relative_to(resolved_root):
                raise ValueError("icc_profile escapes the queue root")
            with icc_profile_path.open("rb") as profile_file:
                profile_header = profile_file.read(128)
            if len(profile_header) < 128 or profile_header[36:40] != b"acsp":
                raise ValueError("icc_profile is not a valid ICC color profile")
            profile_digest = hashlib.sha256()
            with icc_profile_path.open("rb") as profile_file:
                for chunk in iter(lambda: profile_file.read(1024 * 1024), b""):
                    profile_digest.update(chunk)
            icc_sha256 = profile_digest.hexdigest()

    return ColorCalibration(
        version=1,
        source_name=source_name,
        mappings=tuple(mappings),
        icc_profile=icc_profile,
        icc_profile_path=icc_profile_path,
        icc_sha256=icc_sha256,
    )


def initialize_workspace(root: Path, *, overwrite_config: bool = False) -> Path:
    """Create the queue directories and default configuration.

    Args:
        root: Local or mounted NAS directory used as the queue root.
        overwrite_config: Replace an existing configuration when true.

    Returns:
        Path to the queue configuration file.

    Raises:
        OSError: If the queue cannot be created or written.
    """
    root = root.expanduser().resolve()
    for directory in QUEUE_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)

    config_path = root / "config.json"
    if overwrite_config or not config_path.exists():
        config_path.write_text(
            json.dumps(asdict(ConverterConfig()), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return config_path


def load_config(root: Path) -> ConverterConfig:
    """Load and validate the queue configuration.

    Args:
        root: Queue root containing ``config.json``.

    Returns:
        Validated converter configuration.

    Raises:
        FileNotFoundError: If the configuration does not exist.
        ValueError: If the JSON root or a setting is invalid.
    """
    config_path = root.expanduser().resolve() / "config.json"
    with config_path.open(encoding="utf-8") as config_file:
        raw = json.load(config_file)
    if not isinstance(raw, dict):
        raise ValueError("config.json must contain a JSON object")

    known_fields = {field.name for field in fields(ConverterConfig)}
    unknown_fields = sorted(set(raw) - known_fields)
    if unknown_fields:
        raise ValueError(f"unknown config fields: {', '.join(unknown_fields)}")

    config = ConverterConfig(**raw)
    if config.poll_seconds <= 0:
        raise ValueError("poll_seconds must be greater than zero")
    if config.settle_seconds < 0:
        raise ValueError("settle_seconds cannot be negative")
    if config.automation_timeout_seconds <= 0:
        raise ValueError("automation_timeout_seconds must be greater than zero")
    if config.illustrator_open_wait_seconds < 0:
        raise ValueError("illustrator_open_wait_seconds cannot be negative")
    if config.minimum_cdr_bytes < 1:
        raise ValueError("minimum_cdr_bytes must be at least one")
    if not config.local_work_root.strip():
        raise ValueError("local_work_root cannot be empty")
    if not config.corel_bootstrap_document.strip():
        raise ValueError("corel_bootstrap_document cannot be empty")
    if config.corel_import_confirm_presses < 0:
        raise ValueError("corel_import_confirm_presses cannot be negative")
    if config.corel_post_save_confirm_presses < 0:
        raise ValueError("corel_post_save_confirm_presses cannot be negative")
    return config


def build_illustrator_jsx(
    source: Path,
    bridge_pdf: Path,
    config: ConverterConfig,
    calibration: ColorCalibration | None = None,
    calibration_audit: Path | None = None,
) -> str:
    """Build the Illustrator JavaScript used for a color-managed PDF bridge.

    Args:
        source: AI file to open in Illustrator.
        bridge_pdf: Destination PDF consumed by CorelDRAW.
        config: Conversion settings controlling links and text treatment.
        calibration: Optional validated process-CMYK replacement rules.
        calibration_audit: JSON path receiving per-rule match counts.

    Returns:
        ExtendScript source suitable for Illustrator's ``do javascript`` command.
    """
    source_literal = json.dumps(str(source.resolve()), ensure_ascii=False)
    bridge_literal = json.dumps(str(bridge_pdf.resolve()), ensure_ascii=False)
    embed_script = ""
    if config.embed_linked_images:
        embed_script = """
    for (var placedIndex = documentRef.placedItems.length - 1; placedIndex >= 0; placedIndex--) {
        documentRef.placedItems[placedIndex].embed();
    }
"""
    outline_script = ""
    if config.outline_text:
        outline_script = """
    for (var textIndex = documentRef.textFrames.length - 1; textIndex >= 0; textIndex--) {
        documentRef.textFrames[textIndex].createOutline();
    }
"""
    calibration_script = ""
    if calibration and calibration.mappings:
        if calibration_audit is None:
            raise ValueError("calibration_audit is required for CMYK mappings")
        mappings_literal = json.dumps(
            [
                {
                    "label": mapping.label,
                    "source": mapping.source,
                    "target": mapping.target,
                    "applyTo": mapping.apply_to,
                    "tolerance": mapping.tolerance,
                }
                for mapping in calibration.mappings
            ],
            ensure_ascii=False,
        )
        audit_literal = json.dumps(
            str(calibration_audit.resolve()),
            ensure_ascii=False,
        )
        calibration_script = f"""
    var colorMappings = {mappings_literal};
    var mappingMatchCounts = [];
    for (var mappingIndex = 0; mappingIndex < colorMappings.length; mappingIndex++) {{
        mappingMatchCounts[mappingIndex] = 0;
    }}

    function matchingColorMapping(color, isStroke) {{
        if (!color || color.typename !== "CMYKColor") {{
            return -1;
        }}
        for (var matchIndex = 0; matchIndex < colorMappings.length; matchIndex++) {{
            var mapping = colorMappings[matchIndex];
            if (
                (mapping.applyTo === "all" ||
                    (isStroke && mapping.applyTo === "stroke") ||
                    (!isStroke && mapping.applyTo === "fill")) &&
                Math.abs(color.cyan - mapping.source[0]) <= mapping.tolerance &&
                Math.abs(color.magenta - mapping.source[1]) <= mapping.tolerance &&
                Math.abs(color.yellow - mapping.source[2]) <= mapping.tolerance &&
                Math.abs(color.black - mapping.source[3]) <= mapping.tolerance
            ) {{
                return matchIndex;
            }}
        }}
        return -1;
    }}

    function calibratedCMYKColor(mapping) {{
        var color = new CMYKColor();
        color.cyan = mapping.target[0];
        color.magenta = mapping.target[1];
        color.yellow = mapping.target[2];
        color.black = mapping.target[3];
        return color;
    }}

    for (var pageItemIndex = 0; pageItemIndex < documentRef.pageItems.length; pageItemIndex++) {{
        var pageItem = documentRef.pageItems[pageItemIndex];
        try {{
            if (pageItem.filled) {{
                var fillMappingIndex = matchingColorMapping(pageItem.fillColor, false);
                if (fillMappingIndex >= 0) {{
                    pageItem.fillColor = calibratedCMYKColor(colorMappings[fillMappingIndex]);
                    mappingMatchCounts[fillMappingIndex]++;
                }}
            }}
        }} catch (fillError) {{}}
        try {{
            if (pageItem.stroked) {{
                var strokeMappingIndex = matchingColorMapping(pageItem.strokeColor, true);
                if (strokeMappingIndex >= 0) {{
                    pageItem.strokeColor = calibratedCMYKColor(colorMappings[strokeMappingIndex]);
                    mappingMatchCounts[strokeMappingIndex]++;
                }}
            }}
        }} catch (strokeError) {{}}
    }}

    var calibrationAudit = {{
        version: 1,
        source_name: sourceFile.name,
        mappings: []
    }};
    for (var auditIndex = 0; auditIndex < colorMappings.length; auditIndex++) {{
        calibrationAudit.mappings.push({{
            label: colorMappings[auditIndex].label,
            source: colorMappings[auditIndex].source,
            target: colorMappings[auditIndex].target,
            apply_to: colorMappings[auditIndex].applyTo,
            tolerance: colorMappings[auditIndex].tolerance,
            matches: mappingMatchCounts[auditIndex]
        }});
    }}
    var calibrationAuditFile = new File({audit_literal});
    calibrationAuditFile.encoding = "UTF-8";
    if (!calibrationAuditFile.open("w")) {{
        throw new Error("Cannot write color calibration audit");
    }}
    calibrationAuditFile.write(JSON.stringify(calibrationAudit, null, 2));
    calibrationAuditFile.close();
    for (var requiredIndex = 0; requiredIndex < mappingMatchCounts.length; requiredIndex++) {{
        if (mappingMatchCounts[requiredIndex] === 0) {{
            throw new Error(
                "Color calibration did not match any object: " +
                colorMappings[requiredIndex].label
            );
        }}
    }}
"""

    return f"""// Generated by ai_cdr_converter.py. Do not edit active documents manually.
var sourceFile = new File({source_literal});
var bridgeFile = new File({bridge_literal});
if (!sourceFile.exists) {{
    throw new Error("AI source does not exist: " + sourceFile.fsName);
}}

app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
var documentRef = app.open(sourceFile);
try {{{embed_script}{outline_script}{calibration_script}
    if (documentRef.pageItems.length === 0) {{
        throw new Error("Illustrator source contains no page items");
    }}

    var artworkBounds = documentRef.visibleBounds;
    var intersectsArtboard = false;
    for (var artboardIndex = 0; artboardIndex < documentRef.artboards.length; artboardIndex++) {{
        var artboardBounds = documentRef.artboards[artboardIndex].artboardRect;
        if (
            artworkBounds[0] < artboardBounds[2] &&
            artworkBounds[2] > artboardBounds[0] &&
            artworkBounds[1] > artboardBounds[3] &&
            artworkBounds[3] < artboardBounds[1]
        ) {{
            intersectsArtboard = true;
            break;
        }}
    }}
    if (!intersectsArtboard) {{
        if (
            artworkBounds[2] <= artworkBounds[0] ||
            artworkBounds[1] <= artworkBounds[3]
        ) {{
            throw new Error("Illustrator artwork has invalid visible bounds");
        }}
        var activeArtboardIndex = documentRef.artboards.getActiveArtboardIndex();
        documentRef.artboards[activeArtboardIndex].artboardRect = artworkBounds;
    }}

    var pdfOptions = new PDFSaveOptions();
    pdfOptions.colorConversionID = ColorConversion.None;
    pdfOptions.colorDestinationID = ColorDestination.None;
    pdfOptions.colorProfileID = ColorProfile.LEAVEPROFILEUNCHANGED;
    pdfOptions.colorDownsamplingMethod = DownsampleMethod.NODOWNSAMPLE;
    pdfOptions.grayscaleDownsamplingMethod = DownsampleMethod.NODOWNSAMPLE;
    pdfOptions.monochromeDownsamplingMethod = DownsampleMethod.NODOWNSAMPLE;
    pdfOptions.preserveEditability = false;
    pdfOptions.generateThumbnails = false;
    pdfOptions.optimization = false;
    pdfOptions.viewAfterSaving = false;
    documentRef.saveAs(bridgeFile, pdfOptions);
}} finally {{
    documentRef.close(SaveOptions.DONOTSAVECHANGES);
}}

var bridgeDocument = app.open(bridgeFile);
try {{
    if (bridgeDocument.pageItems.length === 0) {{
        throw new Error("Illustrator created an empty bridge PDF");
    }}
}} finally {{
    bridgeDocument.close(SaveOptions.DONOTSAVECHANGES);
}}
"""


def build_corel_javascript(bridge_pdf: Path, output_cdr: Path) -> str:
    """Build a CorelDRAW script that saves through the native document API.

    Args:
        bridge_pdf: Illustrator PDF bridge opened by CorelDRAW.
        output_cdr: CDR path written by CorelDRAW.

    Returns:
        JavaScript source for CorelDRAW's in-app JavaScript engine.
    """
    bridge_literal = json.dumps(str(bridge_pdf.resolve()), ensure_ascii=False)
    output_literal = json.dumps(str(output_cdr.resolve()), ensure_ascii=False)
    validation_cdr = output_cdr.with_name(
        f".{output_cdr.stem}.validating{output_cdr.suffix}"
    )
    preview_pdf = output_cdr.with_name(f"{output_cdr.stem}.preview.pdf")
    validation_literal = json.dumps(str(validation_cdr.resolve()), ensure_ascii=False)
    preview_literal = json.dumps(str(preview_pdf.resolve()), ensure_ascii=False)
    return f"""// Generated by ai_cdr_converter.py for one local job.
let documentRef = host.OpenDocument({bridge_literal});
try {{
    let importedShapeCount = 0;
    for (let pageIndex = 1; pageIndex <= documentRef.Pages.Count; pageIndex++) {{
        importedShapeCount += documentRef.Pages.Item(pageIndex).Shapes.Count;
    }}
    if (importedShapeCount === 0) {{
        throw new Error("PDF import contains no CorelDRAW shapes");
    }}
    let saveOptions = host.CreateStructSaveAsOptions();
    saveOptions.Filter = 1795; // cdrCDR
    saveOptions.Overwrite = true;
    saveOptions.EmbedICCProfile = true;
    saveOptions.Range = 0; // cdrAllPages
    documentRef.SaveAs({validation_literal}, saveOptions);
}} finally {{
    documentRef.Close();
}}

let validationDocument = host.OpenDocument({validation_literal});
try {{
    let validatedShapeCount = 0;
    for (let pageIndex = 1; pageIndex <= validationDocument.Pages.Count; pageIndex++) {{
        validatedShapeCount += validationDocument.Pages.Item(pageIndex).Shapes.Count;
    }}
    if (validatedShapeCount === 0) {{
        throw new Error("validated CDR contains no CorelDRAW shapes");
    }}
    validationDocument.PublishToPDF({preview_literal});
    let finalSaveOptions = host.CreateStructSaveAsOptions();
    finalSaveOptions.Filter = 1795; // cdrCDR
    finalSaveOptions.Overwrite = true;
    finalSaveOptions.EmbedICCProfile = true;
    finalSaveOptions.Range = 0; // cdrAllPages
    validationDocument.SaveAs({output_literal}, finalSaveOptions);
}} finally {{
    validationDocument.Close();
}}
"""


def build_corel_snapshot_javascript(source_cdr: Path, output_pdf: Path) -> str:
    """Build a CorelDRAW script that publishes one CDR document to PDF.

    Args:
        source_cdr: Existing CDR document opened by CorelDRAW.
        output_pdf: PDF proof written by CorelDRAW.

    Returns:
        JavaScript source for CorelDRAW's in-app JavaScript engine.
    """
    source_literal = json.dumps(str(source_cdr.resolve()), ensure_ascii=False)
    output_literal = json.dumps(str(output_pdf.resolve()), ensure_ascii=False)
    return f"""// Generated by ai_cdr_converter.py for one CDR snapshot.
let documentRef = host.OpenDocument({source_literal});
try {{
    let shapeCount = 0;
    for (let pageIndex = 1; pageIndex <= documentRef.Pages.Count; pageIndex++) {{
        shapeCount += documentRef.Pages.Item(pageIndex).Shapes.Count;
    }}
    if (shapeCount === 0) {{
        throw new Error("CDR snapshot source contains no shapes");
    }}
    documentRef.PublishToPDF({output_literal});
}} finally {{
    documentRef.Close();
}}
"""


def run_corel_worker_with_accessibility(
    process_name: str,
    worker_name: str,
    dialog_wait_seconds: float,
    expected_output: Path,
) -> None:
    """Run the fixed CorelDRAW script through the native accessibility API.

    This runs inside the signed converter process so macOS applies the
    AI-CDR-Converter app's accessibility permission. It avoids the separate
    ``osascript`` process, clipboard access, and Save As keystrokes.

    Args:
        process_name: CorelDRAW executable process name.
        worker_name: User script name visible in CorelDRAW's Scripts inspector.
        dialog_wait_seconds: Delay after opening menus or expanding tree nodes.
        expected_output: First artifact proving the script actually started.

    Raises:
        ConversionError: If permission is missing or a required control cannot
            be found or activated.
    """
    if sys.platform != "darwin":
        raise ConversionError("CorelDRAW accessibility requires macOS")

    application_services = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    core_foundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class CGSize(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    pointer = ctypes.c_void_p
    encoding_utf8 = 0x08000100

    application_services.AXIsProcessTrusted.restype = ctypes.c_bool
    application_services.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
    application_services.AXUIElementCreateApplication.restype = pointer
    application_services.AXUIElementCopyAttributeValue.argtypes = [
        pointer,
        pointer,
        ctypes.POINTER(pointer),
    ]
    application_services.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
    application_services.AXUIElementPerformAction.argtypes = [pointer, pointer]
    application_services.AXUIElementPerformAction.restype = ctypes.c_int32
    application_services.AXUIElementSetAttributeValue.argtypes = [
        pointer,
        pointer,
        pointer,
    ]
    application_services.AXUIElementSetAttributeValue.restype = ctypes.c_int32
    application_services.AXValueGetValue.argtypes = [
        pointer,
        ctypes.c_int32,
        pointer,
    ]
    application_services.AXValueGetValue.restype = ctypes.c_bool
    application_services.CGEventCreateMouseEvent.argtypes = [
        pointer,
        ctypes.c_uint32,
        CGPoint,
        ctypes.c_uint32,
    ]
    application_services.CGEventCreateMouseEvent.restype = pointer
    application_services.CGEventCreateKeyboardEvent.argtypes = [
        pointer,
        ctypes.c_uint16,
        ctypes.c_bool,
    ]
    application_services.CGEventCreateKeyboardEvent.restype = pointer
    application_services.CGEventSetIntegerValueField.argtypes = [
        pointer,
        ctypes.c_uint32,
        ctypes.c_int64,
    ]
    application_services.CGEventPost.argtypes = [ctypes.c_uint32, pointer]
    application_services.CGEventPostToPid.argtypes = [ctypes.c_int, pointer]

    core_foundation.CFStringCreateWithCString.argtypes = [
        pointer,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    core_foundation.CFStringCreateWithCString.restype = pointer
    core_foundation.CFGetTypeID.argtypes = [pointer]
    core_foundation.CFGetTypeID.restype = ctypes.c_ulong
    core_foundation.CFStringGetTypeID.restype = ctypes.c_ulong
    core_foundation.CFStringGetLength.argtypes = [pointer]
    core_foundation.CFStringGetLength.restype = ctypes.c_long
    core_foundation.CFStringGetMaximumSizeForEncoding.argtypes = [
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    core_foundation.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    core_foundation.CFStringGetCString.argtypes = [
        pointer,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    core_foundation.CFStringGetCString.restype = ctypes.c_bool
    core_foundation.CFArrayGetCount.argtypes = [pointer]
    core_foundation.CFArrayGetCount.restype = ctypes.c_long
    core_foundation.CFArrayGetValueAtIndex.argtypes = [pointer, ctypes.c_long]
    core_foundation.CFArrayGetValueAtIndex.restype = pointer
    core_foundation.CFRetain.argtypes = [pointer]
    core_foundation.CFRetain.restype = pointer
    core_foundation.CFRelease.argtypes = [pointer]

    if not application_services.AXIsProcessTrusted():
        raise ConversionError(
            "AI-CDR-Converter is not enabled in System Settings > "
            "Privacy & Security > Accessibility"
        )

    process_result = subprocess.run(
        ["/usr/bin/pgrep", "-x", process_name],
        check=False,
        capture_output=True,
        text=True,
    )
    process_ids = [
        int(value)
        for value in process_result.stdout.splitlines()
        if value.strip().isdigit()
    ]
    if not process_ids:
        raise ConversionError(f"CorelDRAW process is not running: {process_name}")

    def create_cf_string(value: str) -> int:
        return int(
            core_foundation.CFStringCreateWithCString(
                None,
                value.encode("utf-8"),
                encoding_utf8,
            )
        )

    def release(value: int | None) -> None:
        if value:
            core_foundation.CFRelease(pointer(value))

    def copy_attribute(element: int, name: str) -> int | None:
        attribute = create_cf_string(name)
        result = pointer()
        try:
            error = application_services.AXUIElementCopyAttributeValue(
                pointer(element),
                pointer(attribute),
                ctypes.byref(result),
            )
        finally:
            release(attribute)
        return int(result.value) if error == 0 and result.value else None

    def cf_string_to_text(value: int) -> str | None:
        if core_foundation.CFGetTypeID(pointer(value)) != (
            core_foundation.CFStringGetTypeID()
        ):
            return None
        length = core_foundation.CFStringGetLength(pointer(value))
        if length < 0:
            return None
        maximum = (
            core_foundation.CFStringGetMaximumSizeForEncoding(
                length,
                encoding_utf8,
            )
            + 1
        )
        buffer = ctypes.create_string_buffer(maximum)
        if not core_foundation.CFStringGetCString(
            pointer(value),
            buffer,
            maximum,
            encoding_utf8,
        ):
            return None
        return buffer.value.decode("utf-8", errors="replace")

    def element_texts(element: int) -> list[str]:
        texts: list[str] = []
        for attribute_name in ("AXTitle", "AXDescription", "AXValue", "AXHelp"):
            value = copy_attribute(element, attribute_name)
            if value is None:
                continue
            try:
                text = cf_string_to_text(value)
                if text:
                    texts.append(text)
            finally:
                release(value)
        return texts

    def element_role(element: int) -> str | None:
        value = copy_attribute(element, "AXRole")
        if value is None:
            return None
        try:
            return cf_string_to_text(value)
        finally:
            release(value)

    def find_named(
        element: int,
        targets: tuple[str, ...],
        depth: int = 0,
        *,
        exact: bool = False,
    ) -> int | None:
        normalized_targets = tuple(target.casefold() for target in targets)
        for text in element_texts(element):
            normalized_text = text.casefold()
            if any(
                target == normalized_text or (not exact and target in normalized_text)
                for target in normalized_targets
            ):
                return int(core_foundation.CFRetain(pointer(element)))
        if depth >= 14:
            return None
        children = copy_attribute(element, "AXChildren")
        if children is None:
            return None
        try:
            for index in range(core_foundation.CFArrayGetCount(pointer(children))):
                child = core_foundation.CFArrayGetValueAtIndex(
                    pointer(children),
                    index,
                )
                if not child:
                    continue
                found = find_named(
                    int(child),
                    targets,
                    depth + 1,
                    exact=exact,
                )
                if found is not None:
                    return found
        finally:
            release(children)
        return None

    def find_in_windows(application: int, targets: tuple[str, ...]) -> int | None:
        windows = copy_attribute(application, "AXWindows")
        if windows is None:
            return None
        try:
            for index in range(core_foundation.CFArrayGetCount(pointer(windows))):
                window = core_foundation.CFArrayGetValueAtIndex(
                    pointer(windows),
                    index,
                )
                if not window:
                    continue
                found = find_named(int(window), targets)
                if found is not None:
                    return found
        finally:
            release(windows)
        return None

    def perform_action(element: int, action_name: str) -> int:
        action = create_cf_string(action_name)
        try:
            return int(
                application_services.AXUIElementPerformAction(
                    pointer(element),
                    pointer(action),
                )
            )
        finally:
            release(action)

    def double_click_element(element: int) -> bool:
        position_value = copy_attribute(element, "AXPosition")
        size_value = copy_attribute(element, "AXSize")
        if position_value is None or size_value is None:
            release(position_value)
            release(size_value)
            return False
        position = CGPoint()
        size = CGSize()
        try:
            if not application_services.AXValueGetValue(
                pointer(position_value),
                1,
                ctypes.byref(position),
            ) or not application_services.AXValueGetValue(
                pointer(size_value),
                2,
                ctypes.byref(size),
            ):
                return False
        finally:
            release(position_value)
            release(size_value)

        click_point = CGPoint(
            position.x + max(size.width / 2, 1),
            position.y + max(size.height / 2, 1),
        )
        for click_count in (1, 2):
            for event_type in (1, 2):
                event = application_services.CGEventCreateMouseEvent(
                    None,
                    event_type,
                    click_point,
                    0,
                )
                if not event:
                    return False
                try:
                    application_services.CGEventSetIntegerValueField(
                        event,
                        1,
                        click_count,
                    )
                    application_services.CGEventPost(0, event)
                finally:
                    release(int(event))
                time.sleep(0.05)
        return True

    def disclose_named_container(
        application: int,
        targets: tuple[str, ...],
    ) -> None:
        current = find_in_windows(application, targets)
        if current is None:
            return
        attribute = create_cf_string("AXDisclosing")
        try:
            for _ in range(6):
                role = element_role(current)
                if role not in ("AXStaticText", "AXWindow"):
                    disclose_error = application_services.AXUIElementSetAttributeValue(
                        pointer(current),
                        pointer(attribute),
                        pointer(
                            ctypes.c_void_p.in_dll(
                                core_foundation,
                                "kCFBooleanTrue",
                            ).value
                        ),
                    )
                    if disclose_error == 0:
                        return
                    if perform_action(current, "AXPress") == 0:
                        return
                parent = copy_attribute(current, "AXParent")
                if parent is None:
                    return
                release(current)
                current = parent
        finally:
            release(attribute)
            release(current)

    def activate_named_container(
        application: int,
        targets: tuple[str, ...],
    ) -> str | None:
        current = find_in_windows(application, targets)
        if current is None:
            return None
        try:
            for _ in range(6):
                role = element_role(current)
                if role not in ("AXStaticText", "AXWindow"):
                    if perform_action(current, "AXConfirm") == 0:
                        return "ran"
                    if perform_action(current, "AXPress") == 0:
                        return "selected"
                parent = copy_attribute(current, "AXParent")
                if parent is None:
                    return None
                release(current)
                current = parent
        finally:
            release(current)
        return None

    def collect_texts(element: int, depth: int = 0) -> list[str]:
        texts = element_texts(element)
        if depth >= 8 or len(texts) >= 300:
            return texts[:300]
        children = copy_attribute(element, "AXChildren")
        if children is None:
            return texts[:120]
        try:
            for index in range(core_foundation.CFArrayGetCount(pointer(children))):
                child = core_foundation.CFArrayGetValueAtIndex(
                    pointer(children),
                    index,
                )
                if child:
                    texts.extend(collect_texts(int(child), depth + 1))
                if len(texts) >= 300:
                    break
        finally:
            release(children)
        return texts[:300]

    def collect_element_details(element: int, depth: int = 0) -> list[str]:
        texts = element_texts(element)
        role = element_role(element) or "unknown"
        details = [f"{depth}:{role}:{' / '.join(texts)}"] if texts else []
        if depth >= 10 or len(details) >= 240:
            return details[:240]
        children = copy_attribute(element, "AXChildren")
        if children is None:
            return details
        try:
            for index in range(core_foundation.CFArrayGetCount(pointer(children))):
                child = core_foundation.CFArrayGetValueAtIndex(
                    pointer(children),
                    index,
                )
                if child:
                    details.extend(collect_element_details(int(child), depth + 1))
                if len(details) >= 240:
                    break
        finally:
            release(children)
        return details[:240]

    def open_scripts_inspector(application: int) -> None:
        menu_bar = copy_attribute(application, "AXMenuBar")
        if menu_bar is None:
            raise ConversionError("CorelDRAW menu bar is not accessible")
        try:
            window_menu = find_named(
                menu_bar,
                ("Window", "窗口"),
                exact=True,
            )
            if window_menu is None:
                visible = " | ".join(dict.fromkeys(collect_texts(menu_bar)))
                raise ConversionError(
                    "CorelDRAW Window menu is not accessible; "
                    f"visible controls: {visible}"
                )
            try:
                if perform_action(window_menu, "AXPress") != 0:
                    raise ConversionError("CorelDRAW Window menu cannot be opened")
                time.sleep(dialog_wait_seconds)
                inspectors_menu = find_named(
                    window_menu,
                    ("Inspectors", "检查器"),
                    exact=True,
                )
                if inspectors_menu is None:
                    inspectors_menu = find_named(
                        menu_bar,
                        ("Inspectors", "检查器"),
                        exact=True,
                    )
                if inspectors_menu is None:
                    visible = " | ".join(dict.fromkeys(collect_texts(window_menu)))
                    raise ConversionError(
                        "CorelDRAW Inspectors menu is not accessible; "
                        f"Window controls: {visible}"
                    )
                try:
                    show_error = perform_action(inspectors_menu, "AXShowMenu")
                    if show_error != 0:
                        press_error = perform_action(inspectors_menu, "AXPress")
                        if press_error != 0:
                            raise ConversionError(
                                "CorelDRAW Inspectors submenu cannot be opened; "
                                f"AXShowMenu error {show_error}; "
                                f"AXPress error {press_error}"
                            )
                    time.sleep(dialog_wait_seconds)
                    scripts_menu = find_named(
                        inspectors_menu,
                        ("Scripts", "脚本"),
                        exact=True,
                    )
                    if scripts_menu is None:
                        scripts_menu = find_named(
                            menu_bar,
                            ("Scripts", "脚本"),
                            exact=True,
                        )
                    if scripts_menu is None:
                        visible = " | ".join(
                            dict.fromkeys(collect_texts(inspectors_menu))
                        )
                        raise ConversionError(
                            "CorelDRAW Scripts inspector cannot be opened; "
                            f"Inspectors controls: {visible}"
                        )
                    try:
                        action_error = perform_action(scripts_menu, "AXPress")
                        if action_error != 0:
                            raise ConversionError(
                                "CorelDRAW Scripts inspector cannot be opened; "
                                f"AXPress error {action_error}"
                            )
                    finally:
                        release(scripts_menu)
                finally:
                    release(inspectors_menu)
            finally:
                release(window_menu)
        finally:
            release(menu_bar)

    application = int(
        application_services.AXUIElementCreateApplication(process_ids[-1])
    )
    if not application:
        raise ConversionError("Unable to connect to the CorelDRAW UI process")

    try:
        frontmost_attribute = create_cf_string("AXFrontmost")
        true_value = ctypes.c_void_p.in_dll(
            core_foundation,
            "kCFBooleanTrue",
        ).value
        try:
            application_services.AXUIElementSetAttributeValue(
                pointer(application),
                pointer(frontmost_attribute),
                pointer(true_value),
            )
        finally:
            release(frontmost_attribute)
        time.sleep(dialog_wait_seconds)

        for key_down in (True, False):
            return_event = application_services.CGEventCreateKeyboardEvent(
                None,
                36,
                key_down,
            )
            if return_event:
                try:
                    application_services.CGEventPostToPid(
                        process_ids[-1],
                        return_event,
                    )
                finally:
                    release(int(return_event))
        time.sleep(dialog_wait_seconds)

        windows = copy_attribute(application, "AXWindows")
        if windows is not None:
            try:
                for index in range(core_foundation.CFArrayGetCount(pointer(windows))):
                    window = core_foundation.CFArrayGetValueAtIndex(
                        pointer(windows),
                        index,
                    )
                    if not window:
                        continue
                    dismiss_button = find_named(
                        int(window),
                        ("OK", "确定"),
                        exact=True,
                    )
                    if dismiss_button is None:
                        continue
                    try:
                        perform_action(dismiss_button, "AXPress")
                    finally:
                        release(dismiss_button)
                    time.sleep(dialog_wait_seconds)
                    break
            finally:
                release(windows)

        scripts_panel = find_in_windows(application, ("JavaScript",))
        if scripts_panel is None:
            open_scripts_inspector(application)
        else:
            release(scripts_panel)

        time.sleep(dialog_wait_seconds)
        user_scripts = find_in_windows(application, ("User Scripts", "用户脚本"))
        if user_scripts is None:
            disclose_named_container(application, ("JavaScript",))
            time.sleep(dialog_wait_seconds)
        else:
            release(user_scripts)

        worker = find_in_windows(application, (worker_name,))
        if worker is None:
            disclose_named_container(application, ("User Scripts", "用户脚本"))
            time.sleep(dialog_wait_seconds)
            worker = find_in_windows(application, (worker_name,))
        if worker is None:
            windows = copy_attribute(application, "AXWindows")
            visible = []
            details = []
            if windows is not None:
                try:
                    for index in range(
                        core_foundation.CFArrayGetCount(pointer(windows))
                    ):
                        window = core_foundation.CFArrayGetValueAtIndex(
                            pointer(windows),
                            index,
                        )
                        if window:
                            visible.extend(collect_texts(int(window)))
                            details.extend(collect_element_details(int(window)))
                finally:
                    release(windows)
            raise ConversionError(
                f"CorelDRAW worker script is not visible: {worker_name}; "
                f"window controls: {' | '.join(dict.fromkeys(visible))}; "
                f"AX details: {' | '.join(details)}"
            )
        try:
            double_click_element(worker)
            action_deadline = time.monotonic() + max(
                20,
                dialog_wait_seconds * 12,
            )
            while time.monotonic() < action_deadline:
                if expected_output.exists() and expected_output.stat().st_size > 0:
                    return
                time.sleep(0.25)

            perform_action(worker, "AXConfirm")
            action_deadline = time.monotonic() + max(
                12,
                dialog_wait_seconds * 8,
            )
            while time.monotonic() < action_deadline:
                if expected_output.exists() and expected_output.stat().st_size > 0:
                    return
                time.sleep(0.25)

            perform_action(worker, "AXPress")
            time.sleep(dialog_wait_seconds)
            run_button = find_in_windows(
                application,
                ("Run", "运行", "运行脚本", "Play"),
            )
            if run_button is not None:
                try:
                    perform_action(run_button, "AXPress")
                finally:
                    release(run_button)
            action_deadline = time.monotonic() + max(
                12,
                dialog_wait_seconds * 8,
            )
            while time.monotonic() < action_deadline:
                if expected_output.exists() and expected_output.stat().st_size > 0:
                    return
                time.sleep(0.25)

        finally:
            release(worker)
        raise ConversionError(
            "CorelDRAW worker script did not create its validation output"
        )
    finally:
        release(application)


class MacOSDesktopConverter:
    """Use installed macOS desktop applications as conversion engines."""

    def __init__(self, config: ConverterConfig) -> None:
        """Create a desktop converter.

        Args:
            config: App names, timeouts, and fidelity settings.
        """
        self.config = config
        self.progress_callback: Callable[[str, int, str], None] | None = None

    def _report_progress(self, stage: str, percentage: int, message: str) -> None:
        """Publish one desktop-engine progress milestone when configured.

        Args:
            stage: Stable machine-readable conversion stage.
            percentage: Approximate progress for the current file.
            message: Short designer-facing stage description.
        """
        if self.progress_callback is not None:
            self.progress_callback(stage, percentage, message)

    def convert(self, source: Path, bridge_pdf: Path, output_cdr: Path) -> None:
        """Export AI through Illustrator, then save the PDF as CDR.

        Args:
            source: AI source inside the active job directory.
            bridge_pdf: PDF path that Illustrator must create.
            output_cdr: CDR path that CorelDRAW must create.

        Raises:
            ConversionError: If automation fails or an output is missing.
        """
        if sys.platform != "darwin":
            raise ConversionError("real conversion requires macOS")

        self._report_progress("illustrator", 30, "Illustrator 正在处理 AI")
        work_dir = source.parent
        illustrator_jsx = work_dir / "illustrator-export.jsx"
        illustrator_driver = work_dir / "run-illustrator.applescript"
        calibration_sidecar = source.with_suffix(".color.json")
        calibration: ColorCalibration | None = None
        calibration_audit: Path | None = None
        if calibration_sidecar.is_file():
            calibration = load_color_calibration(calibration_sidecar)
            if calibration.source_name != source.name:
                raise ConversionError(
                    "color calibration source_name does not match the AI file"
                )
            if calibration.mappings:
                calibration_audit = work_dir / "color-calibration-audit.json"
        illustrator_jsx.write_text(
            build_illustrator_jsx(
                source,
                bridge_pdf,
                self.config,
                calibration,
                calibration_audit,
            ),
            encoding="utf-8",
        )
        illustrator_app = json.dumps(self.config.illustrator_app, ensure_ascii=False)
        illustrator_target = (
            f"application id {illustrator_app}"
            if "." in self.config.illustrator_app
            else f"application {illustrator_app}"
        )
        illustrator_driver.write_text(
            f"""on run argv
    set jsxFile to POSIX file (item 1 of argv)
    tell {illustrator_target}
        activate
        do javascript file jsxFile
    end tell
end run
""",
            encoding="utf-8",
        )

        subprocess.Popen(
            ["/usr/bin/open", "-b", self.config.illustrator_app],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(self.config.illustrator_open_wait_seconds)

        try:
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    str(illustrator_driver),
                    str(illustrator_jsx),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=self.config.automation_timeout_seconds,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", None)
            detail = stderr.strip() if isinstance(stderr, str) else str(exc)
            raise ConversionError(f"Illustrator export failed: {detail}") from exc

        if not bridge_pdf.exists() or bridge_pdf.stat().st_size == 0:
            raise ConversionError("Illustrator did not create a usable bridge PDF")
        self._report_progress("pdf_bridge", 55, "PDF 矢量桥接已生成")

        self._report_progress("coreldraw", 62, "CorelDRAW 正在生成 CDR")
        corel_scripts_root = Path(self.config.corel_scripts_root).expanduser()
        corel_scripts_root.mkdir(parents=True, exist_ok=True)
        corel_worker = corel_scripts_root / f"{self.config.corel_worker_name}.js"
        corel_worker.write_text(
            build_corel_javascript(bridge_pdf, output_cdr),
            encoding="utf-8",
        )
        try:
            corel_selector = "-b" if "." in self.config.corel_app else "-a"
            corel_command = [
                "/usr/bin/open",
                corel_selector,
                self.config.corel_app,
            ]
            bootstrap_document = Path(self.config.corel_bootstrap_document).expanduser()
            if bootstrap_document.is_file():
                corel_command.append(str(bootstrap_document))
            corel_started = False
            for _ in range(3):
                subprocess.Popen(
                    corel_command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                attempt_deadline = time.monotonic() + 5
                while time.monotonic() < attempt_deadline:
                    process_check = subprocess.run(
                        [
                            "/usr/bin/pgrep",
                            "-x",
                            self.config.corel_process_name,
                        ],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if process_check.returncode == 0:
                        corel_started = True
                        break
                    time.sleep(1)
                if corel_started:
                    break
            if not corel_started:
                raise ConversionError(
                    f"CorelDRAW process did not start: {self.config.corel_process_name}"
                )
            time.sleep(self.config.corel_open_wait_seconds)
            if bootstrap_document.is_file():
                corel_target = (
                    f"application id {json.dumps(self.config.corel_app)}"
                    if "." in self.config.corel_app
                    else f"application {json.dumps(self.config.corel_app)}"
                )
                subprocess.run(
                    [
                        "/usr/bin/osascript",
                        "-e",
                        f"tell {corel_target} to open POSIX file "
                        f"{json.dumps(str(bootstrap_document.resolve()))}",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=self.config.automation_timeout_seconds,
                )
            time.sleep(self.config.corel_open_wait_seconds)
            run_corel_worker_with_accessibility(
                self.config.corel_process_name,
                self.config.corel_worker_name,
                self.config.corel_dialog_wait_seconds,
                output_cdr.with_name(
                    f".{output_cdr.stem}.validating{output_cdr.suffix}"
                ),
            )
        except ConversionError:
            raise
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            stderr = getattr(exc, "stderr", None)
            detail = stderr.strip() if isinstance(stderr, str) else str(exc)
            raise ConversionError(f"CorelDRAW automation failed: {detail}") from exc

        deadline = time.monotonic() + self.config.automation_timeout_seconds
        preview_pdf = output_cdr.with_name(f"{output_cdr.stem}.preview.pdf")
        preview_png = output_cdr.with_name(f"{output_cdr.stem}.preview.png")
        last_size = -1
        stable_reads = 0
        while time.monotonic() < deadline:
            if output_cdr.exists():
                current_size = output_cdr.stat().st_size
                if current_size >= self.config.minimum_cdr_bytes:
                    stable_reads = stable_reads + 1 if current_size == last_size else 0
                    if stable_reads >= 2:
                        if preview_pdf.exists() and preview_pdf.stat().st_size > 0:
                            try:
                                subprocess.run(
                                    [
                                        "/usr/bin/sips",
                                        "-s",
                                        "format",
                                        "png",
                                        str(preview_pdf),
                                        "--out",
                                        str(preview_png),
                                    ],
                                    check=True,
                                    capture_output=True,
                                    text=True,
                                    timeout=self.config.automation_timeout_seconds,
                                )
                            except (
                                OSError,
                                subprocess.CalledProcessError,
                                subprocess.TimeoutExpired,
                            ) as exc:
                                stderr = getattr(exc, "stderr", None)
                                detail = (
                                    stderr.strip()
                                    if isinstance(stderr, str)
                                    else str(exc)
                                )
                                raise ConversionError(
                                    f"CorelDRAW proof rendering failed: {detail}"
                                ) from exc
                            if preview_png.exists() and preview_png.stat().st_size > 0:
                                self._report_progress(
                                    "validated",
                                    88,
                                    "CDR 已重新打开并通过非空校验",
                                )
                                return
                last_size = current_size
            time.sleep(0.5)
        raise ConversionError(
            "CorelDRAW did not create a stable CDR before the timeout"
        )


def export_cdr_snapshot(
    source_cdr: Path,
    output_png: Path,
    config: ConverterConfig,
) -> Path:
    """Open an existing CDR and export its current page through CorelDRAW.

    Args:
        source_cdr: Existing CDR document to render.
        output_png: PNG destination for the rendered page.
        config: CorelDRAW app, script, and timeout settings.

    Returns:
        Resolved path to the stable PNG snapshot.

    Raises:
        ConversionError: If CorelDRAW cannot open or export the document.
    """
    if sys.platform != "darwin":
        raise ConversionError("CDR snapshots require macOS and CorelDRAW")

    source_cdr = source_cdr.expanduser().resolve()
    output_png = output_png.expanduser().resolve()
    if not source_cdr.is_file():
        raise ConversionError(f"CDR source does not exist: {source_cdr}")
    output_png.parent.mkdir(parents=True, exist_ok=True)
    proof_pdf = output_png.with_name(f".{output_png.stem}.corel-proof.pdf")
    output_png.unlink(missing_ok=True)
    proof_pdf.unlink(missing_ok=True)

    corel_scripts_root = Path(config.corel_scripts_root).expanduser()
    corel_scripts_root.mkdir(parents=True, exist_ok=True)
    snapshot_worker_name = config.corel_worker_name
    corel_worker = corel_scripts_root / f"{snapshot_worker_name}.js"
    corel_worker.write_text(
        build_corel_snapshot_javascript(source_cdr, proof_pdf),
        encoding="utf-8",
    )

    corel_selector = "-b" if "." in config.corel_app else "-a"
    corel_command = ["/usr/bin/open", corel_selector, config.corel_app]
    bootstrap_document = Path(config.corel_bootstrap_document).expanduser()
    if bootstrap_document.is_file():
        corel_command.append(str(bootstrap_document))
    subprocess.Popen(
        corel_command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + config.automation_timeout_seconds
    while time.monotonic() < deadline:
        process_check = subprocess.run(
            ["/usr/bin/pgrep", "-x", config.corel_process_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if process_check.returncode == 0:
            break
        time.sleep(1)
    else:
        raise ConversionError(
            f"CorelDRAW process did not start: {config.corel_process_name}"
        )

    time.sleep(config.corel_open_wait_seconds)
    run_corel_worker_with_accessibility(
        config.corel_process_name,
        snapshot_worker_name,
        config.corel_dialog_wait_seconds,
        proof_pdf,
    )

    last_size = -1
    stable_reads = 0
    while time.monotonic() < deadline:
        if proof_pdf.exists():
            current_size = proof_pdf.stat().st_size
            if current_size > 0:
                stable_reads = stable_reads + 1 if current_size == last_size else 0
                if stable_reads >= 2:
                    break
            last_size = current_size
        time.sleep(0.5)
    else:
        raise ConversionError(
            "CorelDRAW did not create a stable PDF proof before the timeout"
        )

    try:
        subprocess.run(
            [
                "/usr/bin/sips",
                "-s",
                "format",
                "png",
                str(proof_pdf),
                "--out",
                str(output_png),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=config.automation_timeout_seconds,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        stderr = getattr(exc, "stderr", None)
        detail = stderr.strip() if isinstance(stderr, str) else str(exc)
        raise ConversionError(f"CorelDRAW proof rendering failed: {detail}") from exc
    if not output_png.exists() or output_png.stat().st_size == 0:
        raise ConversionError("CorelDRAW proof rendering created no PNG")
    return output_png


def _unique_destination(directory: Path, name: str, digest: str) -> Path:
    """Return a non-overwriting artifact destination.

    Args:
        directory: Destination directory.
        name: Preferred artifact filename.
        digest: Source content digest used for deterministic disambiguation.

    Returns:
        An available path that never overwrites a prior job artifact.
    """
    preferred = directory / name
    if not preferred.exists():
        return preferred

    stem = Path(name).stem
    suffix = Path(name).suffix
    candidate = directory / f"{stem}-{digest[:10]}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{digest[:10]}-{counter}{suffix}"
        counter += 1
    return candidate


def _write_job_progress(
    progress_path: Path,
    *,
    job_id: str,
    source_name: str,
    started_at: str,
    status: str,
    stage: str,
    percentage: int,
    message: str,
    error: str | None = None,
) -> None:
    """Atomically publish one queue progress snapshot without blocking conversion.

    Args:
        progress_path: NAS JSON path watched by the designer app.
        job_id: Unique conversion job identifier.
        source_name: Uploaded AI filename.
        started_at: ISO-8601 job start time.
        status: Current job state.
        stage: Stable machine-readable conversion stage.
        percentage: Approximate current-file completion percentage.
        message: Short designer-facing stage description.
        error: Optional final failure detail.
    """
    try:
        started = datetime.fromisoformat(started_at)
        now = datetime.now(UTC)
        payload = {
            "job_id": job_id,
            "source_name": source_name,
            "status": status,
            "stage": stage,
            "percentage": max(0, min(100, percentage)),
            "message": message,
            "started_at": started_at,
            "updated_at": now.isoformat(),
            "elapsed_seconds": round((now - started).total_seconds(), 1),
            "error": error,
        }
        progress_upload = progress_path.with_name(f".{progress_path.name}.uploading")
        progress_upload.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        progress_upload.replace(progress_path)
    except OSError as exc:
        print(
            f"Progress update failed for {source_name}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def process_pending(
    root: Path,
    config: ConverterConfig,
    *,
    dry_run: bool = False,
    desktop_converter: DesktopConverter | None = None,
) -> list[JobResult]:
    """Process every stable AI file currently present in the inbox.

    Args:
        root: Initialized queue root.
        config: Validated converter configuration.
        dry_run: Discover and plan jobs without changing files or launching apps.
        desktop_converter: Optional injected converter used by tests or adapters.

    Returns:
        One result for every stable inbox item discovered during this pass.

    Raises:
        FileNotFoundError: If the queue has not been initialized.
    """
    root = root.expanduser().resolve()
    inbox = root / "inbox"
    if not inbox.is_dir():
        raise FileNotFoundError(f"queue is not initialized: {root}")
    # Existing installations may predate newly added queue directories. Repair
    # the directory layout on startup without changing config or queued files.
    for directory in QUEUE_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)

    now = time.time()
    discovered_sources: list[tuple[float, Path, Path | None]] = []
    for path in inbox.iterdir():
        try:
            source_mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if (
            not path.is_file()
            or path.suffix.casefold() != ".ai"
            or now - source_mtime < config.settle_seconds
        ):
            continue
        calibration_sidecar = path.with_suffix(".color.json")
        if calibration_sidecar.is_file():
            if now - calibration_sidecar.stat().st_mtime < config.settle_seconds:
                continue
            discovered_sources.append((source_mtime, path, calibration_sidecar))
        else:
            discovered_sources.append((source_mtime, path, None))
    sources = [
        (path, calibration_sidecar)
        for _, path, calibration_sidecar in sorted(
            discovered_sources,
            key=lambda item: (item[0], item[1].name),
        )
    ]
    engine = desktop_converter or MacOSDesktopConverter(config)
    results: list[JobResult] = []

    for source, source_sidecar in sources:
        digest = hashlib.sha256()
        with source.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(chunk)
        source_sha256 = digest.hexdigest()
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        job_id = f"{timestamp}-{source_sha256[:10]}"
        report_path = root / "reports" / f"{job_id}.json"

        if dry_run:
            results.append(
                JobResult(
                    job_id=job_id,
                    source_name=source.name,
                    status="planned",
                    report_path=report_path,
                    output_cdr=root / "outbox" / f"{source.stem}.cdr",
                )
            )
            continue

        started_at = datetime.now(UTC).isoformat()
        progress_path = root / "progress" / f"{job_id}.json"
        nas_job_dir = root / "processing" / job_id
        nas_job_dir.mkdir(parents=False)
        claimed_source = nas_job_dir / source.name
        claimed_sidecar: Path | None = None
        try:
            source.replace(claimed_source)
        except FileNotFoundError:
            nas_job_dir.rmdir()
            continue

        local_work_root = Path(config.local_work_root).expanduser().resolve()
        local_work_root.mkdir(parents=True, exist_ok=True)
        local_job_dir = local_work_root / job_id
        local_job_dir.mkdir(parents=False)
        work_source = local_job_dir / source.name
        bridge_pdf = local_job_dir / "bridge.pdf"
        work_cdr = local_job_dir / "output.cdr"
        work_preview = local_job_dir / "output.preview.png"
        upload_paths: list[Path] = []
        published_paths: list[Path] = []
        archived_source: Path | None = None
        archived_calibration: Path | None = None
        calibration: ColorCalibration | None = None
        calibration_audit: dict[str, object] | None = None

        try:
            _write_job_progress(
                progress_path,
                job_id=job_id,
                source_name=source.name,
                started_at=started_at,
                status="in_progress",
                stage="preparing",
                percentage=12,
                message="正在校验并复制 AI 到 mini4",
            )
            if source_sidecar is not None:
                claimed_sidecar = nas_job_dir / source_sidecar.name
                source_sidecar.replace(claimed_sidecar)
                calibration = load_color_calibration(
                    claimed_sidecar,
                    queue_root=root,
                )
                if calibration.source_name != source.name:
                    raise ValueError(
                        "color calibration source_name does not match the AI file"
                    )
            shutil.copy2(claimed_source, work_source)
            if claimed_sidecar is not None:
                shutil.copy2(claimed_sidecar, work_source.with_suffix(".color.json"))
            local_digest = hashlib.sha256()
            with work_source.open("rb") as local_source_file:
                for chunk in iter(lambda: local_source_file.read(1024 * 1024), b""):
                    local_digest.update(chunk)
            if local_digest.hexdigest() != source_sha256:
                raise ConversionError("local AI copy does not match the NAS source")

            _write_job_progress(
                progress_path,
                job_id=job_id,
                source_name=source.name,
                started_at=started_at,
                status="in_progress",
                stage="local_copy",
                percentage=24,
                message="AI 本地副本校验完成",
            )
            if isinstance(engine, MacOSDesktopConverter):
                engine.progress_callback = lambda stage, percentage, message: (
                    _write_job_progress(
                        progress_path,
                        job_id=job_id,
                        source_name=source.name,
                        started_at=started_at,
                        status="in_progress",
                        stage=stage,
                        percentage=percentage,
                        message=message,
                    )
                )
            try:
                engine.convert(work_source, bridge_pdf, work_cdr)
            finally:
                if isinstance(engine, MacOSDesktopConverter):
                    engine.progress_callback = None
            local_calibration_audit = local_job_dir / "color-calibration-audit.json"
            if local_calibration_audit.is_file():
                with local_calibration_audit.open(encoding="utf-8") as audit_file:
                    raw_audit = json.load(audit_file)
                if not isinstance(raw_audit, dict):
                    raise ConversionError(
                        "Illustrator color calibration audit is not a JSON object"
                    )
                calibration_audit = raw_audit
            if not work_cdr.exists():
                raise ConversionError("converter returned without a CDR file")
            if work_cdr.stat().st_size < config.minimum_cdr_bytes:
                raise ConversionError(
                    f"CDR is smaller than minimum_cdr_bytes: {work_cdr.stat().st_size}"
                )
            if not work_preview.exists() or work_preview.stat().st_size == 0:
                raise ConversionError("converter returned without a PNG preview")

            _write_job_progress(
                progress_path,
                job_id=job_id,
                source_name=source.name,
                started_at=started_at,
                status="in_progress",
                stage="publishing",
                percentage=92,
                message="正在把 CDR、预览和诊断文件上传到 NAS",
            )

            output_cdr = _unique_destination(
                root / "outbox", f"{source.stem}.cdr", source_sha256
            )
            archived_source = _unique_destination(
                root / "outbox" / "originals", source.name, source_sha256
            )
            if claimed_sidecar is not None:
                archived_calibration = _unique_destination(
                    root / "outbox" / "originals",
                    claimed_sidecar.name,
                    source_sha256,
                )
            published_preview = _unique_destination(
                root / "outbox" / "previews",
                f"{source.stem}.png",
                source_sha256,
            )
            published_bridge: Path | None = None

            output_upload = output_cdr.with_name(
                f".{output_cdr.name}.{job_id}.uploading"
            )
            upload_paths.append(output_upload)
            shutil.copy2(work_cdr, output_upload)
            if output_upload.stat().st_size != work_cdr.stat().st_size:
                raise ConversionError("CDR upload to NAS is incomplete")
            output_upload.replace(output_cdr)
            published_paths.append(output_cdr)

            preview_upload = published_preview.with_name(
                f".{published_preview.name}.{job_id}.uploading"
            )
            upload_paths.append(preview_upload)
            shutil.copy2(work_preview, preview_upload)
            if preview_upload.stat().st_size != work_preview.stat().st_size:
                raise ConversionError("PNG preview upload to NAS is incomplete")
            preview_upload.replace(published_preview)
            published_paths.append(published_preview)

            if config.preserve_bridge_pdf:
                published_bridge = _unique_destination(
                    root / "outbox" / "bridge_pdf",
                    f"{source.stem}.pdf",
                    source_sha256,
                )
                bridge_upload = published_bridge.with_name(
                    f".{published_bridge.name}.{job_id}.uploading"
                )
                upload_paths.append(bridge_upload)
                shutil.copy2(bridge_pdf, bridge_upload)
                if bridge_upload.stat().st_size != bridge_pdf.stat().st_size:
                    raise ConversionError("bridge PDF upload to NAS is incomplete")
                bridge_upload.replace(published_bridge)
                published_paths.append(published_bridge)

            claimed_source.replace(archived_source)
            if claimed_sidecar is not None and archived_calibration is not None:
                claimed_sidecar.replace(archived_calibration)

            report = {
                "job_id": job_id,
                "status": "succeeded",
                "source_name": source.name,
                "source_sha256": source_sha256,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "local_work_root": str(local_work_root),
                "archived_source": str(archived_source),
                "bridge_pdf": str(published_bridge) if published_bridge else None,
                "preview_png": str(published_preview),
                "output_cdr": str(output_cdr),
                "color_calibration": (
                    {
                        "sidecar": str(archived_calibration),
                        "mapping_count": len(calibration.mappings),
                        "icc_profile": calibration.icc_profile,
                        "icc_sha256": calibration.icc_sha256,
                        "icc_mode": "registered_only",
                        "audit": calibration_audit,
                    }
                    if calibration is not None
                    else None
                ),
                "error": None,
            }
            report_upload = report_path.with_name(f".{report_path.name}.uploading")
            upload_paths.append(report_upload)
            report_upload.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            report_upload.replace(report_path)
            _write_job_progress(
                progress_path,
                job_id=job_id,
                source_name=source.name,
                started_at=started_at,
                status="succeeded",
                stage="completed",
                percentage=100,
                message="CDR 已生成并通过重新打开校验",
            )
            shutil.rmtree(local_job_dir, ignore_errors=True)
            shutil.rmtree(nas_job_dir, ignore_errors=True)
            results.append(
                JobResult(
                    job_id=job_id,
                    source_name=source.name,
                    status="succeeded",
                    report_path=report_path,
                    output_cdr=output_cdr,
                )
            )
        except Exception as exc:
            for upload_path in upload_paths:
                upload_path.unlink(missing_ok=True)
            for published_path in published_paths:
                published_path.unlink(missing_ok=True)

            failed_job = root / "failed" / job_id
            quarantine_error: str | None = None
            try:
                if nas_job_dir.exists():
                    shutil.move(str(nas_job_dir), failed_job)
                else:
                    failed_job.mkdir(parents=False)
                if archived_source is not None and archived_source.exists():
                    archived_source.replace(failed_job / source.name)
                if archived_calibration is not None and archived_calibration.exists():
                    archived_calibration.replace(failed_job / archived_calibration.name)
                if source_sidecar is not None and source_sidecar.exists():
                    source_sidecar.replace(failed_job / source_sidecar.name)
                if local_job_dir.exists():
                    shutil.copytree(local_job_dir, failed_job / "local-work")
                    shutil.rmtree(local_job_dir)
            except Exception as quarantine_exc:
                quarantine_error = str(quarantine_exc)

            error = str(exc)
            if quarantine_error:
                error = (
                    f"{error}; quarantine failed: {quarantine_error}; "
                    f"local artifacts: {local_job_dir}"
                )
            report = {
                "job_id": job_id,
                "status": "failed",
                "source_name": source.name,
                "source_sha256": source_sha256,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "local_work_root": str(local_work_root),
                "failed_job": str(failed_job),
                "output_cdr": None,
                "color_calibration": (
                    {
                        "sidecar": str(
                            failed_job / claimed_sidecar.name
                            if claimed_sidecar is not None
                            else ""
                        ),
                        "mapping_count": len(calibration.mappings),
                        "icc_profile": calibration.icc_profile,
                        "icc_sha256": calibration.icc_sha256,
                        "icc_mode": "registered_only",
                        "audit": calibration_audit,
                    }
                    if calibration is not None
                    else None
                ),
                "error": error,
            }
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _write_job_progress(
                progress_path,
                job_id=job_id,
                source_name=source.name,
                started_at=started_at,
                status="failed",
                stage="failed",
                percentage=100,
                message="转换失败，请查看错误详情",
                error=error,
            )
            results.append(
                JobResult(
                    job_id=job_id,
                    source_name=source.name,
                    status="failed",
                    report_path=report_path,
                    error=error,
                )
            )
    return results


def queue_status(root: Path) -> dict[str, int]:
    """Count files and jobs in each queue state.

    Args:
        root: Initialized queue root.

    Returns:
        Counts suitable for JSON output or a health check.
    """
    root = root.expanduser().resolve()
    return {
        "inbox": len(list((root / "inbox").glob("*.ai"))),
        "processing": len(list((root / "processing").iterdir())),
        "outbox": len(list((root / "outbox").glob("*.cdr"))),
        "failed": len(list((root / "failed").iterdir())),
        "reports": len(list((root / "reports").glob("*.json"))),
    }


def build_launch_agent(
    root: Path,
    *,
    python_executable: Path | None = None,
    label: str = DEFAULT_SERVICE_LABEL,
) -> dict[str, object]:
    """Build a per-user launchd service for continuous conversion.

    Args:
        root: Initialized queue root watched by the service.
        python_executable: Python interpreter used to run this module.
        label: Unique launchd service label.

    Returns:
        Property-list dictionary suitable for ``~/Library/LaunchAgents``.
    """
    root = root.expanduser().resolve()
    executable = (python_executable or Path(sys.executable)).resolve()
    script = Path(__file__).resolve()
    program_arguments = [str(executable)]
    if not getattr(sys, "frozen", False):
        program_arguments.append(str(script))
    program_arguments.extend(["watch", "--root", str(root)])
    log_root = Path.home() / "Library" / "Logs" / "AI-CDR-Converter"
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_root / "service.stdout.log"),
        "StandardErrorPath": str(log_root / "service.stderr.log"),
    }


def install_launch_agent(
    root: Path,
    *,
    label: str = DEFAULT_SERVICE_LABEL,
) -> Path:
    """Install and start the per-user queue watcher.

    Args:
        root: Initialized queue root watched by the service.
        label: Unique launchd service label.

    Returns:
        Path to the installed launch-agent property list.

    Raises:
        subprocess.CalledProcessError: If launchd rejects the service.
    """
    root = root.expanduser().resolve()
    load_config(root)
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    (Path.home() / "Library" / "Logs" / "AI-CDR-Converter").mkdir(
        parents=True,
        exist_ok=True,
    )
    plist_path = launch_agents / f"{label}.plist"
    plist_path.write_bytes(plistlib.dumps(build_launch_agent(root, label=label)))

    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["/bin/launchctl", "bootout", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(plist_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return plist_path


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface.

    Returns:
        Configured argument parser for queue administration and conversion.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a conversion queue")
    init_parser.add_argument("--root", type=Path, required=True)
    init_parser.add_argument("--overwrite-config", action="store_true")

    once_parser = subparsers.add_parser("once", help="Process current inbox files")
    once_parser.add_argument("--root", type=Path, required=True)
    once_parser.add_argument("--dry-run", action="store_true")

    watch_parser = subparsers.add_parser("watch", help="Continuously watch the inbox")
    watch_parser.add_argument("--root", type=Path, required=True)

    service_parser = subparsers.add_parser(
        "install-service", help="Start the watcher automatically after login"
    )
    service_parser.add_argument("--root", type=Path, required=True)

    status_parser = subparsers.add_parser("status", help="Print queue counts as JSON")
    status_parser.add_argument("--root", type=Path, required=True)

    snapshot_parser = subparsers.add_parser(
        "snapshot", help="Render an existing CDR page to PNG with CorelDRAW"
    )
    snapshot_parser.add_argument("--source-cdr", type=Path, required=True)
    snapshot_parser.add_argument("--output-png", type=Path, required=True)
    snapshot_parser.add_argument("--root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the AI-to-CDR queue command.

    Args:
        argv: Optional command arguments excluding the executable name.

    Returns:
        Process exit code. Failed conversions return one.
    """
    args = build_parser().parse_args(argv)
    if args.command == "init":
        config_path = initialize_workspace(
            args.root,
            overwrite_config=args.overwrite_config,
        )
        print(config_path)
        return 0

    if args.command == "status":
        print(json.dumps(queue_status(args.root), ensure_ascii=False, indent=2))
        return 0

    if args.command == "install-service":
        print(install_launch_agent(args.root))
        return 0

    if args.command == "snapshot":
        config = load_config(args.root) if args.root else ConverterConfig()
        print(export_cdr_snapshot(args.source_cdr, args.output_png, config))
        return 0

    config = load_config(args.root)
    if args.command == "once":
        results = process_pending(args.root, config, dry_run=args.dry_run)
        for result in results:
            print(
                json.dumps(
                    {
                        "job_id": result.job_id,
                        "source_name": result.source_name,
                        "status": result.status,
                        "output_cdr": str(result.output_cdr)
                        if result.output_cdr
                        else None,
                        "error": result.error,
                    },
                    ensure_ascii=False,
                )
            )
        return 1 if any(result.status == "failed" for result in results) else 0

    try:
        while True:
            results = process_pending(args.root, config)
            for result in results:
                print(
                    json.dumps(
                        {
                            "job_id": result.job_id,
                            "source_name": result.source_name,
                            "status": result.status,
                            "output_cdr": str(result.output_cdr)
                            if result.output_cdr
                            else None,
                            "error": result.error,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
