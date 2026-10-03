"""Find the inverters' LAN addresses without asking the user.

Two complementary mechanisms, both keyed by Tuya Device ID:

1. Broadcast listening: Tuya devices announce {"gwId", "ip", ...} on UDP
   6666/6667, and v3.5 devices answer a REQ_DEVINFO probe on UDP 7000.
   Another Tuya integration (LocalTuya) may already hold those ports; the
   sockets are opened with SO_REUSEPORT so both can listen, and a port that
   still cannot be bound is simply skipped.
2. Subnet sweep (fallback, only for inverters never heard): connect to TCP 6668 on every host of the local
   /24 networks and identify each inverter by a successful v3.5 session
   handshake with its local key (a wrong key fails the handshake). It is
   kept rare because it briefly opens a session on other Tuya devices too.
   A changed address (DHCP) is picked up from the next broadcast.

The cloud reports only the public WAN address (the same for every device in
the house), so it is never used for this.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import time
from collections.abc import Callable

import tinytuya

from homeassistant.components import network
from homeassistant.core import HomeAssistant, callback

from .const import (
    DISCOVERY_PORTS,
    DISCOVERY_PROBE_EVERY,
    DISCOVERY_PROBE_PORT,
    SWEEP_AFTER,
    SWEEP_EVERY,
    TUYA_PORT,
)

_LOGGER = logging.getLogger(__name__)

SWEEP_CONNECT_TIMEOUT = 0.6
SWEEP_CONCURRENCY = 64


def _probe_payload(own_ip: str) -> bytes:
    """Build the REQ_DEVINFO broadcast that makes v3.5 devices announce."""
    body = json.dumps({"from": "app", "ip": own_ip}).encode()
    msg = tinytuya.TuyaMessage(
        0, tinytuya.REQ_DEVINFO, None, body, 0, True, tinytuya.PREFIX_6699_VALUE, True
    )
    return tinytuya.pack_message(msg, hmac_key=tinytuya.udpkey)


def _handshake_ok(device_id: str, local_key: str, ip: str) -> bool:
    """Return True if ip answers a v3.5 session handshake with this key."""
    dev = tinytuya.Device(device_id, ip, local_key, version=3.5)
    dev.set_socketTimeout(3)
    dev.set_socketRetryLimit(1)
    try:
        return dev._get_socket(True) is True  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet: Callable[[bytes, str], None]) -> None:
        self._on_packet = on_packet

    def datagram_received(self, data: bytes, addr) -> None:
        self._on_packet(data, addr[0])


class SossenDiscovery:
    """Keep a live Device ID -> LAN IP map for the configured inverters."""

    def __init__(
        self,
        hass: HomeAssistant,
        devices: dict[str, str],
        overrides: dict[str, str] | None = None,
    ) -> None:
        """devices maps Device ID -> local key."""
        self.hass = hass
        self._keys = devices
        self._ips: dict[str, str] = {
            dev_id: ip for dev_id, ip in (overrides or {}).items() if ip
        }
        self._fixed = set(self._ips)
        self._transports: list[asyncio.DatagramTransport] = []
        self._task: asyncio.Task | None = None
        self._sweep_lock = asyncio.Lock()
        self._last_sweep = 0.0
        self._started = 0.0

    def get_ip(self, device_id: str) -> str | None:
        """Return the last known LAN IP of an inverter."""
        return self._ips.get(device_id)

    @callback
    def _on_packet(self, data: bytes, sender: str) -> None:
        try:
            info = json.loads(tinytuya.decrypt_udp(data))
        except Exception:  # noqa: BLE001
            return
        dev_id = info.get("gwId") or info.get("devId")
        if dev_id not in self._keys or dev_id in self._fixed:
            return
        ip = info.get("ip") or sender
        if self._ips.get(dev_id) != ip:
            _LOGGER.info("Inverter %s found at %s (broadcast)", dev_id, ip)
            self._ips[dev_id] = ip

    async def async_start(self) -> None:
        """Open the UDP listeners and start the probe/sweep loop."""
        loop = asyncio.get_running_loop()
        for port in DISCOVERY_PORTS:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind(("", port))
                sock.setblocking(False)
            except OSError as err:
                sock.close()
                _LOGGER.debug("UDP %s unavailable for discovery: %s", port, err)
                continue
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self._on_packet), sock=sock
            )
            self._transports.append(transport)
        self._started = time.monotonic()
        self._task = self.hass.async_create_background_task(
            self._loop(), "sossen_direct discovery"
        )

    async def async_stop(self) -> None:
        """Close listeners and stop the loop."""
        if self._task:
            self._task.cancel()
        for transport in self._transports:
            transport.close()
        self._transports.clear()

    def _missing(self) -> list[str]:
        return [dev_id for dev_id in self._keys if dev_id not in self._ips]

    async def _loop(self) -> None:
        while True:
            if self._missing():
                await self._send_probe()
                now = time.monotonic()
                if (
                    now - self._started >= SWEEP_AFTER
                    and now - self._last_sweep >= SWEEP_EVERY
                ):
                    await self.async_sweep()
            await asyncio.sleep(DISCOVERY_PROBE_EVERY)

    async def _ipv4_networks(self) -> list[tuple[str, ipaddress.IPv4Network]]:
        result = []
        for adapter in await network.async_get_adapters(self.hass):
            if not adapter["enabled"]:
                continue
            for ip_info in adapter["ipv4"]:
                own = ip_info["address"]
                prefix = max(int(ip_info["network_prefix"]), 24)
                net = ipaddress.IPv4Network(f"{own}/{prefix}", strict=False)
                if not net.is_loopback:
                    result.append((own, net))
        return result

    async def _send_probe(self) -> None:
        for own, net in await self._ipv4_networks():
            payload = _probe_payload(own)
            for target in (str(net.broadcast_address), "255.255.255.255"):
                try:
                    await self.hass.async_add_executor_job(
                        self._send_udp, own, target, payload
                    )
                except OSError as err:
                    _LOGGER.debug("Probe %s -> %s failed: %s", own, target, err)

    @staticmethod
    def _send_udp(own: str, target: str, payload: bytes) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((own, 0))
            sock.sendto(payload, (target, DISCOVERY_PROBE_PORT))

    async def async_sweep(self) -> None:
        """Scan local /24 networks for TCP 6668 and match inverters by key."""
        async with self._sweep_lock:
            self._last_sweep = time.monotonic()
            missing = self._missing()
            if not missing:
                return
            hosts: list[str] = []
            for own, net in await self._ipv4_networks():
                hosts += [str(h) for h in net.hosts() if str(h) != own]
            sem = asyncio.Semaphore(SWEEP_CONCURRENCY)

            async def is_open(host: str) -> str | None:
                async with sem:
                    try:
                        _, writer = await asyncio.wait_for(
                            asyncio.open_connection(host, TUYA_PORT),
                            SWEEP_CONNECT_TIMEOUT,
                        )
                    except (OSError, asyncio.TimeoutError):
                        return None
                    writer.close()
                    return host

            known = set(self._ips.values())
            candidates = [
                h for h in await asyncio.gather(*(is_open(h) for h in hosts))
                if h and h not in known
            ]
            _LOGGER.debug(
                "Sweep: %d hosts with port %s open, %d inverters to place",
                len(candidates), TUYA_PORT, len(missing),
            )
            for dev_id in missing:
                for host in list(candidates):
                    ok = await self.hass.async_add_executor_job(
                        _handshake_ok, dev_id, self._keys[dev_id], host
                    )
                    if ok:
                        _LOGGER.info("Inverter %s found at %s (sweep)", dev_id, host)
                        self._ips[dev_id] = host
                        candidates.remove(host)
                        break
