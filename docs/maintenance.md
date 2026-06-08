# 维护规范

## 1. 提交内容

仓库应提交：

- `src`
- `data`
- `rules`
- `docs`
- `scripts`
- `requirements.txt`
- `README.md`
- 项目配置文件

仓库不提交：

- `build`
- `dist`
- `release`
- 运行日志
- 下载文件
- 备份文件
- 输出报告
- 现场第三方工具程序

## 2. 配置安全

提交前检查：

- `data/config.json` 中 `password` 应为空。
- 不提交现场真实账号密码。
- 不提交现场下载的 MAP、报告和日志。

## 3. 发布流程

1. 更新源码。
2. 执行 `python -m py_compile src\MapFanSim.py`。
3. 执行 `.\scripts\build_release.ps1`。
4. 启动 `release\MapFanSim\MapFanSim.exe` 做基础验证。
5. 将 `release\MapFanSim` 交付现场。

注意：PowerShell 只用于第 3 步构建。现场电脑拿到 `release\MapFanSim` 后直接运行 `MapFanSim.exe`，不需要 Python 或 PowerShell。

## 4. 服务器文件约束

服务器生效文件名固定为：

```text
slaverMB_1.map
```

所有自动上传逻辑必须覆盖该文件名。服务器备份文件必须使用其他名称，禁止与生效文件同名。
