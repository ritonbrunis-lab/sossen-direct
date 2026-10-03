"""Smoke tests: config flow, setup with a fake inverter, UDP discovery."""

import base64
import json
from unittest.mock import patch

import pytest
import tinytuya
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.sossen_direct.const import DOMAIN
from custom_components.sossen_direct.discovery import SossenDiscovery

INVERTERS = [
    {"device_id": "bff6c75c727ebcdeaezdwt", "name": "SOSSEN-2in1-FR 2",
     "local_key": "k1k1k1k1k1k1k1k1", "model": "2in1", "product_name": "SOSSEN-2in1-FR"},
    {"device_id": "bf73c00c73c6b6f5f8oirs", "name": "SOSSEN-2in1-FR 3",
     "local_key": "k2k2k2k2k2k2k2k2", "model": "2in1", "product_name": "SOSSEN-2in1-FR"},
]
ACCOUNT = {
    "user_code": "abc",
    "token_info": {"t": 1, "uid": "uid1", "expire_time": 7200,
                   "access_token": "a", "refresh_token": "r"},
    "terminal_id": "term", "endpoint": "https://apigw.tuyaeu.com",
}


def _frame() -> str:
    def rec(dp, v):
        return bytes([1, 1, dp >> 8, dp & 255, v >> 8, v & 255])
    data = bytes([3, 1]) + rec(4096, 3) + rec(4098, 12345) + rec(4103, 2301) + rec(4126, 650)
    return base64.b64encode(data).decode()


class FakeDevice:
    def __init__(self, dev_id, ip, key, version=3.5):
        self.ip = ip

    def set_socketTimeout(self, t): pass
    def set_socketPersistent(self, p): pass
    def heartbeat(self, nowait=False): pass
    def set_value(self, *a, **k): return {}
    def updatedps(self, *a): pass
    def close(self): pass

    def receive(self):
        return {"dps": {"21": _frame()}}


async def test_config_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    flow = hass.config_entries.flow._progress[result["flow_id"]]
    with patch.object(flow._login, "request_qr", return_value=(True, {})) as rq:
        flow._login.qr_code = "QRTOKEN"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"user_code": "abc"})
    assert rq.called and result["step_id"] == "scan"

    with patch.object(flow._login, "result", return_value=(True, {**ACCOUNT, "username": "eric"})), \
         patch("custom_components.sossen_direct.config_flow.fetch_inverters",
               return_value=(INVERTERS, None)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "confirm"
    assert "SOSSEN-2in1-FR 3" in result["description_placeholders"]["names"]

    with patch("custom_components.sossen_direct.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["devices"] == INVERTERS
    assert "username" not in result["data"]


async def test_setup_reads_inverters(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={**ACCOUNT, "devices": INVERTERS},
                            unique_id="uid1")
    entry.add_to_hass(hass)
    with patch("custom_components.sossen_direct.fetch_inverters",
               side_effect=RuntimeError("offline")), \
         patch.object(SossenDiscovery, "async_start", return_value=None), \
         patch.object(SossenDiscovery, "get_ip", return_value="192.168.1.50"), \
         patch("custom_components.sossen_direct.coordinator.tinytuya.Device", FakeDevice), \
         patch("custom_components.sossen_direct.const.LISTEN_WINDOW", 0.05), \
         patch("custom_components.sossen_direct.coordinator.LISTEN_WINDOW", 0.05):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        powers = [s for s in hass.states.async_all("sensor") if s.entity_id.endswith("ac_power")]
        assert len(powers) == 2, [s.entity_id for s in hass.states.async_all("sensor")]
        assert all(s.state == "650.0" for s in powers)
        energy = [s for s in hass.states.async_all("sensor") if "total_energy" in s.entity_id]
        assert energy and energy[0].state == "1234.5"
        assert len(hass.states.async_all("number")) == 2
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_udp_announcement_sets_ip(hass):
    disc = SossenDiscovery(hass, {d["device_id"]: d["local_key"] for d in INVERTERS})
    body = json.dumps({"ip": "192.168.1.77", "gwId": "bf73c00c73c6b6f5f8oirs",
                       "version": "3.5"}).encode()
    msg = tinytuya.TuyaMessage(0, 0x13, None, body, 0, True, tinytuya.PREFIX_6699_VALUE, True)
    disc._on_packet(tinytuya.pack_message(msg, hmac_key=tinytuya.udpkey), "192.168.1.77")
    assert disc.get_ip("bf73c00c73c6b6f5f8oirs") == "192.168.1.77"
    assert disc.get_ip("bff6c75c727ebcdeaezdwt") is None
    disc._on_packet(b"garbage", "1.2.3.4")


async def test_override_wins(hass):
    disc = SossenDiscovery(hass, {"bf73c00c73c6b6f5f8oirs": "k"},
                           {"bf73c00c73c6b6f5f8oirs": "10.0.0.9"})
    body = json.dumps({"ip": "192.168.1.77", "gwId": "bf73c00c73c6b6f5f8oirs"}).encode()
    msg = tinytuya.TuyaMessage(0, 0x13, None, body, 0, True, tinytuya.PREFIX_6699_VALUE, True)
    disc._on_packet(tinytuya.pack_message(msg, hmac_key=tinytuya.udpkey), "192.168.1.77")
    assert disc.get_ip("bf73c00c73c6b6f5f8oirs") == "10.0.0.9"


async def test_listeners_start_and_stop(hass, socket_enabled):
    disc = SossenDiscovery(hass, {"bf73c00c73c6b6f5f8oirs": "k"})
    with patch.object(SossenDiscovery, "_loop", return_value=None):
        await disc.async_start()
    assert len(disc._transports) == 3
    await disc.async_stop()


async def test_sweep_matches_by_handshake(hass, socket_enabled):
    import asyncio
    import ipaddress

    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    disc = SossenDiscovery(hass, {"bf73c00c73c6b6f5f8oirs": "k"})

    async def nets():
        return [("127.0.0.2", ipaddress.IPv4Network("127.0.0.0/30"))]

    with patch.object(disc, "_ipv4_networks", nets), \
         patch("custom_components.sossen_direct.discovery.TUYA_PORT", port), \
         patch("custom_components.sossen_direct.discovery._handshake_ok",
               side_effect=lambda dev, key, ip: ip == "127.0.0.1"):
        await disc.async_sweep()
    server.close()
    assert disc.get_ip("bf73c00c73c6b6f5f8oirs") == "127.0.0.1"
