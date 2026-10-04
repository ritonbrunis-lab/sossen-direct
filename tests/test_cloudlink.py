"""Cloud fallback: frames and power limit are recognised by content."""

import base64
from unittest.mock import MagicMock

from custom_components.sossen_direct.cloudlink import CloudLink
from custom_components.sossen_direct.protocol import build_set_power_payload


def _frame(ac_power: int, ac_voltage_dv: int) -> str:
    records = [(4096, 1), (4103, ac_voltage_dv), (4126, ac_power), (4098, 12345)]
    data = bytes([0x03, 0x01])
    for dp, val in records:
        data += bytes([0x01, 0x01, dp >> 8, dp & 0xFF, val >> 8, val & 0xFF])
    return base64.b64encode(data).decode()


def _link() -> CloudLink:
    return CloudLink(MagicMock(), MagicMock(), ["dev1"])


def test_frame_from_mqtt_push() -> None:
    link = _link()
    link._on_message(
        {"protocol": 4, "data": {"devId": "dev1", "status": [
            {"dpId": 21, "t": 1, "value": _frame(412, 2398)},
        ]}}
    )
    frame = link.frame("dev1")
    assert frame["ac_power_w"] == 412
    assert frame["ac_voltage_v"] == 239.8
    assert link.online("dev1")


def test_other_devices_ignored() -> None:
    link = _link()
    link._on_message(
        {"protocol": 4, "data": {"devId": "other", "status": [
            {"dpId": 21, "value": _frame(412, 2398)},
        ]}}
    )
    assert link.frame("other") is None


def test_power_limit_and_command_code_learned() -> None:
    link = _link()
    link._absorb("dev1", [{"code": "cmd_raw", "value": build_set_power_payload(800)}])
    assert link.power_limit("dev1") == 800
    assert link._codes["dev1"][24] == "cmd_raw"


def test_set_power_limit_without_code_fails() -> None:
    link = _link()
    link._manager = MagicMock()
    assert link.set_power_limit("dev1", 900) is False
    link._codes["dev1"] = {24: "cmd_raw"}
    link._manager.customer_api.post.return_value = {"success": True}
    assert link.set_power_limit("dev1", 900) is True
    body = link._manager.customer_api.post.call_args[0][2]
    assert body["commands"][0]["code"] == "cmd_raw"
