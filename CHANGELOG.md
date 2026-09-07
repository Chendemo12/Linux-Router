# Changelog

本项目采用[语义化版本](https://semver.org/lang/zh-CN/)（Semantic Versioning）。版本号常量定义在 `router_panel/core.py` 的 `VERSION`，并在「设置」页页脚显示。

## [1.0.0] - 2026-09-08

首个语义化版本。记录自项目起步以来的主要改动，并引入正式的版本号标识。

### 新增

- 引入语义化版本号 `VERSION`（`router_panel/core.py`），随 `get_build_info()` 提供；「设置」页页脚现显示 `Version / Branch / Build` 三项。
- 新增 `static/icons/git-tag.svg` 图标，用于页脚版本号展示。

### 修复

- 修复「热点」开启死循环：当无线网卡原本由外部 hostapd/RaspAP 占用时，「交给 NetworkManager 接管」只修改了 NetworkManager 侧配置、未停用 hostapd，导致接管后仍报「hostapd 正在占用无线网卡」而无法开启热点。现接管操作会先自动 `stop` 并 `disable` 冲突的外部 hostapd 服务，真正把网卡释放给 NetworkManager。
- 修正上述场景下接管成功提示：当确有停用外部 hostapd 时，提示为「已停用外部 hostapd 并将 <接口> 交给 NetworkManager 接管」。

### 说明

- 早期版本没有语义版本号；「设置」页页脚原仅显示由 `install.sh` 在安装/升级时按远端 git 分支提交自动写入 `BUILD_INFO` 的 `branch` 与 `build`（7 位提交短 SHA）。`build` 仍保持自动生成，与新增的 `version` 相互独立。
