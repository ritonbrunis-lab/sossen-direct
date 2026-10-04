"""Tests for the SOSSEN payload decoder."""

import base64

from custom_components.sossen_direct.const import (
    DP_AC_VOLTAGE,
    DP_ENERGY_AC_UNITS,
    DP_ENERGY_DC_UNITS,
)
from custom_components.sossen_direct.protocol import decode_payload


def _payload(records: dict[int, int]) -> str:
    data = bytes([0x03, 0x01])
    for dp, value in records.items():
        data += bytes([0x01, 0x01, dp >> 8, dp & 0xFF, value >> 8, value & 0xFF])
    return base64.b64encode(data).decode()


def test_efficiency_reported_when_plausible():
    frame = decode_payload(
        _payload({DP_AC_VOLTAGE: 2400, DP_ENERGY_AC_UNITS: 4750, DP_ENERGY_DC_UNITS: 5000})
    )
    assert frame["conversion_efficiency_lifetime"] == 95.0


def test_efficiency_hidden_above_100_percent():
    frame = decode_payload(
        _payload({DP_AC_VOLTAGE: 2400, DP_ENERGY_AC_UNITS: 5300, DP_ENERGY_DC_UNITS: 5000})
    )
    assert frame["conversion_efficiency_lifetime"] is None
    assert frame["energy_dc_total_kwh"] is None
