"""Config flow for SOSSEN Direct: Smart Life QR login, no keys to type."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .cloud import QrLogin, fetch_inverters
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_IP_OVERRIDES,
    CONF_NAME,
    CONF_USER_CODE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


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


class SossenDirectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Log in to the Smart Life account and import every SOSSEN inverter."""

    VERSION = 1

    def __init__(self) -> None:
        self._login = QrLogin()
        self._account: dict[str, Any] = {}
        self._inverters: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the Smart Life user code."""
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
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the inverters found and create the entry."""
        uid = self._account["token_info"]["uid"]
        await self.async_set_unique_id(uid)
        self._abort_if_unique_id_configured(
            updates={**self._account, CONF_DEVICES: self._inverters}
        )
        if user_input is None:
            names = "\n".join(f"- {d[CONF_NAME]}" for d in self._inverters)
            return self.async_show_form(
                step_id="confirm",
                description_placeholders={
                    "count": str(len(self._inverters)),
                    "names": names,
                },
            )
        data = {k: v for k, v in self._account.items() if k != "username"}
        data[CONF_DEVICES] = self._inverters
        return self.async_create_entry(
            title=f"SOSSEN ({self._account.get('username') or uid})", data=data
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        """Options: optional fixed IP per inverter."""
        return SossenDirectOptionsFlow()


class SossenDirectOptionsFlow(config_entries.OptionsFlow):
    """Let the user pin an inverter to a fixed IP (blank = automatic)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        devices = self.config_entry.data[CONF_DEVICES]
        current = self.config_entry.options.get(CONF_IP_OVERRIDES, {})
        if user_input is not None:
            overrides = {
                d[CONF_DEVICE_ID]: user_input.get(d[CONF_DEVICE_ID], "").strip()
                for d in devices
            }
            return self.async_create_entry(
                data={CONF_IP_OVERRIDES: {k: v for k, v in overrides.items() if v}}
            )
        schema = {
            vol.Optional(
                d[CONF_DEVICE_ID],
                description={"suggested_value": current.get(d[CONF_DEVICE_ID], "")},
            ): str
            for d in devices
        }
        names = "\n".join(f"- {d[CONF_NAME]} : `{d[CONF_DEVICE_ID]}`" for d in devices)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            description_placeholders={"names": names},
        )
