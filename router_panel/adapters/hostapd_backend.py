"""hostapd hotspot backend.

Drives the radio with ``hostapd`` (bypassing NetworkManager's wpa_supplicant
AP path, which fails on some Realtek adapters such as the RTL8852BE with
"802.1X supplicant took too long to authenticate").

Hardware reality (verified on OrangePi 5 Plus / rtw89_8852be): this driver
reports *no interface combinations* in ``iw phy``, and linux-router auto-forces
its ``--no-virt`` path on such cards. A virtual ``ap-*`` sub-interface cannot be
brought up for AP mode (hostapd fails with "Could not set interface ... UP: Name
not unique on network" / "Device or resource busy"); only the *physical* root
interface switches to AP cleanly. So this backend drives the physical card
exclusively and takes it over from NetworkManager for the duration.

The panel owns the whole LAN side on the (physical) AP interface:

* the gateway address is assigned to it (``ip addr``),
* DHCP + DNS are served by a standalone ``dnsmasq``,
* clients reach the Internet through ``iptables`` NAT + forwarding.

Because concurrent AP+STA is not supported by this driver, ``mode`` is accepted
but treated as exclusive: the physical card is given to the AP regardless. Any
live STA link on the card is suspended while the hotspot is up and restored on
stop.

Runtime state (configs, pids, leases) lives under ``/run/linux-router/hostapd``
(tmpfs), keyed by the physical interface name, never in the persistent data
dir, because the hostapd config embeds the passphrase.
"""

from __future__ import annotations

import time
from ipaddress import ip_network
from pathlib import Path
from typing import Any, Callable

ProgressCallback = Callable[[str], None]

from ..core import (
    HOTSPOT_BACKEND_HOSTAPD,
    CommandResult,
    NETWORKMANAGER_CONF_DIR,
    atomic_write_text,
    command_exists,
    load_network_config,
    run_command,
)

RUN_ROOT = Path("/run/linux-router/hostapd")

# Best-effort iptables rules that give the LAN outbound NAT. Kept as (table,
# chain, args...) so teardown can delete them symmetrically.
_NAT_RULES = (
    ("nat", "POSTROUTING", ["-s", "{net}", "!", "-d", "{net}", "-j", "MASQUERADE"]),
    ("filter", "FORWARD", ["-i", "{iface}", "-s", "{net}", "-j", "ACCEPT"]),
    ("filter", "FORWARD", ["-o", "{iface}", "-d", "{net}", "-j", "ACCEPT"]),
)

_COMMENT_TAG = "router-panel-hostapd"


def _comment(tag: str) -> list[str]:
    return ["-m", "comment", "--comment", tag]


def _hw_mode_from_band(band: str) -> str:
    if band in {"a", "5", "5GHz", "5G"}:
        return "a"
    return "g"


def _dhcp_range(network: str, gateway: str) -> str:
    try:
        net = ip_network(network, strict=False)
    except ValueError:
        return f"{gateway},254h"
    host = net.network_address
    if net.num_addresses < 20:
        return f"{host + 1},{host + 10},24h"
    return f"{host + 10},{host + 250},24h"


class HostapdBackend:
    """Hotspot backend that brings the AP up on the physical interface with hostapd.

    All entry points take the physical interface name; the card is temporarily
    taken over from NetworkManager (exclusive AP) and given back on stop.
    """

    @classmethod
    def name(cls) -> str:
        return HOTSPOT_BACKEND_HOSTAPD

    # -- paths -------------------------------------------------------------
    def _run_dir(self, ifname: str) -> Path:
        return RUN_ROOT / ifname

    def _iface_exists(self, ifname: str) -> bool:
        return Path(f"/sys/class/net/{ifname}").exists()

    def _resolve_phy(self, ifname: str) -> str:
        from ..network import get_wireless_interface_phy_map

        return get_wireless_interface_phy_map().get(ifname, "")

    # -- lifecycle ---------------------------------------------------------
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
        if not 8 <= len(password) <= 63:
            return CommandResult(False, "热点密码长度必须在 8 到 63 个字符之间")
        if not command_exists("hostapd"):
            return CommandResult(False, "系统缺少 hostapd，请先安装 hostapd")
        if not command_exists("dnsmasq"):
            return CommandResult(False, "系统缺少 dnsmasq，请先安装 dnsmasq-base")

        if progress:
            progress("正在准备 hostapd 运行时目录")
        try:
            self._run_dir(ifname).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return CommandResult(False, f"创建 hostapd 运行时目录失败：{exc}")

        lan = load_network_config()
        network = lan["lan_network"]
        gateway = lan["lan_gateway"]
        lan_address = lan["lan_address"]
        hw_mode = _hw_mode_from_band(band)
        if channel and channel.isdigit():
            channel_num = channel
        else:
            # The panel may submit an empty channel (auto) for the hostapd path,
            # which bypasses the NM auto-channel selector. This Realtek driver
            # rejects channel=0, so default to a concrete, non-DFS channel per
            # band — matching linux-router (2.4G -> 1, 5G -> 36).
            channel_num = "36" if hw_mode == "a" else "1"

        error = self._bring_up(
            ifname,
            ssid,
            password,
            hw_mode,
            channel_num,
            network,
            gateway,
            lan_address,
            progress,
        )
        if error:
            # Roll back anything partially started so we never leave a half-AP.
            self.stop(ifname)
            return CommandResult(False, error)
        return CommandResult(True, f"已开启热点：{ssid}（hostapd）")

    def _bring_up(
        self,
        ifname: str,
        ssid: str,
        password: str,
        hw_mode: str,
        channel_num: str,
        network: str,
        gateway: str,
        lan_address: str,
        progress: ProgressCallback | None,
    ) -> str | None:
        """Bring the AP fully up; return an error string or None on success."""
        if progress:
            progress("正在将无线网卡从 NetworkManager 接管")
        if not self._take_interface_unmanaged(ifname):
            return f"无法将 {ifname} 从 NetworkManager 接管"

        # Disable power save (hostapd needs a stable radio) and leave the phy
        # free to transmit.
        run_command(["iw", "dev", ifname, "set", "power_save", "off"], timeout=10)
        self._unblock_rfkill()

        # linux-router's known-good bring-up: down -> flush -> up the interface
        # so it is clean before hostapd opens it. A card NM still holds (scanning
        # / mid-state) makes hostapd fail with "Could not configure driver mode".
        run_command(["ip", "link", "set", "dev", ifname, "down"], timeout=10)
        self._flush_interface_address(ifname)
        run_command(["ip", "link", "set", "dev", ifname, "up"], timeout=10)

        if progress:
            progress("正在启动 hostapd")
        hostapd_error = self._start_hostapd(ifname, ssid, password, hw_mode, channel_num)
        if hostapd_error:
            return hostapd_error

        if progress:
            progress("正在为 AP 接口配置地址")
        assign_error = self._assign_interface_address(ifname, lan_address)
        if assign_error:
            return assign_error

        if progress:
            progress("正在启用 IP 转发")
        self._enable_ip_forward()

        if progress:
            progress("正在启动 DHCP/DNS")
        dnsmasq_error = self._start_dnsmasq(ifname, network, gateway)
        if dnsmasq_error:
            return dnsmasq_error

        if progress:
            progress("正在配置 NAT 转发")
        self._apply_nat(ifname, network)
        return None

    def stop(self, ifname: str):
        run_dir = self._run_dir(ifname)
        lan = load_network_config()
        self._remove_nat(ifname, lan["lan_network"])
        self._kill_from_pidfile(run_dir / "dnsmasq.pid")
        self._kill_from_pidfile(run_dir / "hostapd.pid")
        self._flush_interface_address(ifname)
        self._restore_interface_managed(ifname)
        # The card is back under NetworkManager control. Drop any leftover
        # inactive project hotspot connection so NM does not auto-resurrect an
        # AP on the card now that it is managed again.
        try:
            from ..network_operations import delete_inactive_hotspot_profiles

            delete_inactive_hotspot_profiles()
        except Exception:
            pass
        try:
            if run_dir.exists():
                for child in run_dir.iterdir():
                    child.unlink()
                run_dir.rmdir()
        except OSError:
            pass
        return CommandResult(True, "已关闭热点（hostapd）")

    # -- status ------------------------------------------------------------
    def profile(self) -> dict[str, str]:
        default = {
            "ssid": "",
            "password": "",
            "band": "",
            "channel": "",
            "interface_name": "",
            "mode": "exclusive",
        }
        # A live hostapd instance is the source of truth for the running config.
        for run_dir in RUN_ROOT.glob("*") if RUN_ROOT.exists() else []:
            conf = run_dir / "hostapd.conf"
            if not conf.exists():
                continue
            try:
                lines = conf.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            values: dict[str, str] = dict(default)
            for line in lines:
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if key == "ssid":
                    values["ssid"] = value
                elif key == "wpa_passphrase":
                    values["password"] = value
                elif key == "hw_mode":
                    values["band"] = "a" if value == "a" else "bg"
                elif key == "channel":
                    values["channel"] = "" if value == "0" else value
            # The run dir is keyed by the physical interface the AP runs on.
            values["interface_name"] = run_dir.name
            return values
        return default

    def active_by_phy(self) -> dict[str, dict[str, Any]]:
        """Map phy -> {device} for AP interfaces hostapd currently has up."""
        if not RUN_ROOT.exists():
            return {}
        found: dict[str, dict[str, Any]] = {}
        for run_dir in RUN_ROOT.iterdir():
            pid_file = run_dir / "hostapd.pid"
            if not pid_file.exists():
                continue
            if not self._pid_alive(pid_file):
                continue
            conf = run_dir / "hostapd.conf"
            if not conf.exists():
                continue
            try:
                lines = conf.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            interface = ""
            for line in lines:
                if line.startswith("interface="):
                    interface = line.partition("=")[2].strip()
                    break
            if not interface:
                continue
            iface_dev = self._iface_dev(interface)
            phy_name = iface_dev.get("phy", "")
            if not phy_name:
                continue
            found[phy_name] = {"device": interface, "mode": "exclusive"}
        return found

    def dhcp_leases(self, ifname: str) -> dict[str, dict[str, str]]:
        run_dir = self._run_dir(ifname)
        lease_path = run_dir / "dnsmasq.leases"
        leases: dict[str, dict[str, str]] = {}
        try:
            lines = lease_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return leases

        from ..core import normalize_mac_address

        for line in lines:
            fields = line.split(maxsplit=4)
            if len(fields) < 4:
                continue
            _, mac_address, ip_address, hostname = fields[:4]
            normalized_mac = normalize_mac_address(mac_address)
            if not normalized_mac:
                continue
            leases[normalized_mac] = {
                "ip_address": ip_address.strip() or "未知",
                "device_name": hostname.strip() if hostname.strip() not in {"", "*"} else "未知设备",
            }
        return leases

    def active_binding_conflict(self) -> bool:
        # Under this backend hostapd is OURS; an active hostapd service is not a
        # conflict (it is the mechanism we drive, or nothing else should run).
        return False

    # -- helpers -----------------------------------------------------------
    def _pid_alive(self, pid_file: Path) -> bool:
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        return Path(f"/proc/{pid}").exists()

    def _kill_from_pidfile(self, pid_file: Path) -> None:
        if not pid_file.exists():
            return
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        run_command(["kill", str(pid)], timeout=5)
        # Give it a moment then SIGKILL if it lingers.
        for _ in range(10):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.2)
        if Path(f"/proc/{pid}").exists():
            run_command(["kill", "-9", str(pid)], timeout=5)

    def _iface_dev(self, ifname: str) -> dict[str, str]:
        from ..network import get_wireless_interface_phy_map

        phy_map = get_wireless_interface_phy_map()
        return {"phy": phy_map.get(ifname, "")}

    def _wait_interface(self, ifname: str, timeout: float = 3.0) -> bool:
        for _ in range(int(timeout / 0.1)):
            if self._iface_exists(ifname):
                return True
            time.sleep(0.1)
        return self._iface_exists(ifname)

    def _take_interface_unmanaged(self, ifname: str) -> bool:
        # Write a conf.d entry so NM leaves the interface alone, then set it
        # unmanaged at runtime too. Unmanaging first is what lets hostapd switch
        # the physical card into AP mode (see module docstring).
        conf_written = False
        if NETWORKMANAGER_CONF_DIR.exists():
            conf_file = NETWORKMANAGER_CONF_DIR / f"90-router-panel-hostapd-{ifname}.conf"
            try:
                atomic_write_text(
                    conf_file,
                    "[device]\n"
                    f"match-device=interface-name:{ifname}\n"
                    "managed=false\n",
                    mode=0o600,
                )
                conf_written = True
            except OSError:
                pass
        run_command(["nmcli", "general", "reload"], timeout=15)
        set_result = run_command(
            ["nmcli", "device", "set", ifname, "managed", "no"], timeout=15
        )
        if set_result.ok or conf_written or self._iface_is_unmanaged(ifname):
            return True
        return False

    def _iface_is_unmanaged(self, ifname: str) -> bool:
        result = run_command(
            ["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"], timeout=8
        )
        for line in result.output.splitlines():
            fields = line.split(":")
            if len(fields) >= 2 and fields[0] == ifname and "unmanaged" in fields[1]:
                return True
        return False

    def _restore_interface_managed(self, ifname: str) -> None:
        if NETWORKMANAGER_CONF_DIR.exists():
            conf_file = NETWORKMANAGER_CONF_DIR / f"90-router-panel-hostapd-{ifname}.conf"
            try:
                conf_file.unlink()
            except OSError:
                pass
        run_command(["nmcli", "general", "reload"], timeout=15)
        run_command(["nmcli", "device", "set", ifname, "managed", "yes"], timeout=15)

    def _unblock_rfkill(self) -> None:
        # Realtek radios are frequently soft-blocked; hostapd then fails to open
        # the interface. linux-router unblocks wifi rfkill right before bring-up.
        if command_exists("rfkill"):
            run_command(["rfkill", "unblock", "wifi"], timeout=10)

    def _assign_interface_address(self, ifname: str, lan_address: str) -> str | None:
        # The link is already up (see _bring_up); just add the gateway address.
        added = run_command(["ip", "addr", "add", lan_address, "dev", ifname], timeout=10)
        if not added.ok:
            return added.output or f"为 {ifname} 配置地址失败"
        return None

    def _flush_interface_address(self, ifname: str) -> None:
        run_command(["ip", "addr", "flush", "dev", ifname], timeout=10)

    def _write_hostapd_conf(
        self, ifname: str, ssid: str, password: str, hw_mode: str, channel: str
    ) -> Path:
        run_dir = self._run_dir(ifname)
        ctrl_dir = run_dir / "ctrl"
        ctrl_dir.mkdir(parents=True, exist_ok=True)
        conf = run_dir / "hostapd.conf"
        # Mirror linux-router's known-good, minimal conf. Notably we do NOT force
        # 802.11n/HT or wmm here: some single-radio Realtek cards then fail to
        # switch into AP mode. Plain 802.11b/g/a keeps the widest compatibility.
        content = (
            f"interface={ifname}\n"
            "driver=nl80211\n"
            f"ssid={ssid}\n"
            f"hw_mode={hw_mode}\n"
            f"channel={channel}\n"
            f"ctrl_interface={ctrl_dir}\n"
            "ctrl_interface_group=0\n"
            "beacon_int=100\n"
            "wpa=2\n"
            f"wpa_passphrase={password}\n"
            "wpa_key_mgmt=WPA-PSK\n"
            "rsn_pairwise=CCMP\n"
            "wpa_pairwise=CCMP\n"
        )
        atomic_write_text(conf, content, mode=0o600)
        return conf

    def _start_hostapd(
        self, ifname: str, ssid: str, password: str, hw_mode: str, channel: str
    ) -> str | None:
        conf = self._write_hostapd_conf(ifname, ssid, password, hw_mode, channel)
        run_dir = self._run_dir(ifname)
        log = run_dir / "hostapd.log"
        pid = run_dir / "hostapd.pid"
        started = run_command(
            [
                "hostapd",
                "-B",
                "-P",
                str(pid),
                "-f",
                str(log),
                str(conf),
            ],
            timeout=20,
        )
        if not started.ok:
            return self._hostapd_log_tail(ifname, started.output or "启动 hostapd 失败")
        for _ in range(20):
            if self._pid_alive(pid) and self._iface_has_ap(ifname):
                return None
            time.sleep(0.3)
        return self._hostapd_log_tail(ifname, "hostapd 已启动但 AP 未就绪")

    def _iface_has_ap(self, ifname: str) -> bool:
        result = run_command(["iw", "dev", ifname, "info"], timeout=8)
        return result.ok and ("type AP" in result.output or "type __AP" in result.output)

    def _hostapd_log_tail(self, ifname: str, prefix: str) -> str:
        log = self._run_dir(ifname) / "hostapd.log"
        tail = ""
        try:
            tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-6:])
        except OSError:
            tail = ""
        if tail:
            return f"{prefix}。hostapd 日志：\n{tail}"
        return prefix

    def _enable_ip_forward(self) -> None:
        run_command(["sysctl", "-w", "net.ipv4.ip_forward=1"], timeout=10)

    def _write_dnsmasq_conf(self, ifname: str, network: str, gateway: str) -> Path:
        run_dir = self._run_dir(ifname)
        conf = run_dir / "dnsmasq.conf"
        range_value = _dhcp_range(network, gateway)
        content = (
            f"interface={ifname}\n"
            "bind-interfaces\n"
            f"listen-address={gateway}\n"
            f"dhcp-range={range_value}\n"
            f"dhcp-option=option:router,{gateway}\n"
            f"dhcp-option=option:dns-server,{gateway}\n"
            "no-resolv\n"
            "no-poll\n"
            f"pid-file={run_dir}/dnsmasq.pid\n"
            f"dhcp-leasefile={run_dir}/dnsmasq.leases\n"
        )
        atomic_write_text(conf, content, mode=0o600)
        return conf

    def _start_dnsmasq(self, ifname: str, network: str, gateway: str) -> str | None:
        conf = self._write_dnsmasq_conf(ifname, network, gateway)
        pid = self._run_dir(ifname) / "dnsmasq.pid"
        # Use the --opt=value form: some dnsmasq builds reject space-separated
        # long options ("junk found in command line").
        started = run_command(
            ["dnsmasq", f"--conf-file={conf}", f"--pid-file={pid}"],
            timeout=20,
        )
        if not started.ok:
            return started.output or "启动 DHCP/DNS 服务失败"
        for _ in range(20):
            if self._pid_alive(pid):
                return None
            time.sleep(0.3)
        return "dnsmasq 已启动但未就绪"

    def _apply_nat(self, ifname: str, network: str) -> None:
        for table, chain, args in _NAT_RULES:
            rule = [arg.replace("{net}", network).replace("{iface}", ifname) for arg in args]
            command = (
                ["iptables", "-t", table, "-C", chain, *rule, *_comment(_COMMENT_TAG)]
            )
            check = run_command(command, timeout=8)
            if check.ok:
                continue
            run_command(
                ["iptables", "-t", table, "-I", chain, *rule, *_comment(_COMMENT_TAG)],
                timeout=8,
            )

    def _remove_nat(self, ifname: str, network: str) -> None:
        for table, chain, args in _NAT_RULES:
            rule = [arg.replace("{net}", network).replace("{iface}", ifname) for arg in args]
            run_command(
                ["iptables", "-t", table, "-D", chain, *rule, *_comment(_COMMENT_TAG)],
                timeout=8,
            )
