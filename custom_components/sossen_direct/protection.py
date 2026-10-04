"""Built-in overvoltage protection: lower the limits before the grid trips.

The inverters disconnect from the grid around 253 V, and the voltage they
measure rises with what they inject (~3.5 V per kW). Every interval, the
highest AC voltage among the inverters that are up decides: at or above the
high threshold every limit goes down one step (step down), at or below the
low threshold it goes back up one smaller step (step up), in between nothing
changes: down fast, up slowly, so it does not oscillate. A command is sent only
when a limit actually changes (the inverter may store each one in flash).

Replaces the external automation that did the same; options without the
protection keys (installs older than 0.5.0) keep it disabled, and the single
step of 0.5.0 (CONF_LIMIT_STEP) still applies to both directions until the
two steps are set.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.translation import async_get_translations

from .const import (
    CONF_HIGH_VOLTAGE,
    CONF_INTERVAL,
    CONF_LIMIT_STEP,
    CONF_LOW_VOLTAGE,
    CONF_MAX_LIMIT,
    CONF_MIN_LIMIT,
    CONF_PROTECTION,
    CONF_STEP_DOWN,
    CONF_STEP_UP,
    DOMAIN,
    PROTECTION_DEFAULTS,
)
from .coordinator import SossenCoordinator

_LOGGER = logging.getLogger(__name__)


def protection_settings(options: dict) -> dict:
    """Return the protection settings, defaults filled in.

    Options saved by 0.5.0 have one CONF_LIMIT_STEP: it stands for both steps.
    """
    defaults = dict(PROTECTION_DEFAULTS)
    if (legacy := options.get(CONF_LIMIT_STEP)) is not None:
        defaults[CONF_STEP_DOWN] = defaults[CONF_STEP_UP] = legacy
    return {key: options.get(key, default) for key, default in defaults.items()}


def protection_errors(values: dict) -> dict[str, str]:
    """Inconsistent settings, as {field: translation key}."""
    errors: dict[str, str] = {}
    if values[CONF_LOW_VOLTAGE] >= values[CONF_HIGH_VOLTAGE]:
        errors[CONF_LOW_VOLTAGE] = "low_above_high"
    if values[CONF_MIN_LIMIT] > values[CONF_MAX_LIMIT]:
        errors[CONF_MIN_LIMIT] = "min_above_max"
    return errors


def is_up(coordinator: SossenCoordinator) -> bool:
    """True when the inverter reports a live frame (producing or in alarm)."""
    data = coordinator.data or {}
    return bool(data.get("status")) and data.get("ac_voltage_v") is not None


async def async_runtime_texts(hass: HomeAssistant) -> dict[str, str]:
    """Return the "runtime" texts (selector translations) in HA's language."""
    prefix = f"component.{DOMAIN}.selector.runtime.options."
    strings = await async_get_translations(
        hass, hass.config.language, "selector", {DOMAIN}
    )
    return {k[len(prefix):]: v for k, v in strings.items() if k.startswith(prefix)}


def format_volts(hass: HomeAssistant, volts: float) -> str:
    """One decimal, with a decimal comma in French."""
    text = f"{volts:.1f}"
    return text.replace(".", ",") if hass.config.language.startswith("fr") else text


class OvervoltageProtection:
    """Entry-level controller acting on every inverter's power limit."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinators: list[SossenCoordinator],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinators = coordinators
        self.last_action: str | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._interval: int | None = None
        self._listeners: list[Callable[[], None]] = []

    @property
    def settings(self) -> dict:
        return protection_settings(self.entry.options)

    @property
    def enabled(self) -> bool:
        return bool(self.settings[CONF_PROTECTION])

    @callback
    def async_set_setting(self, key: str, value: float | bool) -> None:
        """Store one setting in the options (dashboard number entities).

        The update listener then applies it without reloading the entry.
        """
        values = {**self.settings, key: value}
        if errors := protection_errors(values):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=next(iter(errors.values())),
            )
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, key: value}
        )

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        """Call update whenever the state or the last action changes."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def async_apply_options(self) -> None:
        """(Re)start or stop the timer after the options changed."""
        interval = int(self.settings[CONF_INTERVAL])
        if not self.enabled:
            self.async_stop()
        elif self._unsub_timer is None or interval != self._interval:
            self.async_stop()
            self._interval = interval
            self._unsub_timer = async_track_time_interval(
                self.hass, self.async_check, timedelta(seconds=interval)
            )
            _LOGGER.info("Overvoltage protection active, every %ss", interval)
        self._notify()

    @callback
    def async_stop(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    async def async_check(self, _now=None) -> None:
        """One regulation step."""
        if not self.enabled:
            return
        cfg = self.settings
        up = [c for c in self.coordinators if is_up(c)]
        if not up:
            return
        volts = max(c.data["ac_voltage_v"] for c in up)
        if volts >= cfg[CONF_HIGH_VOLTAGE]:
            action, delta = "lowered", -int(cfg[CONF_STEP_DOWN])
        elif volts <= cfg[CONF_LOW_VOLTAGE]:
            action, delta = "raised", int(cfg[CONF_STEP_UP])
        else:
            return
        changed: list[int] = []
        for coordinator in up:
            highest = min(int(cfg[CONF_MAX_LIMIT]), coordinator.model["power_limit_max"])
            current = coordinator.power_limit or highest
            target = max(int(cfg[CONF_MIN_LIMIT]), min(highest, current + delta))
            if target != current:
                await coordinator.async_set_power_limit(target)
                changed.append(target)
        if not changed:
            return
        _LOGGER.info("Overvoltage protection: %s to %s W at %.1f V", action, changed, volts)
        texts = await async_runtime_texts(self.hass)
        template = texts.get(action, "{limit} W ({voltage} V)")
        self.last_action = template.format(
            limit=max(changed), voltage=format_volts(self.hass, volts)
        )
        self._notify()
