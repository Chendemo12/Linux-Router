# lnxrouter

基于 [garywill/linux-router](https://github.com/garywill/linux-router) 的脚本，把网卡变成 NATed 子网 / WiFi 热点。

```bash
./lnxrouter.sh --ap wlP2p33s0 SSID -p PASSWORD 
```

## 创建热点并后台运行

```bash
sudo ./lnxrouter.sh --ap <wifi接口> <SSID> -p <密码> --daemon
```

脚本会自动：
1. 校验网卡是否支持 AP 模式；
2. 用 `iw` 创建虚拟接口（形如 `x0wlan0`，可用 `--no-virt` 关闭）；
3. 默认 NAT 上网 + dnsmasq（DHCP/DNS），随机 LAN 地址；
4. 将 PID 与配置记录到 `/dev/shm/lnxrouter_tmp/`（或 `/tmp`）。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--ap <接口> <SSID>` | 创建热点（可带 `-p` 指定密码，8~63 位） |
| `-o <接口>` | 指定上行上网接口（不指定则自动选） |
| `-n` | 不提供上网 |
| `--daemon` | 后台运行（用 `setsid` 分离，退出终端仍运行） |
| `--qr` | 终端显示热点二维码（需 `qrencode`） |
| `--hidden` | 隐藏 SSID |
| `-c <channel>` | 指定信道（默认 2.4G 用 1，5G 用 36） |
| `--freq-band <GHz>` | 频段 `2.4` 或 `5`（默认 `2.4`） |
| `-w <版本>` | WPA 版本：`2` / `3` / `1` / `3+2` ...（默认 `2`） |
| `--no-virt` | 不创建虚拟接口（需网卡支持） |
| `--virt-name <名>` | 指定虚拟接口名 |

### 管理运行中的实例

```bash
# 列出运行实例（PID + 接口）
./lnxrouter.sh --list-running

# 停止实例（用 PID 或子网接口名）
./lnxrouter.sh --stop <pid或接口>

# 查看某实例的客户端
./lnxrouter.sh --list-clients <pid或接口>
```

## 完整示例

```bash
# 创建热点、指定上行网卡、后台运行
sudo ./lnxrouter.sh --ap wlan0 MyHotspot \
  -p "yourpass123" \
  -o eth0 \
  --daemon

# 5GHz 热点
sudo ./lnxrouter.sh --ap wlan0 MyHotspot5G \
  -p "yourpass123" \
  --freq-band 5 \
  -c 149 \
  --daemon
```

运行 `./lnxrouter.sh --help` 查看全部选项。
