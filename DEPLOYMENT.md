# Linux Router 部署与故障排查

本文说明 Linux Router 的手动部署、网络配置、服务管理和常见故障排查。全新设备优先使用 [README.zh-CN.md](README.zh-CN.md) 中的安装器；手动部署适合开发、定制目录或需要逐步检查系统配置的场景。

下文使用以下示例路径：

```bash
INSTALL_DIR=/home/router-panel
DATA_DIR=/home/router-panel/data
```

如使用其他路径，必须同步修改环境变量、systemd unit 和后续命令。

## 1. 系统要求

目标系统为 Debian 13 或基于 Debian 的 Armbian，并使用 systemd、apt 和 NetworkManager。

安装依赖：

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-flask \
  gunicorn \
  network-manager \
  dnsmasq-base \
  iptables \
  iw \
  iproute2 \
  udev \
  wpasupplicant \
  curl
```

组件用途：

- `python3-flask`、`gunicorn`：运行 Web 服务；
- `network-manager`：管理有线、Wi-Fi 和热点连接；
- `dnsmasq-base`：提供 NetworkManager shared 热点所需的 DHCP/DNS 能力；
- `iptables`：NetworkManager shared 模式使用的系统转发后端；
- `iw`：读取无线接口、PHY 和 AP+STA 能力；
- `iproute2`、`udev`：系统网络和硬件信息查询；
- `wpasupplicant`：为 NetworkManager 提供 Wi-Fi 扫描、认证和连接后端。

项目不要求目标设备安装 Git。安装器和手动部署都可以使用源码目录或 GitHub 源码压缩包。

## 2. 部署程序文件

将项目文件部署到目标目录，并确保包含以下内容：

```text
$INSTALL_DIR/app.py
$INSTALL_DIR/agent.py
$INSTALL_DIR/router_panel/
$INSTALL_DIR/templates/
$INSTALL_DIR/static/
$INSTALL_DIR/router-panel.service
$INSTALL_DIR/router-panel-agent.service
```

例如从当前源码目录复制：

```bash
sudo install -d -m 0755 "$INSTALL_DIR"
sudo cp -a ./. "$INSTALL_DIR/"
sudo chown -R root:root "$INSTALL_DIR"
```

不要把运行时账号、密钥或网络配置放入源码目录。运行数据应保存在 `$DATA_DIR`。

## 3. 创建运行账号和数据目录

Web 服务以普通用户运行，Agent 以 root 运行并使用 `router-panel` 组限制 Unix Socket 访问：

```bash
sudo groupadd --system router-panel
sudo useradd --system \
  --gid router-panel \
  --home-dir "$DATA_DIR" \
  --no-create-home \
  --shell /usr/sbin/nologin \
  router-panel

sudo install -d -o router-panel -g router-panel -m 0700 "$DATA_DIR"
```

如果用户或组已经存在，不要重复创建；确认数据目录最终归属为 `router-panel:router-panel`。

初始化应用数据并生成开发环境默认账号：

```bash
sudo env \
  LINUX_ROUTER_DATA_DIR="$DATA_DIR" \
  LINUX_ROUTER_INITIAL_PASSWORD='CHANGE_THIS_PASSWORD' \
  python3 -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import app"

# 将 CHANGE_THIS_PASSWORD 替换为实际初始密码
sudo chown -R router-panel:router-panel "$DATA_DIR"
sudo chmod 0700 "$DATA_DIR"
sudo find "$DATA_DIR" -type f -exec chmod 0600 {} +
```

如果未设置 `LINUX_ROUTER_INITIAL_PASSWORD`，开发环境默认密码为 `password`。生产部署应显式设置初始密码并在首次登录后立即修改。

## 4. 配置 NetworkManager 和 netplan

启用 NetworkManager：

```bash
sudo systemctl enable --now NetworkManager.service
```

确认传统接口由 NetworkManager 管理。`/etc/NetworkManager/NetworkManager.conf` 至少应包含：

```ini
[ifupdown]
managed=true
```

如果系统使用 netplan，应将 renderer 设置为 `NetworkManager`。建议先备份现有配置：

```bash
sudo cp -a /etc/netplan "/etc/netplan.backup.$(date +%Y%m%d%H%M%S)"
```

项目生成的最小配置示例：

```yaml
network:
  version: 2
  renderer: NetworkManager
```

保存后，在本地控制台或维护窗口执行：

```bash
sudo netplan generate
sudo netplan apply
sudo systemctl restart NetworkManager.service
```

检查 renderer 和网卡状态：

```bash
grep -R "^[[:space:]]*renderer:" /etc/netplan 2>/dev/null
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
```

如果系统使用 `dhcpcd` 管理同一批接口，应在维护窗口确认后停止并禁用：

```bash
sudo systemctl disable --now dhcpcd.service
```

不要让 `dhcpcd`、`systemd-networkd` 和 NetworkManager 同时管理同一接口。

## 5. 配置 IPv4 转发

热点共享需要 IPv4 转发：

```bash
printf 'net.ipv4.ip_forward=1\n' | sudo tee /etc/sysctl.d/90-router-panel.conf
sudo sysctl --system
sudo sysctl -n net.ipv4.ip_forward
```

期望输出：

```text
1
```

## 6. 安装并启动 systemd 服务

服务模板包含 `@INSTALL_DIR@` 和 `@DATA_DIR@` 占位符。生成实际 unit：

```bash
sudo sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@DATA_DIR@|$DATA_DIR|g" \
  "$INSTALL_DIR/router-panel-agent.service" \
  | sudo tee /etc/systemd/system/router-panel-agent.service >/dev/null

sudo sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@DATA_DIR@|$DATA_DIR|g" \
  "$INSTALL_DIR/router-panel.service" \
  | sudo tee /etc/systemd/system/router-panel.service >/dev/null

sudo chmod 0644 \
  /etc/systemd/system/router-panel-agent.service \
  /etc/systemd/system/router-panel.service

sudo systemd-analyze verify \
  /etc/systemd/system/router-panel-agent.service \
  /etc/systemd/system/router-panel.service

sudo systemctl daemon-reload
sudo systemctl enable --now router-panel-agent.service router-panel.service
```

检查服务：

```bash
sudo systemctl status router-panel-agent.service --no-pager
sudo systemctl status router-panel.service --no-pager
curl -sS http://127.0.0.1/healthz
```

健康检查应返回：

```json
{"status":"ok"}
```

Web 服务默认监听 TCP `80` 端口。服务需要绑定低端口能力，unit 中已配置 `CAP_NET_BIND_SERVICE`。

## 7. 首次登录和运行数据

安装或初始化完成后：

- 用户名：`admin`；
- 初始密码：由 `LINUX_ROUTER_INITIAL_PASSWORD` 指定，或使用开发环境默认值；
- 密码提示文件：`$DATA_DIR/initial_password.txt`。

以下文件属于运行数据，不应提交到 Git：

```text
$DATA_DIR/auth.json
$DATA_DIR/secret_key
$DATA_DIR/network.json
$DATA_DIR/hotspot_keepalive.json
```

## 8. 热点和共享网络检查

热点使用 NetworkManager 的 `ipv4.method shared`，项目不直接维护一套独立的 NAT 规则。启动热点前确认：

热点首先使用 NetworkManager 默认安全参数激活；如果配置已写入但激活失败，会自动改用 WPA2-RSN、CCMP 并关闭 PMF 重试一次。第二次失败时会同时保留两次错误信息。

### 热点后端选择：NetworkManager 或 hostapd

热点可由两种后端驱动，通过 `network.json` 里的 `hotspot_backend` 字段（`nm` 或 `hostapd`，默认 `nm`）选择：

```bash
# 以 hostapd 后端为例
sudo sed -i 's/"hotspot_backend": "nm"/"hotspot_backend": "hostapd"/' "$DATA_DIR/network.json"
```

- **`nm`（默认）**：NetworkManager 驱动射频进 AP 模式，DHCP/NAT 用 `ipv4.method=shared`。绝大多数网卡首选。
- **`hostapd`**：由 `hostapd` 直接接管**物理无线口**起 AP，面板自管网关地址、独立 `dnsmasq` 与 `iptables` NAT。适用于 NetworkManager 的 `shared` AP 路径起不来的驱动（如部分 Realtek RTL8852BE）。

要点：

- 后端在 `router-panel-agent.service` 进程内**读取一次并缓存**，改字段后**必须重启 agent**（`sudo systemctl restart router-panel-agent.service`），并先停掉正在跑的热点再切。
- hostapd 后端对网卡做独占接管（不开并发 AP+STA），`mode` 一律按独占处理。
- 需要安装 `hostapd`（`install.sh` 已含）。
- AP 自动保活对 hostapd 后端同样适用：探活走 hostapd/dnsmasq 运行时状态，掉线时自动重启后端。

```bash
systemctl is-active NetworkManager.service
command -v nmcli
command -v iw
command -v iptables
dpkg-query -W dnsmasq-base
sysctl -n net.ipv4.ip_forward
```

查看活动连接和热点配置：

```bash
nmcli -f NAME,TYPE,DEVICE connection show --active
nmcli connection show DebianRouterHotspot
nmcli -g ipv4.method connection show id DebianRouterHotspot
```

热点共享正常时，最后一条命令应返回：

```text
shared
```

查看无线接口和驱动能力：

```bash
iw dev
iw phy
```

并发 AP+STA 是否可用取决于无线驱动。许多设备只支持 AP 与 STA 使用同一频段或同一信道；连接不同信道的上游 Wi-Fi 时，热点可能无法启动或连接可能失败。

## 9. 常见故障排查

### Web 页面无法打开

```bash
sudo systemctl status router-panel.service --no-pager
sudo journalctl -u router-panel.service -n 100 --no-pager
sudo ss -ltnp | grep ':80 '
curl -v http://127.0.0.1/healthz
```

### Agent 不可用

```bash
sudo systemctl status router-panel-agent.service --no-pager
sudo journalctl -u router-panel-agent.service -n 100 --no-pager
sudo ls -l /run/linux-router/agent.sock
```

确认 Web 服务和 Agent 使用相同的 `LINUX_ROUTER_DATA_DIR`、Socket 路径以及 `router-panel` 组。

### 网卡显示为 unmanaged

```bash
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
grep -R "^[[:space:]]*renderer:" /etc/netplan 2>/dev/null
sudo udevadm info -q property -p /sys/class/net/<接口名> | grep NM_UNMANAGED
```

确认 NetworkManager 使用 `managed=true`，netplan renderer 为 `NetworkManager`，并且没有其他服务接管同一接口。修改后在维护窗口执行 `netplan generate`、`netplan apply` 和 NetworkManager 重启。

### 无线网卡显示为 unavailable

```bash
dpkg-query -W -f='${db:Status-Abbrev} ${Version}\n' wpasupplicant
nmcli radio wifi
rfkill list
ip link show <接口名>
sudo dmesg | grep -iE 'firmware|wifi|wlan|iwl'
```

确认 `wpasupplicant` 已完整安装、Wi-Fi 射频已开启、rfkill 未屏蔽设备，并检查内核日志中是否存在驱动或固件加载失败。Intel 无线网卡缺少固件时可安装 `firmware-iwlwifi`；其他芯片应安装与硬件对应的固件包。

### 热点能连接但无法上网

依次确认：

1. 上游 Wi-Fi 或有线接口已连接并拥有默认路由；
2. `DebianRouterHotspot` 的 IPv4 方法为 `shared`；
3. `dnsmasq-base`、`iptables` 已安装；
4. `net.ipv4.ip_forward` 为 `1`。

```bash
ip route
nmcli -g ipv4.method connection show id DebianRouterHotspot
systemctl status NetworkManager.service --no-pager
```

### 修改配置后未生效

```bash
sudo systemctl restart router-panel.service
sudo systemctl restart router-panel-agent.service
```

修改 `static/style.css` 后，还需递增 `templates/base.html` 中 CSS URL 的 `v` 参数。

## 10. 卸载和恢复

如果程序由安装器部署，推荐使用安装器卸载：

```bash
sudo bash install.sh uninstall \
  --install-dir /home/router-panel \
  --data-dir /home/router-panel/data
```

默认卸载保留账号、密钥和 LAN 配置，并恢复安装器记录的 NetworkManager、netplan、sysctl、IPv4 转发和 `dhcpcd` 状态。使用 `--purge-data` 会删除全部运行数据，且不可恢复。

通过 SSH 卸载时，网络运行状态可能被延迟恢复。确认维护窗口后，使用：

```bash
sudo bash install.sh uninstall \
  --install-dir /home/router-panel \
  --data-dir /home/router-panel/data \
  --apply-network-now
```

如果安装或升级在健康检查前失败，安装器会尝试恢复应用目录、数据目录、项目网络配置和服务状态。恢复后仍应检查 `systemctl`、`nmcli` 和默认路由。

## 11. 开发验证

开发环境可分别启动 Agent 和 Web：

```bash
cd "$INSTALL_DIR"
sudo env \
  LINUX_ROUTER_DATA_DIR="$DATA_DIR" \
  LINUX_ROUTER_AGENT_SOCKET=/run/linux-router/agent.sock \
  python3 agent.py

# 另一个终端
python3 app.py
```

运行测试：

```bash
python3 -m unittest tests.test_application
```

修改 Web 代码或模板后重启 `router-panel.service`；修改 Agent、系统查询或网络操作后重启 `router-panel-agent.service`。生产环境应使用 systemd 管理的 Gunicorn 服务。

## 12. 更新设备到 GitHub 上的最新代码

如果设备最初由安装器（`install.sh`）部署，之后希望更新到仓库里最新的代码，推荐在**设备本身**上使用安装器的 `upgrade` 命令。安装器会在运行时从 GitHub 拉取源码压缩包并解压部署，因此目标设备无需安装 Git。

`upgrade` 只替换程序文件和 systemd 服务定义，**保留**账号、密钥、LAN 配置等运行数据，且不重配网络；结束后会执行健康检查，失败时自动回滚到上一版本。纯代码改动（如本仓库的 `router_panel/`、`templates/`、`static/`）走 `upgrade` 即可，无需 `--apply-network-now`。

> **关键提示**：安装器默认从 `https://github.com/Jaksay/Linux-Router` 的 `main` 分支拉取（`install.sh` 顶部默认值）。如果你的改动位于**自维护仓库的其它分支**（例如 fork 的 `dev`），必须显式指定 `--repo` 与 `--branch`；否则 `upgrade` 会拉取上游 `main`，不仅装不上你的改动，还可能覆盖设备上现有的程序文件。

以自维护仓库 `Chendemo12/Linux-Router` 的 `dev` 分支为例：

```bash
# 拉取与你仓库/分支匹配的 install.sh
curl -fsSL https://raw.githubusercontent.com/Chendemo12/Linux-Router/dev/install.sh \
  -o /tmp/linux-router-install.sh

# 用你的仓库 + dev 分支升级
sudo bash /tmp/linux-router-install.sh upgrade \
  --repo https://github.com/Chendemo12/Linux-Router.git \
  --branch dev
```

也可用环境变量（`LINUX_ROUTER_REPO_URL`、`LINUX_ROUTER_BRANCH`）代替参数：

```bash
sudo env \
  LINUX_ROUTER_REPO_URL=https://github.com/Chendemo12/Linux-Router.git \
  LINUX_ROUTER_BRANCH=dev \
  bash /tmp/linux-router-install.sh upgrade
```

如果安装目录不是默认的 `/opt/linux-router`（例如按本文使用 `$INSTALL_DIR`/`$DATA_DIR`），请补充：

```bash
sudo bash /tmp/linux-router-install.sh upgrade \
  --repo https://github.com/Chendemo12/Linux-Router.git \
  --branch dev \
  --install-dir "$INSTALL_DIR" \
  --data-dir "$DATA_DIR"
```

把示例中的仓库和分支替换成你自己的值。前提是目标设备能访问 GitHub，且对应分支的最新提交已推送。

如果设备无法访问 GitHub（完全离线），先在能联网的机器上下载仓库源码压缩包并拷到设备，然后用 `--archive` 指定这个本地 tar 包安装或升级。此模式不访问 GitHub、也不下载，离线可用；构建标识会写为 `build=local`，`branch` 则沿用 `--branch` 的值：

```bash
# 在设备上，源码包已位于 /tmp/app.tar.gz
sudo bash /tmp/linux-router-install.sh upgrade \
  --archive /tmp/app.tar.gz \
  --install-dir "$INSTALL_DIR" \
  --data-dir "$DATA_DIR"
```

`--archive` 与 `--archive-url` 互斥；注意 `--archive-url` 只接受 HTTPS 且仍需联网解析分支，不能用于离线安装。若需要彻底脱离 tar 包，也可按本文第 2 节的方式用 `cp -a` 从源码目录手动更新。

**离线升级之二：用代码目录（clone）替换（仅限已安装设备）**

如果设备离线，但你能把一份完整代码目录（例如一份 git clone，或用 `git bundle` 生成后通过 U 盘/局域网拷入）放到设备上，也可以直接替换应用目录完成升级。它**只能用于“已经用安装器部署过”的设备**——首次安装所需的服务账号、`$DATA_DIR` 数据、systemd unit、netplan/IPv4 转发等在首次 `install` 时已就位，此处只替换程序文件。

前提：
- 设备必须已经安装过，且安装目录默认即 `/opt/linux-router`、数据目录 `/var/lib/linux-router`（或与你的 `$INSTALL_DIR`/`$DATA_DIR` 一致）；
- 代码目录在设备上已就位（例如 `/opt/linux-router.new`）。

```bash
# 先备份当前版本，便于回滚
sudo mv /opt/linux-router /opt/linux-router.previous
sudo mv /opt/linux-router.new /opt/linux-router
sudo chown -R root:root /opt/linux-router

# 可选：写回 BUILD_INFO，否则页脚 build 显示 unknown
printf 'branch=%s\nbuild=%s\n' \
  "$(sudo git -C /opt/linux-router rev-parse --abbrev-ref HEAD 2>/dev/null || echo dev)" \
  "$(sudo git -C /opt/linux-router rev-parse --short HEAD 2>/dev/null || echo local)" \
  | sudo tee /opt/linux-router/BUILD_INFO >/dev/null

# 重启两个服务以加载新代码
sudo systemctl restart router-panel-agent.service router-panel.service
```

确认新版本运行正常后，再删除备份：`sudo rm -rf /opt/linux-router.previous`。若运行异常，用备份回滚并重启服务即可。

> **不要**改动或删除 `/var/lib/linux-router`——账号、密钥、LAN 配置等运行数据都在其中，且与代码目录相互独立；替换 `/opt/linux-router` 不会影响它。`.git` 目录可保留，方便以后在联网时 `sudo git -C /opt/linux-router pull` 后再重启。
