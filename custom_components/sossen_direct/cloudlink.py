"""Read the inverters through the Smart Life cloud when the LAN cannot.

Fallback for inverters that Home Assistant cannot reach locally (for
example behind a second Wi-Fi router doing NAT). The inverter reports its
data frame (DP 21, same SOSSEN payload as on the LAN) to the Tuya cloud;
it is received here by MQTT push, with a REST poll as a safety net. The
same DP 19 "arm" the app sends is sent through the cloud so the inverter
keeps reporting at its fast cadence.

The cloud names datapoints by code, not by number, and the codes of this
product are not documented, so payloads are recognised by content: a raw
value that decodes to a SOSSEN data frame is the data DP, one that holds a
power-limit record is the command DP 24.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from tuya_sharing import Manager, SharingTokenListener

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    ARM_DP19_DP,
    ARM_DP19_VALUE,
    CONF_ENDPOINT,
    CONF_TERMINAL_ID,
    CONF_TOKEN_INFO,
    CONF_USER_CODE,
    DP_SET_POWER_LIMIT,
    TUYA_CLIENT_ID,
    TUYA_DP_COMMAND,
)
from .protocol import build_set_power_payload, decode_payload, decode_records

_LOGGER = logging.getLogger(__name__)

TOKEN_KEYS = ("t", "uid", "expire_time", "access_token", "refresh_token")
# REST safety-net poll of every inverter's last reported status.
CLOUD_POLL_EVERY = 60
# Re-send the DP 19 arm through the cloud when no frame came for this long.
CLOUD_ARM_AFTER = 120
# A cloud frame older than this is no longer shown as current data.
CLOUD_STALE_AFTER = 900


class _TokenSaver(SharingTokenListener):
    """Persist refreshed cloud tokens in the config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    def update_token(self, token_info: dict[str, Any]) -> None:
        token = {k: token_info[k] for k in TOKEN_KEYS if k in token_info}
        self._hass.add_job(self._save, token)

    @callback
    def _save(self, token: dict) -> None:
        self._hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_TOKEN_INFO: token}
        )


class CloudLink:
    """Shared Smart Life session for all inverters of a config entry."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device_ids: list[str]
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._ids = set(device_ids)
        self._manager: Manager | None = None
        self._lock = threading.Lock()
        # device id -> (decoded frame, monotonic time received)
        self._frames: dict[str, tuple[dict, float]] = {}
        self._limits: dict[str, int] = {}
        self._online: dict[str, bool] = {}
        # device id -> {dp id: cloud code}
        self._codes: dict[str, dict[int, str]] = {}
        self._last_poll = 0.0
        self._last_arm: dict[str, float] = {}

    # --- lifecycle (blocking, run in the executor) -------------------------

    def start(self) -> None:
        """Open the cloud session and the MQTT push channel."""
        data = self._entry.data
        manager = Manager(
            TUYA_CLIENT_ID,
            data[CONF_USER_CODE],
            data[CONF_TERMINAL_ID],
            data[CONF_ENDPOINT],
            data[CONF_TOKEN_INFO],
            _TokenSaver(self._hass, self._entry),
        )
        manager.update_device_cache()
        # Subscribe to the inverters only, not the rest of the account.
        for dev_id in list(manager.device_map):
            if dev_id not in self._ids:
                del manager.device_map[dev_id]
        for dev_id, device in manager.device_map.items():
            device.set_up = True
            self._online[dev_id] = bool(getattr(device, "online", False))
            codes = {
                int(dp): info["status_code"]
                for dp, info in (device.local_strategy or {}).items()
                if info.get("status_code")
            }
            self._codes[dev_id] = codes
            self._absorb(dev_id, [{"code": c, "value": v} for c, v in device.status.items()])
            _LOGGER.info(
                "Cloud: inverter %s online=%s, dp codes %s, status codes %s, "
                "functions %s",
                dev_id, self._online[dev_id], codes, sorted(device.status),
                sorted(device.function),
            )
        self._manager = manager
        self._last_poll = time.monotonic()
        manager.refresh_mq()
        if manager.mq is not None:
            manager.mq.add_message_listener(self._on_message)

    def stop(self) -> None:
        """Close the MQTT channel."""
        if self._manager is not None and self._manager.mq is not None:
            self._manager.mq.stop()
        self._manager = None

    # --- incoming data ------------------------------------------------------

    def _on_message(self, msg: dict) -> None:
        """MQTT push from the cloud (paho thread)."""
        data = msg.get("data") or {}
        dev_id = data.get("devId")
        if dev_id in self._ids and isinstance(data.get("status"), list):
            self._absorb(dev_id, data["status"])

    def _absorb(self, dev_id: str, items: list[dict]) -> None:
        """Keep the SOSSEN frames and power limit found in status items."""
        for item in items:
            value = item.get("value")
            if not isinstance(value, str) or len(value) < 8:
                continue
            frame = decode_payload(value) if len(value) > 20 else None
            if frame is not None:
                with self._lock:
                    self._frames[dev_id] = (frame, time.monotonic())
                    self._online[dev_id] = True
                _LOGGER.debug(
                    "Cloud frame from %s: AC power=%sW", dev_id, frame.get("ac_power_w")
                )
                continue
            records = decode_records(value)
            if DP_SET_POWER_LIMIT in records:
                self._limits[dev_id] = records[DP_SET_POWER_LIMIT]
                if "code" in item:
                    self._codes.setdefault(dev_id, {}).setdefault(
                        TUYA_DP_COMMAND, item["code"]
                    )

    def poll(self) -> None:
        """REST poll of the last reported status, at most once a minute."""
        if self._manager is None:
            return
        now = time.monotonic()
        if now - self._last_poll < CLOUD_POLL_EVERY:
            return
        self._last_poll = now
        response = self._manager.customer_api.get(
            "/v1.0/m/life/ha/devices/detail", {"devIds": ",".join(self._ids)}
        )
        if not response.get("success"):
            _LOGGER.debug("Cloud status poll failed: %s", response.get("msg"))
            return
        for item in response.get("result") or []:
            dev_id = item.get("id")
            if dev_id not in self._ids:
                continue
            self._online[dev_id] = bool(item.get("online"))
            self._absorb(dev_id, item.get("status") or [])

    def arm_if_quiet(self, dev_id: str) -> None:
        """Send the DP 19 arm through the cloud when frames stopped."""
        if self._manager is None or not self._online.get(dev_id):
            return
        now = time.monotonic()
        frame = self._frames.get(dev_id)
        if frame and now - frame[1] < CLOUD_ARM_AFTER:
            return
        if now - self._last_arm.get(dev_id, 0.0) < CLOUD_ARM_AFTER:
            return
        self._last_arm[dev_id] = now
        code = self._codes.get(dev_id, {}).get(ARM_DP19_DP)
        if code is None:
            return
        _LOGGER.debug("Cloud: arming %s (%s=%s)", dev_id, code, ARM_DP19_VALUE)
        self._manager.send_commands(dev_id, [{"code": code, "value": ARM_DP19_VALUE}])

    # --- outgoing -----------------------------------------------------------

    def set_power_limit(self, dev_id: str, watts: int) -> bool:
        """Send the power-limit command through the cloud."""
        code = self._codes.get(dev_id, {}).get(TUYA_DP_COMMAND)
        if self._manager is None or code is None:
            _LOGGER.error(
                "Cloud: no command code known for %s, cannot set the power limit",
                dev_id,
            )
            return False
        response = self._manager.customer_api.post(
            f"/v1.1/m/thing/{dev_id}/commands",
            None,
            {"commands": [{"code": code, "value": build_set_power_payload(watts)}]},
        )
        if not response.get("success"):
            _LOGGER.error("Cloud: power limit refused: %s", response.get("msg"))
            return False
        return True

    # --- read side (event loop) ---------------------------------------------

    def frame(self, dev_id: str) -> dict | None:
        """Return the latest fresh frame of an inverter, if any."""
        with self._lock:
            entry = self._frames.get(dev_id)
        if entry is None or time.monotonic() - entry[1] > CLOUD_STALE_AFTER:
            return None
        return entry[0]

    def online(self, dev_id: str) -> bool:
        """Return the cloud's online flag for an inverter."""
        return self._online.get(dev_id, False)

    def power_limit(self, dev_id: str) -> int | None:
        """Return the power limit last reported through the cloud."""
        return self._limits.get(dev_id)
