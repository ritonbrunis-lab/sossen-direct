"""Smart Life account access: fetch the inverters and their local keys.

The cloud is used only to learn each inverter's Device ID and local key
(at setup, and again at each start so a re-paired inverter heals). All
data then flows over the LAN, never through the cloud.
"""

from __future__ import annotations

import logging
from typing import Any

from tuya_sharing import LoginControl, Manager, SharingTokenListener

from .const import (
    CONF_DEVICE_ID,
    CONF_ENDPOINT,
    CONF_LOCAL_KEY,
    CONF_MODEL,
    CONF_NAME,
    CONF_TERMINAL_ID,
    CONF_TOKEN_INFO,
    CONF_USER_CODE,
    TUYA_CLIENT_ID,
    TUYA_SCHEMA,
    model_from_product,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_KEYS = ("t", "uid", "expire_time", "access_token", "refresh_token")


def is_sossen(device: Any) -> bool:
    """Return True if a Tuya device looks like a SOSSEN microinverter."""
    text = f"{getattr(device, 'name', '')} {getattr(device, 'product_name', '')}"
    return "sossen" in text.lower()


def device_entry(device: Any) -> dict:
    """Turn a Tuya CustomerDevice into the dict stored in the config entry."""
    return {
        CONF_DEVICE_ID: device.id,
        CONF_NAME: device.name,
        CONF_LOCAL_KEY: device.local_key,
        CONF_MODEL: model_from_product(getattr(device, "product_name", "")),
        "product_name": getattr(device, "product_name", ""),
    }


class QrLogin:
    """QR-code login against the Smart Life app (blocking, run in executor)."""

    def __init__(self) -> None:
        self._control = LoginControl()
        self.user_code: str | None = None
        self.qr_code: str | None = None

    def request_qr(self, user_code: str) -> tuple[bool, dict]:
        """Ask Tuya for a QR code bound to this user code."""
        response = self._control.qr_code(TUYA_CLIENT_ID, TUYA_SCHEMA, user_code)
        if response.get("success"):
            self.user_code = user_code
            self.qr_code = response["result"]["qrcode"]
            return True, response
        return False, response

    def result(self) -> tuple[bool, dict]:
        """Return (True, entry data) once the QR code has been scanned."""
        ok, info = self._control.login_result(
            self.qr_code, TUYA_CLIENT_ID, self.user_code
        )
        if not ok:
            return False, info
        return True, {
            CONF_USER_CODE: self.user_code,
            CONF_TOKEN_INFO: {k: info[k] for k in TOKEN_KEYS},
            CONF_TERMINAL_ID: info[CONF_TERMINAL_ID],
            CONF_ENDPOINT: info[CONF_ENDPOINT],
            "username": info.get("username"),
        }


class _TokenListener(SharingTokenListener):
    """Collect refreshed tokens so they can be saved to the config entry."""

    def __init__(self) -> None:
        self.token_info: dict | None = None

    def update_token(self, token_info: dict[str, Any]) -> None:
        self.token_info = {k: token_info[k] for k in TOKEN_KEYS if k in token_info}


def fetch_inverters(account: dict) -> tuple[list[dict], dict | None]:
    """Return (SOSSEN inverters, refreshed token or None). Blocking."""
    listener = _TokenListener()
    manager = Manager(
        TUYA_CLIENT_ID,
        account[CONF_USER_CODE],
        account[CONF_TERMINAL_ID],
        account[CONF_ENDPOINT],
        account[CONF_TOKEN_INFO],
        listener,
    )
    manager.update_device_cache()
    inverters = [
        device_entry(dev) for dev in manager.device_map.values() if is_sossen(dev)
    ]
    _LOGGER.debug(
        "Smart Life account has %d devices, %d SOSSEN inverters",
        len(manager.device_map), len(inverters),
    )
    return inverters, listener.token_info
