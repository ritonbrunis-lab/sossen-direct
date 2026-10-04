"""SOSSEN Direct: local Home Assistant integration for SOSSEN microinverters."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .cloud import fetch_inverters
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_FORWARDED,
    CONF_IP_OVERRIDES,
    CONF_LOCAL_KEY,
    CONF_TOKEN_INFO,
    DOMAIN,
    PLATFORMS,
    PROTECTION_KEYS,
    keep_names,
    parse_addresses,
)
from .cloudlink import CloudLink
from .coordinator import SossenCoordinator
from .discovery import SossenDiscovery
from .protection import OvervoltageProtection

_LOGGER = logging.getLogger(__name__)


async def _async_refresh_keys(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-read the inverters and keys from the Smart Life account.

    Best effort: a re-paired inverter gets a new local key, and a new
    inverter added to the app shows up. If the cloud is unreachable, the
    stored keys are used as they are.
    """
    try:
        inverters, token = await hass.async_add_executor_job(
            fetch_inverters, dict(entry.data)
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Smart Life account unreachable, using stored keys: %s", err)
        return
    if not inverters:
        return
    stored = {d[CONF_DEVICE_ID]: d for d in entry.data[CONF_DEVICES]}
    fresh = {d[CONF_DEVICE_ID]: d for d in inverters}
    changed = token is not None or set(stored) != set(fresh) or any(
        stored.get(i, {}).get(CONF_LOCAL_KEY) != d[CONF_LOCAL_KEY]
        for i, d in fresh.items()
    )
    if changed:
        # Names chosen in Home Assistant win over the Smart Life ones.
        keep_names(inverters, entry.data[CONF_DEVICES])
        data = {**entry.data, CONF_DEVICES: inverters}
        if token:
            data[CONF_TOKEN_INFO] = token
        hass.config_entries.async_update_entry(entry, data=data)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SOSSEN Direct from a config entry."""
    await _async_refresh_keys(hass, entry)
    devices = entry.data[CONF_DEVICES]

    discovery = SossenDiscovery(
        hass,
        {d[CONF_DEVICE_ID]: d[CONF_LOCAL_KEY] for d in devices},
        entry.options.get(CONF_IP_OVERRIDES),
        parse_addresses(entry.options.get(CONF_FORWARDED)),
    )
    await discovery.async_start()

    # Cloud fallback for inverters the LAN cannot reach (second router, NAT).
    cloud: CloudLink | None = CloudLink(
        hass, entry, [d[CONF_DEVICE_ID] for d in devices]
    )
    try:
        await hass.async_add_executor_job(cloud.start)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Smart Life cloud fallback unavailable: %s", err)
        cloud = None

    coordinators = [
        SossenCoordinator(hass, entry, device, discovery, cloud) for device in devices
    ]
    await asyncio.gather(
        *(c.async_config_entry_first_refresh() for c in coordinators)
    )

    protection = OvervoltageProtection(hass, entry, coordinators)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "discovery": discovery,
        "cloud": cloud,
        "coordinators": coordinators,
        "protection": protection,
        "options": _session_options(entry),
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    protection.async_apply_options()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    _LOGGER.info("SOSSEN Direct set up with %d inverters", len(coordinators))
    return True


def _session_options(entry: ConfigEntry) -> dict:
    """Options that need new inverter sessions (addresses), not protection."""
    return {k: v for k, v in entry.options.items() if k not in PROTECTION_KEYS}


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the address options change.

    The listener also fires on data updates (saved power limit, refreshed
    token, names) and protection settings, which must not restart the
    inverter sessions: the protection only re-reads its settings.
    """
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not runtime:
        return
    if runtime["options"] != _session_options(entry):
        await hass.config_entries.async_reload(entry.entry_id)
        return
    runtime["protection"].async_apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime = hass.data[DOMAIN].pop(entry.entry_id)
        runtime["protection"].async_stop()
        for coordinator in runtime["coordinators"]:
            await coordinator.async_shutdown()
        await runtime["discovery"].async_stop()
        if runtime["cloud"] is not None:
            await hass.async_add_executor_job(runtime["cloud"].stop)
    return unload_ok
