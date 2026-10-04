"""Config flow for SOSSEN Direct: Smart Life QR login, no keys to type.

New install: user code -> QR scan -> names -> network check (progress, then
a menu when an inverter is missing) -> overvoltage protection -> entry.
Options: a menu with a status checklist, protection, power limits, network
addresses and names.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, selector

from .cloud import QrLogin, fetch_inverters
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_FORWARDED,
    CONF_HIGH_VOLTAGE,
    CONF_INTERVAL,
    CONF_IP_OVERRIDES,
    CONF_LOCAL_KEY,
    CONF_LOW_VOLTAGE,
    CONF_MAX_LIMIT,
    CONF_MIN_LIMIT,
    CONF_NAME,
    CONF_PROTECTION,
    CONF_STEP_DOWN,
    CONF_STEP_UP,
    CONF_USER_CODE,
    DOMAIN,
    LOCATE_TIMEOUT,
    POWER_LIMIT_MIN,
    POWER_LIMIT_STEP,
    keep_names,
    parse_addresses,
)
from .discovery import SossenDiscovery
from .protection import (
    async_runtime_texts,
    format_volts,
    is_up,
    protection_errors,
    protection_settings,
)

_LOGGER = logging.getLogger(__name__)

# Form field holding the limit applied to every inverter at once.
ALL_INVERTERS = "all"


def _qr_schema(qr_code: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional("QR"): selector.QrCodeSelector(
                config=selector.QrCodeSelectorConfig(
                    data=f"tuyaSmart--qrLogin?token={qr_code}",
                    scale=5,
                    error_correction_level=selector.QrErrorCorrectionLevel.QUARTILE,
                )
            )
        }
    )


def _labels(devices: list[dict]) -> dict[str, str]:
    """Form field per inverter: its name (the label shown), made unique."""
    names = [d[CONF_NAME] for d in devices]
    return {
        d[CONF_DEVICE_ID]: d[CONF_NAME]
        if names.count(d[CONF_NAME]) == 1
        else f"{d[CONF_NAME]} ({d[CONF_DEVICE_ID][-4:]})"
        for d in devices
    }


def _number(unit: str, low: float, high: float, step: float) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=low,
            max=high,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _protection_schema(values: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PROTECTION, default=values[CONF_PROTECTION]): bool,
            vol.Required(CONF_HIGH_VOLTAGE, default=values[CONF_HIGH_VOLTAGE]): _number(
                "V", 230, 260, 0.5
            ),
            vol.Required(CONF_LOW_VOLTAGE, default=values[CONF_LOW_VOLTAGE]): _number(
                "V", 220, 260, 0.5
            ),
            vol.Required(CONF_STEP_DOWN, default=values[CONF_STEP_DOWN]): _number(
                "W", 10, 500, 10
            ),
            vol.Required(CONF_STEP_UP, default=values[CONF_STEP_UP]): _number(
                "W", 10, 500, 10
            ),
            vol.Required(CONF_MIN_LIMIT, default=values[CONF_MIN_LIMIT]): _number(
                "W", POWER_LIMIT_MIN, 2400, 10
            ),
            vol.Required(CONF_MAX_LIMIT, default=values[CONF_MAX_LIMIT]): _number(
                "W", POWER_LIMIT_MIN, 2400, 10
            ),
            vol.Required(CONF_INTERVAL, default=values[CONF_INTERVAL]): _number(
                "s", 30, 900, 10
            ),
        }
    )


def _validate_protection(user_input: dict) -> tuple[dict, dict[str, str]]:
    """Return the typed settings and the form errors."""
    values = {
        CONF_PROTECTION: bool(user_input[CONF_PROTECTION]),
        CONF_HIGH_VOLTAGE: float(user_input[CONF_HIGH_VOLTAGE]),
        CONF_LOW_VOLTAGE: float(user_input[CONF_LOW_VOLTAGE]),
        CONF_STEP_DOWN: int(user_input[CONF_STEP_DOWN]),
        CONF_STEP_UP: int(user_input[CONF_STEP_UP]),
        CONF_MIN_LIMIT: int(user_input[CONF_MIN_LIMIT]),
        CONF_MAX_LIMIT: int(user_input[CONF_MAX_LIMIT]),
        CONF_INTERVAL: int(user_input[CONF_INTERVAL]),
    }
    return values, protection_errors(values)


def _addresses_schema(devices: list[dict], forwarded: str, manual: dict) -> vol.Schema:
    """Forwarded addresses, then an optional fixed address per inverter."""
    schema: dict = {
        vol.Optional(CONF_FORWARDED, description={"suggested_value": forwarded}): str
    }
    for dev_id, label in _labels(devices).items():
        schema[
            vol.Optional(label, description={"suggested_value": manual.get(dev_id, "")})
        ] = str
    return vol.Schema(schema)


def _read_manual(devices: list[dict], user_input: dict) -> dict[str, str]:
    """Device ID -> fixed address, from the per-inverter fields."""
    manual = {
        dev_id: (user_input.get(label) or "").strip()
        for dev_id, label in _labels(devices).items()
    }
    return {k: v for k, v in manual.items() if v}


def _located_lines(devices: list[dict], located: dict[str, str]) -> str:
    """One line per inverter: ✓ name — address, or ✗ name."""
    lines = []
    for dev_id, label in _labels(devices).items():
        address = located.get(dev_id)
        lines.append(f"✓ **{label}** — `{address}`" if address else f"✗ **{label}**")
    return "\n\n".join(lines)


async def _async_locate(
    hass: HomeAssistant, devices: list[dict], forwarded: str, manual: dict[str, str]
) -> dict[str, str]:
    """Look for every inverter; manual addresses are checked by the key handshake."""
    discovery = SossenDiscovery(
        hass,
        {d[CONF_DEVICE_ID]: d[CONF_LOCAL_KEY] for d in devices},
        None,
        parse_addresses(forwarded) + list(manual.values()),
    )
    return await discovery.async_locate(LOCATE_TIMEOUT)


class SossenDirectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Log in to the Smart Life account and import every SOSSEN inverter."""

    VERSION = 1

    def __init__(self) -> None:
        self._login = QrLogin()
        self._account: dict[str, Any] = {}
        self._inverters: list[dict] = []
        self._forwarded = ""
        self._manual: dict[str, str] = {}
        self._located: dict[str, str] = {}
        self._locate_task: asyncio.Task | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Welcome, then ask for the Smart Life user code."""
        errors: dict[str, str] = {}
        placeholders = {"msg": ""}
        if user_input is not None:
            ok, response = await self.hass.async_add_executor_job(
                self._login.request_qr, user_input[CONF_USER_CODE].strip()
            )
            if ok:
                return await self.async_step_scan()
            errors["base"] = "login_error"
            placeholders["msg"] = str(response.get("msg", ""))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_USER_CODE): str}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the QR code; on submit, check the scan and list inverters."""
        if user_input is None:
            return self.async_show_form(
                step_id="scan", data_schema=_qr_schema(self._login.qr_code)
            )

        ok, account = await self.hass.async_add_executor_job(self._login.result)
        if not ok:
            await self.hass.async_add_executor_job(
                self._login.request_qr, self._login.user_code
            )
            return self.async_show_form(
                step_id="scan",
                data_schema=_qr_schema(self._login.qr_code),
                errors={"base": "scan_error"},
            )
        self._account = account
        try:
            self._inverters, token = await self.hass.async_add_executor_job(
                fetch_inverters, account
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not list the Smart Life devices")
            return self.async_abort(reason="cloud_error")
        if token:
            self._account["token_info"] = token
        if not self._inverters:
            return self.async_abort(reason="no_inverters")

        # Same account again: refresh its inverters and keys, nothing else.
        uid = self._account["token_info"]["uid"]
        if entry := self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, uid
        ):
            keep_names(self._inverters, entry.data.get(CONF_DEVICES, []))
        await self.async_set_unique_id(uid)
        self._abort_if_unique_id_configured(
            updates={**self._account, CONF_DEVICES: self._inverters}
        )
        return await self.async_step_names()

    async def async_step_names(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the inverters found and let the user rename them."""
        labels = _labels(self._inverters)
        if user_input is not None:
            for device in self._inverters:
                name = (user_input.get(labels[device[CONF_DEVICE_ID]]) or "").strip()
                if name:
                    device[CONF_NAME] = name
            return await self.async_step_network()
        return self.async_show_form(
            step_id="names",
            data_schema=vol.Schema(
                {
                    vol.Required(labels[d[CONF_DEVICE_ID]], default=d[CONF_NAME]): str
                    for d in self._inverters
                }
            ),
            description_placeholders={"count": str(len(self._inverters))},
        )

    async def async_step_network(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Look for the inverters on the network (up to ~45 s)."""
        if self._locate_task is None:
            self._locate_task = self.hass.async_create_task(
                _async_locate(self.hass, self._inverters, self._forwarded, self._manual)
            )
        if not self._locate_task.done():
            return self.async_show_progress(
                step_id="network",
                progress_action="locate",
                progress_task=self._locate_task,
                description_placeholders={"seconds": str(LOCATE_TIMEOUT)},
            )
        try:
            self._located = self._locate_task.result()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Inverter search failed")
            self._located = {}
        self._locate_task = None
        return self.async_show_progress_done(next_step_id="network_result")

    async def async_step_network_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """All found: say where. Some missing: offer the ways to reach them."""
        if all(d[CONF_DEVICE_ID] in self._located for d in self._inverters):
            return await self.async_step_network_ok()
        return await self.async_step_network_missing()

    async def async_step_network_missing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu: behind another router, manual addresses, or later."""
        return self.async_show_menu(
            step_id="network_missing",
            menu_options=["behind_router", "manual", "later"],
            description_placeholders=self._located_placeholders(),
        )

    def _located_placeholders(self) -> dict[str, str]:
        found = sum(d[CONF_DEVICE_ID] in self._located for d in self._inverters)
        return {
            "lines": _located_lines(self._inverters, self._located),
            "found": str(found),
            "count": str(len(self._inverters)),
        }

    async def async_step_network_ok(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Every inverter answered: show where, then go on."""
        if user_input is not None:
            return await self.async_step_protection()
        return self.async_show_form(
            step_id="network_ok", description_placeholders=self._located_placeholders()
        )

    async def async_step_behind_router(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Inverters behind a second router: forwarded addresses, then re-test."""
        if user_input is not None:
            self._forwarded = (user_input.get(CONF_FORWARDED) or "").strip()
            return await self.async_step_network()
        return self.async_show_form(
            step_id="behind_router",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_FORWARDED, description={"suggested_value": self._forwarded}
                    ): str
                }
            ),
            description_placeholders=self._located_placeholders(),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fixed address per inverter (IP or IP:port), then re-test."""
        if user_input is not None:
            self._manual = _read_manual(self._inverters, user_input)
            return await self.async_step_network()
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        label,
                        description={
                            "suggested_value": self._manual.get(dev_id)
                            or self._located.get(dev_id, "")
                        },
                    ): str
                    for dev_id, label in _labels(self._inverters).items()
                }
            ),
            description_placeholders=self._located_placeholders(),
        )

    async def async_step_later(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Go on: discovery keeps searching once the integration runs."""
        return await self.async_step_protection()

    async def async_step_protection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Built-in overvoltage protection (on by default for a new install)."""
        errors: dict[str, str] = {}
        values = {**protection_settings({}), CONF_PROTECTION: True}
        if user_input is not None:
            values, errors = _validate_protection(user_input)
            if not errors:
                return self._create_entry(values)
        return self.async_show_form(
            step_id="protection",
            data_schema=_protection_schema(values),
            errors=errors,
        )

    def _create_entry(self, protection: dict) -> ConfigFlowResult:
        uid = self._account["token_info"]["uid"]
        data = {k: v for k, v in self._account.items() if k != "username"}
        data[CONF_DEVICES] = self._inverters
        options = {
            CONF_IP_OVERRIDES: self._manual,
            CONF_FORWARDED: self._forwarded,
            **protection,
        }
        return self.async_create_entry(
            title=f"SOSSEN ({self._account.get('username') or uid})",
            data=data,
            options=options,
        )

    @callback
    def async_remove(self) -> None:
        """Stop a search still running when the dialog is closed."""
        if self._locate_task is not None:
            self._locate_task.cancel()

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        """Options: status, protection, power, network, names."""
        return SossenDirectOptionsFlow()


class SossenDirectOptionsFlow(config_entries.OptionsFlow):
    """Menu of everything that can change after setup."""

    @property
    def _devices(self) -> list[dict]:
        return self.config_entry.data[CONF_DEVICES]

    @property
    def _runtime(self) -> dict | None:
        return self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)

    def _save(self, **changes: Any) -> ConfigFlowResult:
        """Store the options, keeping the ones this step does not touch."""
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["status", "protection", "power", "network", "names"],
        )

    async def async_step_status(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read-only diagnostic checklist; submit goes back to the menu."""
        if user_input is not None:
            return await self.async_step_init()
        texts = await async_runtime_texts(self.hass)
        return self.async_show_form(
            step_id="status",
            description_placeholders={"checklist": self._checklist(texts)},
        )

    def _checklist(self, t: dict[str, str]) -> str:
        """✓ / ! lines: Smart Life keys, each inverter, protection."""
        lines = []
        labels = _labels(self._devices)
        no_key = [
            labels[d[CONF_DEVICE_ID]] for d in self._devices if not d.get(CONF_LOCAL_KEY)
        ]
        if no_key:
            lines.append("! " + t.get("keys_missing", "{names}").format(names=", ".join(no_key)))
        else:
            lines.append("✓ " + t.get("keys_ok", "{count}").format(count=len(self._devices)))
        runtime = self._runtime
        if runtime is None:
            lines.append("! " + t.get("not_loaded", ""))
            return "\n\n".join(lines)

        discovery = runtime["discovery"]
        for coordinator in runtime["coordinators"]:
            dev_id = coordinator.device_info_data[CONF_DEVICE_ID]
            how = "cloud" if coordinator.via_cloud else discovery.how(dev_id)
            local = how in ("lan", "forwarded", "manual")
            parts = [f"**{labels.get(dev_id, dev_id)}**"]
            if local:
                parts.append(f"`{discovery.get_ip(dev_id)}` ({t.get(how, how)})")
            else:
                parts.append(t.get(how or "not_found", how or ""))
            data = coordinator.data or {}
            if is_up(coordinator):
                parts.append(
                    t.get("producing", "{power} W").format(
                        power=round(data.get("ac_power_w") or 0),
                        voltage=format_volts(self.hass, data["ac_voltage_v"]),
                    )
                )
                if data.get("temperature_c") is not None:
                    parts.append(f"{data['temperature_c']:.0f} °C")
            else:
                parts.append(t.get("off", "off"))
            parts.append(
                t.get("limit", "{limit} W").format(
                    limit=coordinator.power_limit, count=coordinator.limit_commands
                )
            )
            lines.append(("✓ " if local else "! ") + " · ".join(parts))

        cfg = protection_settings(self.config_entry.options)
        if cfg[CONF_PROTECTION]:
            text = t.get("protection_on", "").format(
                high=format_volts(self.hass, cfg[CONF_HIGH_VOLTAGE]),
                low=format_volts(self.hass, cfg[CONF_LOW_VOLTAGE]),
            )
            if action := runtime["protection"].last_action:
                text += f" — {action}"
            lines.append("✓ " + text)
        else:
            lines.append("! " + t.get("protection_off", ""))
        return "\n\n".join(lines)

    async def async_step_protection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Overvoltage protection settings (applied without a reload)."""
        errors: dict[str, str] = {}
        values = protection_settings(self.config_entry.options)
        if user_input is not None:
            values, errors = _validate_protection(user_input)
            if not errors:
                return self._save(**values)
        return self.async_show_form(
            step_id="protection",
            data_schema=_protection_schema(values),
            errors=errors,
        )

    async def async_step_power(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the power limit of every inverter at once, or of each one."""
        runtime = self._runtime
        if runtime is None:
            return self.async_abort(reason="not_loaded")
        coordinators = {
            c.device_info_data[CONF_DEVICE_ID]: c for c in runtime["coordinators"]
        }
        labels = _labels(self._devices)
        if user_input is not None:
            same = user_input.get(ALL_INVERTERS)
            for dev_id, coordinator in coordinators.items():
                watts = same if same is not None else user_input.get(labels.get(dev_id))
                if watts is None:
                    continue
                watts = min(int(watts), coordinator.model["power_limit_max"])
                if watts != coordinator.power_limit:
                    await coordinator.async_set_power_limit(watts)
            return self._save()
        highest = max(c.model["power_limit_max"] for c in coordinators.values())
        field = _number("W", POWER_LIMIT_MIN, highest, POWER_LIMIT_STEP)
        schema: dict = {vol.Optional(ALL_INVERTERS): field}
        for dev_id, coordinator in coordinators.items():
            schema[
                vol.Optional(
                    labels.get(dev_id, dev_id),
                    description={"suggested_value": coordinator.power_limit},
                )
            ] = field
        return self.async_show_form(step_id="power", data_schema=vol.Schema(schema))

    async def async_step_network(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Forwarded addresses and fixed addresses (blank = automatic)."""
        options = self.config_entry.options
        if user_input is not None:
            return self._save(
                **{
                    CONF_IP_OVERRIDES: _read_manual(self._devices, user_input),
                    CONF_FORWARDED: (user_input.get(CONF_FORWARDED) or "").strip(),
                }
            )
        return self.async_show_form(
            step_id="network",
            data_schema=_addresses_schema(
                self._devices,
                options.get(CONF_FORWARDED, ""),
                options.get(CONF_IP_OVERRIDES, {}),
            ),
        )

    async def async_step_names(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rename the inverters (devices renamed in place, no reload)."""
        labels = _labels(self._devices)
        if user_input is not None:
            registry = dr.async_get(self.hass)
            devices = []
            for device in self._devices:
                name = (user_input.get(labels[device[CONF_DEVICE_ID]]) or "").strip()
                devices.append({**device, CONF_NAME: name or device[CONF_NAME]})
                entry = registry.async_get_device(
                    identifiers={(DOMAIN, device[CONF_DEVICE_ID])}
                )
                if entry is not None and name:
                    registry.async_update_device(entry.id, name=name)
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_DEVICES: devices}
            )
            return self._save()
        return self.async_show_form(
            step_id="names",
            data_schema=vol.Schema(
                {
                    vol.Required(labels[d[CONF_DEVICE_ID]], default=d[CONF_NAME]): str
                    for d in self._devices
                }
            ),
        )
