import logging
import ssl
import sys
from pathlib import Path
from typing import Any

import aiohttp.connector as aiohttp_connector

from astrbot.utils.http_ssl_common import build_ssl_context_with_certifi

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DC_ENGINES_ROOT = PROJECT_ROOT / "dc_engines"


def _try_patch_aiohttp_ssl_context(
    ssl_context: ssl.SSLContext,
    log_obj: Any | None = None,
) -> bool:
    log = log_obj or logger
    attr_name = "_SSL_CONTEXT_VERIFIED"

    if not hasattr(aiohttp_connector, attr_name):
        log.warning(
            "aiohttp connector does not expose _SSL_CONTEXT_VERIFIED; skipped patch.",
        )
        return False

    current_value = getattr(aiohttp_connector, attr_name, None)
    if current_value is not None and not isinstance(current_value, ssl.SSLContext):
        log.warning(
            "aiohttp connector exposes _SSL_CONTEXT_VERIFIED with unexpected type; skipped patch.",
        )
        return False

    setattr(aiohttp_connector, attr_name, ssl_context)
    return True


def configure_runtime_ca_bundle(log_obj: Any | None = None) -> bool:
    log = log_obj or logger

    try:
        ssl_context = build_ssl_context_with_certifi(log_obj=log)
        return _try_patch_aiohttp_ssl_context(ssl_context, log_obj=log)
    except Exception as exc:
        log.error("Failed to configure runtime CA bundle for aiohttp: %r", exc)
        return False


def configure_dc_engines_path(log_obj: Any | None = None) -> bool:
    """Make the local dc_engines package importable for AstrBot plugins.

    AstrBot loads plugins from ``data/plugins`` and only guarantees the project
    root on ``sys.path``. The actual ``dc_engines`` package lives under
    ``./dc_engines/dc_engines``; without adding ``./dc_engines`` here, card
    plugins can silently fail to load after a restart.
    """
    log = log_obj or logger
    if not DC_ENGINES_ROOT.exists():
        log.warning("dc_engines root does not exist: %s", DC_ENGINES_ROOT)
        return False

    path = DC_ENGINES_ROOT.as_posix()
    if path not in sys.path:
        sys.path.insert(0, path)
    return True


def initialize_runtime_bootstrap(log_obj: Any | None = None) -> bool:
    path_ok = configure_dc_engines_path(log_obj=log_obj)
    ca_ok = configure_runtime_ca_bundle(log_obj=log_obj)
    return path_ok and ca_ok
