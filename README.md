# MapFanSim

MapFanSim 是一款用于风机 MAP 文件本地替换与云端同步的 Windows 桌面工具。项目基于 Python Tkinter 开发，支持内置 SFTP、WinSCP 辅助连接、风场规则管理、批量替换、报告生成、服务器备份和第三方工具启动。

## 核心能力

- 本地替换：处理 `input_maps` 中的 `.map` 文件，生成 `output_maps`、`reports` 和 `update/slaverMB_1.map`。
- 云端替换：连接服务器下载原始 MAP，先备份服务器文件，再生成并上传仿真后的 `slaverMB_1.map`。
- 风场规则：按风场维护 `device_maps.csv`、`relations.csv`、`extra_rules.txt`、`rule_profile.json`。
- 连接方式：支持内置 SFTP 和 WinSCP 自动化脚本，FlashFXP、OMTG、SSH/SFTP 终端作为现场辅助工具。
- 日志追踪：本地替换和云端替换页面均提供实时日志，日志文件写入 `logs`。

## 关键文件规则

服务器仿真服务只识别固定文件名：

```text
slaverMB_1.map
```

因此上传逻辑必须满足以下约束：

- 最终上传到服务器的文件名固定为 `slaverMB_1.map`。
- 上传前必须先备份服务器原始文件。
- 服务器备份不能命名为 `slaverMB_1.map`。
- 单条仿真关系备份名示例：`1-2-before.map`。
- 多条仿真关系备份名示例：`1-2_3-6-before.map`。
- `download` 和本地 `backup` 中保存的原始服务器文件保持原文件名 `slaverMB_1.map`。

## 目录结构

```text
.
├── src/
│   └── MapFanSim.py              # 应用入口和核心逻辑
├── data/                         # 默认配置和基础规则数据
├── rules/                        # 按风场维护的规则配置
├── input_maps/                   # 本地批量处理输入目录
├── output_maps/                  # 本地处理输出目录
├── update/                       # 待上传文件目录
├── download/                     # 云端下载原始 MAP 目录
├── backup/                       # 本地备份目录
├── reports/                      # CSV 报告目录
├── logs/                         # 运行日志目录
├── tools/                        # 可放置 WinSCP、FlashFXP、OMTG 等第三方工具
├── scripts/                      # 开发、构建、清理脚本
├── docs/                         # 项目文档
└── requirements.txt              # 开发和打包依赖
```

## 开发环境

推荐环境：

- Windows 10/11
- Python 3.10
- PowerShell 5.1 或更高版本

安装依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

运行源码：

```powershell
python src\MapFanSim.py
```

或执行：

```powershell
.\scripts\run_dev.ps1
```

## 打包发布

生成目录版发布包：

```powershell
.\scripts\build_release.ps1
```

输出目录：

```text
release\MapFanSim
```

现场电脑只需要复制整个 `release\MapFanSim` 文件夹并运行：

```text
MapFanSim.exe
```

## 成品下载

码云成品包：

[MapFanSim-windows-x64.zip](https://gitee.com/qssec/map/raw/master/artifacts/MapFanSim-windows-x64.zip)

源码仓库：

- 码云：[https://gitee.com/qssec/map](https://gitee.com/qssec/map)
- GitHub：[https://github.com/qssec1](https://github.com/qssec1)

## 配置说明

默认配置文件：

```text
data/config.json
```

主要字段：

- `remoteMode`：远程模式，推荐 `builtin_sftp`，也支持 `winscp`。
- `host`：服务器地址。
- `port`：SFTP 端口。
- `username`：登录用户。
- `password`：登录密码。
- `remoteDir`：服务器 MAP 所在目录。
- `remoteFile`：服务器 MAP 文件名，现场必须保持为 `slaverMB_1.map`。
- `winscpDir`、`flashfxpDir`、`omtgDir`：第三方工具安装目录。

密码会写入本地配置文件。提交公开仓库前应确认 `password` 为空，或使用本地覆盖配置管理。

## 文档

- [现场使用说明](docs/user-guide.md)
- [构建与发布](docs/build-and-release.md)
- [规则配置说明](docs/rules.md)
- [维护规范](docs/maintenance.md)
