"""Button platform for SOSSEN Direct.

Per inverter: read back the power limit it actually holds. HA otherwise shows
the last limit it set, which the inverter may have lost (night, power cut).
"""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID, DOMAIN, build_device_info
from .coordinator import SossenCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up one read-back button per inverter."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(SossenReadLimit(c) for c in runtime["coordinators"])


class SossenReadLimit(CoordinatorEntity, ButtonEntity):
    """Ask the inverter for its current power limit."""

    _attr_has_entity_name = True
    _attr_translation_key = "read_power_limit"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SossenCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_info_data[CONF_DEVICE_ID]}_read_power_limit"
        self._attr_device_info = build_device_info(coordinator.device_info_data)

    async def async_press(self) -> None:
        """Read the limit; the power limit number shows the answer."""
        if await self.coordinator.async_read_power_limit() is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="limit_not_answered"
            )
