"""NetworkManager hotspot backend.

This is the original hotspot mechanism. NetworkManager drives the radio into AP
mode (via wpa_supplicant) and provides DHCP/NAT through ``ipv4.method=shared``.

Rather than duplicate the well-tested logic, this adapter delegates to the
existing module-level functions in :mod:`router_panel.network_operations` and
:mod:`router_panel.network`, which remain the canonical NM implementation and
stay reachable by the code paths (and unit tests) that call them directly.
"""

from __future__ import annotations

from typing import Any

from ..core import (
    HOTSPOT_BACKEND_NETWORK_MANAGER,
    is_service_active,
)


class NetworkManagerBackend:
    """Hotspot backend that creates the AP through NetworkManager."""

    @classmethod
    def name(cls) -> str:
        return HOTSPOT_BACKEND_NETWORK_MANAGER

    def start(
        self,
        ifname: str,
        phy_name: str,
        ssid: str,
        password: str,
        band: str,
        channel: str,
        mode: str,
        progress=None,
    ):
        from .. import network_operations

        return network_operations.start_hotspot_profile(
            ifname,
            phy_name,
            ssid,
            password,
            band,
            channel,
            mode,
            progress=progress,
        )

    def stop(self, ifname: str):
        from .. import network_operations

        return network_operations.stop_hotspot_profile(ifname)

    def profile(self) -> dict[str, str]:
        from .. import network

        return network.get_hotspot_profile()

    def active_by_phy(self) -> dict[str, dict[str, Any]]:
        from .. import network

        wireless_phy_map = network.get_wireless_interface_phy_map()
        active_items = network.get_active_connections()
        return network.get_hotspot_active_connections_by_phy(active_items, wireless_phy_map)

    def dhcp_leases(self, ifname: str) -> dict[str, dict[str, str]]:
        from .. import network

        return network.get_hotspot_dhcp_leases(ifname)

    def active_binding_conflict(self) -> bool:
        # Under the NetworkManager backend a running external hostapd is a
        # conflict (the card is held by another AP daemon we don't own).
        return is_service_active("hostapd")
