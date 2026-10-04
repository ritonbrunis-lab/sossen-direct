"""Switch platform for SOSSEN Direct (overvoltage protection on/off)."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_PROTECTION, DOMAIN, build_hub_info
from .protection import OvervoltageProtection


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the protection switch on the account-level device."""
    protection = hass.data[DOMAIN][entry.entry_id]["protection"]
    async_add_entities([SossenProtectionSwitch(protection, entry)])


class SossenProtectionSwitch(SwitchEntity):
    """Turn the built-in overvoltage protection on or off.

    The state lives in the entry options, so it survives restarts and the
    options flow shows the same value.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "overvoltage_protection"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_should_poll = False

    def __init__(self, protection: OvervoltageProtection, entry: ConfigEntry) -> None:
        self._protection = protection
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_overvoltage_protection"
        self._attr_device_info = build_hub_info(entry.entry_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._protection.async_add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._protection.enabled

    async def _async_set(self, on: bool) -> None:
        # The update listener restarts the controller without a reload.
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_PROTECTION: on}
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
