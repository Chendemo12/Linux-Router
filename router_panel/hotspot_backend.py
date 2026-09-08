"""Hotspot backend adapter layer.

The WiFi hotspot can be brought up by two very different mechanisms:

* ``nm`` — the default: NetworkManager drives the radio into AP
  mode via ``wpa_supplicant`` and provides DHCP/NAT through ``ipv4.method=shared``.
* ``hostapd`` — hostapd drives the radio directly and the panel manages its own
  LAN side (interface address, standalone dnsmasq, iptables NAT).

This module defines the common :class:`HotspotBackend` interface and the
:func:`get_backend` factory that picks an adapter from ``network.json``'s
``hotspot_backend`` field. The choice is read once and cached, so it is fixed
for the lifetime of the (root agent) process — there is intentionally **no hot
reload**. Swapping backends requires restarting ``router-panel-agent.service``
and re-enabling the hotspot.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from .core import (
    HOTSPOT_BACKEND_HOSTAPD,
    HOTSPOT_BACKEND_NETWORK_MANAGER,
    load_network_config,
)

ProgressCallback = Callable[[str], None]

# Maps backend name -> adapter class. Populated lazily on first use to avoid
# import cycles (adapters import network/network_operations, which route here).
_REGISTRY: dict[str, type["HotspotBackend"]] = {}
_backend: "HotspotBackend | None" = None


class HotspotBackend(ABC):
    """Interface a hotspot mechanism must implement."""

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Backend key, e.g. ``nm`` or ``hostapd``."""

    @abstractmethod
    def start(
        self,
        ifname: str,
        phy_name: str,
        ssid: str,
        password: str,
        band: str,
        channel: str,
        mode: str,
        progress: ProgressCallback | None = None,
    ):
        """Bring the hotspot up on *ifname* (returns a CommandResult-ish)."""

    @abstractmethod
    def stop(self, ifname: str):
        """Tear the hotspot down (returns a CommandResult-ish)."""

    @abstractmethod
    def profile(self) -> dict[str, str]:
        """Read back SSID/password/band/channel/interface of the running config."""

    @abstractmethod
    def active_by_phy(self) -> dict[str, dict[str, Any]]:
        """Map phy -> {device, ...} of hotspot AP interfaces currently up."""

    @abstractmethod
    def dhcp_leases(self, ifname: str) -> dict[str, dict[str, str]]:
        """MAC -> {ip_address, device_name} DHCP leases for *ifname*."""

    @abstractmethod
    def active_binding_conflict(self) -> bool:
        """Whether the card is held by a hotspot mechanism other than ours."""


def _register() -> None:
    if _REGISTRY:
        return
    # Imported lazily: the adapters delegate to network/network_operations,
    # which themselves import this module for routing.
    from .adapters.hostapd_backend import HostapdBackend
    from .adapters.nm_backend import NetworkManagerBackend

    _REGISTRY[HOTSPOT_BACKEND_NETWORK_MANAGER] = NetworkManagerBackend
    _REGISTRY[HOTSPOT_BACKEND_HOSTAPD] = HostapdBackend


def _load_config_backend_name() -> str:
    try:
        return load_network_config().get("hotspot_backend", HOTSPOT_BACKEND_NETWORK_MANAGER)
    except OSError:
        return HOTSPOT_BACKEND_NETWORK_MANAGER


def get_backend() -> HotspotBackend:
    """Return the configured hotspot backend, instantiating and caching it once.

    Because the result is cached in a module global, the backend is fixed once
    the (agent) process first asks for it — matching the "no hot reload" design.
    """
    global _backend
    if _backend is None:
        _register()
        name = _load_config_backend_name()
        backend_class = _REGISTRY.get(name)
        if backend_class is None:
            backend_class = _REGISTRY[HOTSPOT_BACKEND_NETWORK_MANAGER]
        _backend = backend_class()
    return _backend


def reset_backend() -> None:
    """Clear the cached backend (used by tests to pick a fresh config)."""
    global _backend
    _backend = None


def configured_backend_name() -> str:
    """Name of the backend the config selects, without instantiating it."""
    return _load_config_backend_name()
