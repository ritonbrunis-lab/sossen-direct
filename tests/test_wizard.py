"""Setup wizard steps, options menu and the overvoltage protection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.sossen_direct.const import DOMAIN
from custom_components.sossen_direct.discovery import SossenDiscovery
from custom_components.sossen_direct.protection import OvervoltageProtection

from .test_integration import ACCOUNT, INVERTERS, FakeDevice, FakeManager, finish_progress

ID1, ID2 = INVERTERS[0]["device_id"], INVERTERS[1]["device_id"]
PROTECTION = {"protection": True, "high_voltage": 249, "low_voltage": 245,
              "step_down": 100, "step_up": 100, "min_limit": 500, "max_limit": 1000, "interval": 120}


async def _flow_at_names(hass):
    """Run the login steps and return the flow at the names step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    with patch.object(flow._login, "request_qr", return_value=(True, {})):
        flow._login.qr_code = "QR"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"user_code": "abc"})
    with patch.object(flow._login, "result", return_value=(True, dict(ACCOUNT))), \
         patch("custom_components.sossen_direct.config_flow.fetch_inverters",
               return_value=([dict(d) for d in INVERTERS], None)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "names"
    return result


async def test_missing_inverter_menu_then_manual(hass):
    result = await _flow_at_names(hass)
    release = asyncio.Event()
    answers = [{ID1: "192.168.1.50"}, {ID1: "192.168.1.50", ID2: "192.168.1.77"}]
    seen = []

    async def locate(self, timeout):
        seen.append(list(self._forwarded))
        await release.wait()
        return answers.pop(0)

    with patch.object(SossenDiscovery, "async_locate", locate):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "locate"
        release.set()
        result = await finish_progress(hass, result)
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "network_missing"
        assert result["menu_options"] == ["behind_router", "manual", "later"]
        assert result["description_placeholders"]["found"] == "1"
        assert "✗ **SOSSEN-2in1-FR 3**" in result["description_placeholders"]["lines"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "manual"})
        assert result["step_id"] == "manual"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"SOSSEN-2in1-FR 3": "192.168.1.77"})
        result = await finish_progress(hass, result)
    # The manual address is checked by the handshake like a forwarded one.
    assert seen[1] == ["192.168.1.77"]
    assert result["step_id"] == "network_ok"
    assert "`192.168.1.77`" in result["description_placeholders"]["lines"]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    with patch("custom_components.sossen_direct.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], PROTECTION)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["ip_overrides"] == {ID2: "192.168.1.77"}


async def test_later_and_protection_validation(hass):
    result = await _flow_at_names(hass)
    with patch.object(SossenDiscovery, "async_locate", return_value={}):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await finish_progress(hass, result)
    assert result["step_id"] == "network_missing"
    assert result["description_placeholders"]["found"] == "0"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "behind_router"})
    assert result["step_id"] == "behind_router"
    with patch.object(SossenDiscovery, "async_locate", return_value={}):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"forwarded": "192.168.1.41:6668, 192.168.1.41:6669"})
        result = await finish_progress(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "later"})
    assert result["step_id"] == "protection"
    defaults = {k: v.default() for k, v in
                ((key.schema, key) for key in result["data_schema"].schema)}
    assert defaults["protection"] is True and defaults["high_voltage"] == 249.0

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**PROTECTION, "low_voltage": 250})
    assert result["errors"] == {"low_voltage": "low_above_high"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**PROTECTION, "min_limit": 900, "max_limit": 800})
    assert result["errors"] == {"min_limit": "min_above_max"}
    with patch("custom_components.sossen_direct.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**PROTECTION, "protection": False, "step_down": 70, "step_up": 30})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["forwarded"] == "192.168.1.41:6668, 192.168.1.41:6669"
    assert result["options"]["protection"] is False
    assert (result["options"]["step_down"], result["options"]["step_up"]) == (70, 30)


async def test_same_account_updates_and_keeps_names(hass):
    stored = [dict(INVERTERS[0], name="Garage"), dict(INVERTERS[1])]
    entry = MockConfigEntry(domain=DOMAIN, data={**ACCOUNT, "devices": stored},
                            unique_id="uid1")
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    with patch.object(flow._login, "request_qr", return_value=(True, {})):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"user_code": "abc"})
    fresh = [dict(d, local_key="newkeynewkeynewk") for d in INVERTERS]
    with patch.object(flow._login, "result", return_value=(True, dict(ACCOUNT))), \
         patch("custom_components.sossen_direct.config_flow.fetch_inverters",
               return_value=(fresh, None)), \
         patch("custom_components.sossen_direct.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["devices"][0]["name"] == "Garage"
    assert entry.data["devices"][0]["local_key"] == "newkeynewkeynewk"


# --- Options menu (integration loaded with fake inverters) ----------------

def _setup_patches():
    return (
        patch("custom_components.sossen_direct.fetch_inverters",
              side_effect=RuntimeError("offline")),
        patch.object(SossenDiscovery, "async_start", return_value=None),
        patch.object(SossenDiscovery, "get_ip", return_value="192.168.1.50"),
        patch("custom_components.sossen_direct.cloudlink.Manager", FakeManager),
        patch("custom_components.sossen_direct.coordinator.tinytuya.Device", FakeDevice),
        patch("custom_components.sossen_direct.coordinator.LISTEN_WINDOW", 0.05),
    )


async def _loaded_entry(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data={**ACCOUNT, "devices": INVERTERS},
                            options=options or {}, unique_id="uid1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_options_menu(hass):
    p = _setup_patches()
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        entry = await _loaded_entry(hass, {"forwarded": "192.168.1.41:6669"})
        runtime = hass.data[DOMAIN][entry.entry_id]
        # Installs older than 0.5.0: protection off, its switch too.
        assert not runtime["protection"].enabled
        switch = hass.states.async_all("switch")
        assert len(switch) == 1 and switch[0].state == "off"

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        assert result["menu_options"] == ["status", "protection", "power", "network", "names"]

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "status"})
        checklist = result["description_placeholders"]["checklist"]
        assert checklist.startswith("✓ Smart Life keys present for 2")
        assert "`192.168.1.50` (local network)" in checklist
        assert "producing 650 W at 230.1 V" in checklist
        assert "! Overvoltage protection off" in checklist
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.MENU

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "protection"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**PROTECTION, "high_voltage": 245})
        assert result["errors"] == {"low_voltage": "low_above_high"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], PROTECTION)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        # Protection settings apply without restarting the inverter sessions.
        assert hass.data[DOMAIN][entry.entry_id] is runtime
        assert runtime["protection"].enabled and runtime["protection"]._unsub_timer
        assert entry.options["forwarded"] == "192.168.1.41:6669"
        assert hass.states.async_all("switch")[0].state == "on"

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "power"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"all": 800})
        await hass.async_block_till_done()
        assert [c.power_limit for c in runtime["coordinators"]] == [800, 800]
        assert [c.limit_commands for c in runtime["coordinators"]] == [1, 1]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "power"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"SOSSEN-2in1-FR 2": 800, "SOSSEN-2in1-FR 3": 700})
        await hass.async_block_till_done()
        assert [c.limit_commands for c in runtime["coordinators"]] == [1, 2]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "names"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"SOSSEN-2in1-FR 2": "Garage", "SOSSEN-2in1-FR 3": "Jardin"})
        await hass.async_block_till_done()
        assert [d["name"] for d in entry.data["devices"]] == ["Garage", "Jardin"]
        from homeassistant.helpers import device_registry as dr
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, ID1)})
        assert device.name == "Garage"

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "network"})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"forwarded": "", "Jardin": "192.168.1.77:6670"})
        await hass.async_block_till_done()
        assert entry.options["ip_overrides"] == {ID2: "192.168.1.77:6670"}
        assert entry.options["forwarded"] == "" and entry.options["protection"] is True
        # Addresses changed: the entry was reloaded.
        assert hass.data[DOMAIN][entry.entry_id] is not runtime
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_protection_switch(hass):
    p = _setup_patches()
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        entry = await _loaded_entry(hass)
        runtime = hass.data[DOMAIN][entry.entry_id]
        switch = hass.states.async_all("switch")[0].entity_id
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch}, blocking=True)
        await hass.async_block_till_done()
        assert entry.options["protection"] is True
        assert hass.states.get(switch).state == "on"
        assert hass.data[DOMAIN][entry.entry_id] is runtime
        assert runtime["protection"]._unsub_timer is not None
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": switch}, blocking=True)
        await hass.async_block_till_done()
        assert hass.states.get(switch).state == "off"
        assert runtime["protection"]._unsub_timer is None
        assert await hass.config_entries.async_unload(entry.entry_id)


# --- Protection controller logic ----------------------------------------

def _inverter(volts, limit, status=3, max_w=1000):
    async def set_limit(watts):
        inv.power_limit = watts

    inv = SimpleNamespace(
        data={"status": status, "ac_voltage_v": volts} if status else {"status": 0,
                                                                        "ac_voltage_v": None},
        power_limit=limit, model={"power_limit_max": max_w},
    )
    inv.async_set_power_limit = AsyncMock(side_effect=set_limit)
    return inv


def _protection(hass, inverters, **options):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={**PROTECTION, **options})
    return OvervoltageProtection(hass, entry, inverters)


async def test_protection_lowers_at_high_voltage(hass):
    a, b = _inverter(249.6, 1000), _inverter(247.0, 900)
    ctl = _protection(hass, [a, b])
    await ctl.async_check()
    assert (a.power_limit, b.power_limit) == (900, 800)
    assert ctl.last_action == "Lowered to 900 W (249.6 V)"


async def test_protection_raises_at_low_voltage_and_clamps(hass):
    a, b = _inverter(244.0, 950), _inverter(240.0, 700)
    ctl = _protection(hass, [a, b])
    await ctl.async_check()
    assert (a.power_limit, b.power_limit) == (1000, 800)
    assert ctl.last_action.startswith("Raised to 1000 W")


async def test_protection_clamps_at_min_and_skips_unchanged(hass):
    a, b = _inverter(251.0, 550), _inverter(250.0, 500)
    ctl = _protection(hass, [a, b])
    await ctl.async_check()
    assert a.power_limit == 500
    b.async_set_power_limit.assert_not_called()
    a.async_set_power_limit.reset_mock()
    await ctl.async_check()
    a.async_set_power_limit.assert_not_called()


async def test_protection_idle_cases(hass):
    # Between thresholds: nothing.
    a = _inverter(247.0, 800)
    await _protection(hass, [a]).async_check()
    a.async_set_power_limit.assert_not_called()
    # Nobody producing: nothing, even with a stale high value elsewhere.
    off = _inverter(None, 800, status=0)
    ctl = _protection(hass, [off])
    await ctl.async_check()
    off.async_set_power_limit.assert_not_called()
    assert ctl.last_action is None
    # Disabled: nothing.
    hot = _inverter(252.0, 800)
    await _protection(hass, [hot], protection=False).async_check()
    hot.async_set_power_limit.assert_not_called()


async def test_protection_ignores_inverters_that_are_off(hass):
    on, off = _inverter(250.0, 800), _inverter(None, 1000, status=0)
    await _protection(hass, [on, off]).async_check()
    assert on.power_limit == 700
    off.async_set_power_limit.assert_not_called()


async def test_protection_french_text(hass):
    hass.config.language = "fr"
    a = _inverter(249.6, 900)
    ctl = _protection(hass, [a])
    await ctl.async_check()
    assert ctl.last_action == "Bridé à 800 W (249,6 V)"


async def test_protection_steps_down_fast_up_slowly(hass):
    ctl = _protection(hass, [], step_down=70, step_up=30)
    hot, cool = _inverter(249.5, 1000), _inverter(244.0, 800)
    ctl.coordinators = [hot]
    await ctl.async_check()
    assert hot.power_limit == 930
    ctl.coordinators = [cool]
    await ctl.async_check()
    assert cool.power_limit == 830


async def test_protection_legacy_single_step(hass):
    from custom_components.sossen_direct.protection import protection_settings

    # Fresh defaults: 70 W down, 30 W up.
    assert (protection_settings({})["step_down"], protection_settings({})["step_up"]) == (70, 30)
    # Options saved by 0.5.0: the single step stands for both directions.
    legacy = {k: v for k, v in PROTECTION.items() if k not in ("step_down", "step_up")}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={**legacy, "limit_step": 50})
    hot, cool = _inverter(250.0, 900), _inverter(240.0, 700)
    ctl = OvervoltageProtection(hass, entry, [hot])
    assert (ctl.settings["step_down"], ctl.settings["step_up"]) == (50, 50)
    await ctl.async_check()
    ctl.coordinators = [cool]
    await ctl.async_check()
    assert (hot.power_limit, cool.power_limit) == (850, 750)
    # The new keys win over the legacy one.
    entry = MockConfigEntry(domain=DOMAIN, data={},
                            options={**legacy, "limit_step": 50, "step_up": 20})
    settings = OvervoltageProtection(hass, entry, []).settings
    assert (settings["step_down"], settings["step_up"]) == (50, 20)


NUMBERS = {
    "number.sossen_direct_protection_high_threshold": "249.0",
    "number.sossen_direct_protection_low_threshold": "245.0",
    "number.sossen_direct_protection_step_down": "70",
    "number.sossen_direct_protection_step_up": "30",
    "number.sossen_direct_protection_lowest_limit": "500",
    "number.sossen_direct_protection_highest_limit": "1000",
    "number.sossen_direct_protection_check_interval": "120",
}


async def test_protection_numbers_update_options_without_reload(hass):
    from homeassistant.helpers import entity_registry as er

    p = _setup_patches()
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        entry = await _loaded_entry(hass, {"protection": True, "limit_step": 50})
        runtime = hass.data[DOMAIN][entry.entry_id]
        ctl = runtime["protection"]
        states = {e: hass.states.get(e) for e in NUMBERS}
        expected = {**NUMBERS, "number.sossen_direct_protection_step_down": "50",
                    "number.sossen_direct_protection_step_up": "50"}
        assert {e: s.state for e, s in states.items() if s} == expected
        registry = er.async_get(hass)
        assert all(registry.async_get(e).entity_category == "config" for e in NUMBERS)

        async def set_value(entity_id, value):
            await hass.services.async_call(
                "number", "set_value", {"entity_id": entity_id, "value": value},
                blocking=True)
            await hass.async_block_till_done()

        await set_value("number.sossen_direct_protection_step_up", 20)
        await set_value("number.sossen_direct_protection_high_threshold", 250.5)
        await set_value("number.sossen_direct_protection_check_interval", 300)
        assert entry.options["step_up"] == 20 and entry.options["high_voltage"] == 250.5
        assert entry.options["interval"] == 300 and entry.options["limit_step"] == 50
        # Applied in place: same runtime, timer restarted at the new interval.
        assert hass.data[DOMAIN][entry.entry_id] is runtime
        assert ctl._interval == 300 and ctl._unsub_timer is not None
        assert (ctl.settings["step_down"], ctl.settings["step_up"]) == (50, 20)
        assert hass.states.get("number.sossen_direct_protection_step_up").state == "20"
        assert hass.states.get("number.sossen_direct_protection_high_threshold").state == "250.5"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_protection_numbers_reject_invalid_combination(hass):
    import pytest
    from homeassistant.exceptions import ServiceValidationError

    p = _setup_patches()
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        entry = await _loaded_entry(hass, dict(PROTECTION))
        runtime = hass.data[DOMAIN][entry.entry_id]
        before = dict(entry.options)
        # low >= high, from either side.
        for entity_id, value in (
            ("number.sossen_direct_protection_low_threshold", 249),
            ("number.sossen_direct_protection_high_threshold", 240),
        ):
            with pytest.raises(ServiceValidationError) as err:
                await hass.services.async_call(
                    "number", "set_value", {"entity_id": entity_id, "value": value},
                    blocking=True)
            assert err.value.translation_key == "low_above_high"
            assert "low threshold" in str(err.value)
        # min > max: lower the highest limit first, then try a higher minimum.
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": "number.sossen_direct_protection_highest_limit", "value": 800},
            blocking=True)
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": "number.sossen_direct_protection_lowest_limit", "value": 900},
                blocking=True)
        assert err.value.translation_key == "min_above_max"
        assert "lowest limit" in str(err.value)
        await hass.async_block_till_done()
        assert entry.options == {**before, "max_limit": 800}
        assert hass.data[DOMAIN][entry.entry_id] is runtime
        assert hass.states.get("number.sossen_direct_protection_lowest_limit").state == "500"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_locate_stops_when_all_found_or_timeout(hass, socket_enabled):
    keys = {d["device_id"]: d["local_key"] for d in INVERTERS}
    disc = SossenDiscovery(hass, keys, None, ["192.168.1.41:6669"])
    with patch.object(disc, "_send_probe", AsyncMock()), \
         patch.object(disc, "async_sweep", AsyncMock()) as sweep, \
         patch("custom_components.sossen_direct.discovery._handshake_ok",
               side_effect=lambda dev, key, addr: dev == ID1), \
         patch("custom_components.sossen_direct.discovery.LOCATE_PROBE_EVERY", 0.01):
        found = await disc.async_locate(0.2)
    assert found == {ID1: "192.168.1.41:6669"}
    assert sweep.called and not disc._transports

    disc = SossenDiscovery(hass, {ID1: "k"}, None, ["192.168.1.41:6669"])
    with patch.object(disc, "_send_probe", AsyncMock()), \
         patch("custom_components.sossen_direct.discovery._handshake_ok", return_value=True), \
         patch.object(disc, "async_sweep", AsyncMock()) as sweep:
        assert await disc.async_locate(5) == {ID1: "192.168.1.41:6669"}
    assert not sweep.called


async def test_diagnostics_redacts_keys(hass):
    from custom_components.sossen_direct.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    p = _setup_patches()
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        entry = await _loaded_entry(hass)
        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
    text = str(diag)
    assert "k1k1k1k1" not in text and diag["data"]["token_info"] == "**REDACTED**"
    assert diag["data"]["devices"][0]["local_key"] == "**REDACTED**"
    assert diag["inverters"][0]["address"] == "192.168.1.50"
    assert diag["inverters"][0]["found_by"] == "lan"
    assert diag["protection"]["enabled"] is False
