"""Number platform for SOSSEN Direct.

Per inverter: its power limit. On the account-level device: the overvoltage
protection settings, adjustable from a dashboard (stored in the options).
"""

from dataclasses import dataclass
import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_ID,
    CONF_HIGH_VOLTAGE,
    CONF_INTERVAL,
    CONF_LOW_VOLTAGE,
    CONF_MAX_LIMIT,
    CONF_MIN_LIMIT,
    CONF_STEP_DOWN,
    CONF_STEP_UP,
    DOMAIN,
    POWER_LIMIT_MIN,
    POWER_LIMIT_STEP,
    build_device_info,
    build_hub_info,
)
from .coordinator import SossenCoordinator
from .protection import OvervoltageProtection

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the power limits and the protection settings."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        SossenPowerLimit(coordinator, entry) for coordinator in runtime["coordinators"]
    ]
    entities += [
        SossenProtectionNumber(runtime["protection"], entry, setting)
        for setting in PROTECTION_NUMBERS
    ]
    async_add_entities(entities)


@dataclass(frozen=True)
class ProtectionNumber:
    """One protection setting shown as a number (key = option key)."""

    key: str
    unit: str
    low: float
    high: float
    step: float
    icon: str
    mode: NumberMode = NumberMode.BOX


SLIDER = NumberMode.SLIDER
PROTECTION_NUMBERS = (
    ProtectionNumber(CONF_HIGH_VOLTAGE, "V", 235, 260, 0.5, "mdi:flash-alert"),
    ProtectionNumber(CONF_LOW_VOLTAGE, "V", 230, 255, 0.5, "mdi:flash-outline"),
    ProtectionNumber(CONF_STEP_DOWN, "W", 10, 300, 10, "mdi:arrow-down-bold", SLIDER),
    ProtectionNumber(CONF_STEP_UP, "W", 10, 300, 10, "mdi:arrow-up-bold", SLIDER),
    ProtectionNumber(CONF_MIN_LIMIT, "W", 500, 1000, 10, "mdi:arrow-collapse-down"),
    ProtectionNumber(CONF_MAX_LIMIT, "W", 500, 1000, 10, "mdi:arrow-collapse-up"),
    ProtectionNumber(CONF_INTERVAL, "s", 30, 900, 30, "mdi:timer-outline", SLIDER),
)


class SossenPowerLimit(CoordinatorEntity, NumberEntity):
    """Number entity to set the inverter power limit."""

    _attr_has_entity_name = True
    _attr_translation_key = "power_limit"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_icon = "mdi:transmission-tower"
    _attr_native_min_value = POWER_LIMIT_MIN
    _attr_native_step = POWER_LIMIT_STEP
    _attr_native_unit_of_measurement = "W"
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: SossenCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_info_data[CONF_DEVICE_ID]}_power_limit"
        self._attr_native_max_value = coordinator.model["power_limit_max"]
        self._attr_device_info = build_device_info(coordinator.device_info_data)

    @property
    def native_value(self) -> float | None:
        """Return the current power limit."""
        return self.coordinator.power_limit

    async def async_set_native_value(self, value: float) -> None:
        """Set the power limit."""
        await self.coordinator.async_set_power_limit(int(value))
        self.async_write_ha_state()


class SossenProtectionNumber(NumberEntity):
    """One overvoltage protection setting, on the account-level device.

    The value lives in the entry options (same as the options flow); a change
    is applied by the update listener without reloading the integration.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(
        self,
        protection: OvervoltageProtection,
        entry: ConfigEntry,
        setting: ProtectionNumber,
    ) -> None:
        self._protection = protection
        self._key = setting.key
        self._attr_translation_key = f"protection_{setting.key}"
        self._attr_unique_id = f"{entry.entry_id}_protection_{setting.key}"
        self._attr_device_info = build_hub_info(entry.entry_id)
        self._attr_icon = setting.icon
        self._attr_mode = setting.mode
        self._attr_native_unit_of_measurement = setting.unit
        self._attr_native_min_value = setting.low
        self._attr_native_max_value = setting.high
        self._attr_native_step = setting.step
        if setting.unit == "V":
            self._attr_device_class = NumberDeviceClass.VOLTAGE
        elif setting.unit == "W":
            self._attr_device_class = NumberDeviceClass.POWER
        else:
            self._attr_device_class = NumberDeviceClass.DURATION

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._protection.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self._protection.settings[self._key]

    async def async_set_native_value(self, value: float) -> None:
        """Store the setting; low >= high or min > max is refused."""
        volts = self._attr_native_unit_of_measurement == "V"
        self._protection.async_set_setting(self._key, float(value) if volts else int(value))
