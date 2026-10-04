"""Diagnostics for SOSSEN Direct ("Download diagnostics"), keys redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_TERMINAL_ID,
    CONF_TOKEN_INFO,
    CONF_USER_CODE,
    DOMAIN,
)

TO_REDACT = {
    CONF_LOCAL_KEY,
    CONF_TOKEN_INFO,
    CONF_TERMINAL_ID,
    CONF_USER_CODE,
    "access_token",
    "refresh_token",
    "uid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the entry, its options and each inverter's live state."""
    result: dict[str, Any] = {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
    }
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return result
    discovery = runtime["discovery"]
    result["inverters"] = [
        {
            "device_id": dev_id,
            "address": discovery.get_ip(dev_id),
            "found_by": discovery.how(dev_id),
            "via_cloud": c.via_cloud,
            "powered_off": c.is_powered_off,
            "last_update_success": c.last_update_success,
            "power_limit": c.power_limit,
            "limit_commands": c.limit_commands,
            "data": {k: v for k, v in (c.data or {}).items() if k != "_raw"},
        }
        for c in runtime["coordinators"]
        for dev_id in [c.device_info_data[CONF_DEVICE_ID]]
    ]
    result["protection"] = {
        "enabled": runtime["protection"].enabled,
        "last_action": runtime["protection"].last_action,
    }
    return result
