# 构建与发布

说明：PowerShell 5.1 或更高版本只用于开发者构建发布包。现场电脑运行已经打包好的 `MapFanSim.exe` 时，不需要安装 Python，也不需要安装 PowerShell。

## 1. 安装依赖

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

也可以执行：

```powershell
.\scripts\install_dev.ps1
```

## 2. 本地运行

```powershell
python src\MapFanSim.py
```

或执行：

```powershell
.\scripts\run_dev.ps1
```

## 3. 生成发布包

```powershell
.\scripts\build_release.ps1
```

输出：

```text
release\MapFanSim_构建时间
```

也可指定一个尚不存在的输出目录，再压缩该目录：

```powershell
.\scripts\build_release.ps1 -OutputDirectory release\MapFanSim_2026.09.18.1
Compress-Archive -Path release\MapFanSim_2026.09.18.1\* -DestinationPath artifacts\MapFanSim-windows-x64-v2026.09.18.1.zip
```

码云下载地址：

```text
https://gitee.com/qssec/map/blob/master/artifacts/MapFanSim-windows-x64.zip
```

每次发布同时保留日期命名成品，例如 `artifacts/MapFanSim-windows-x64-v2026.09.18.zip`。

发布包包含：

- `MapFanSim.exe`
- `_internal`
- `data`
- `rules`
- `input_maps`
- `output_maps`
- `download`
- `update`
- `backup`
- `reports`
- `logs`
- `tools`

## 4. 现场部署

1. 将 `release\MapFanSim` 整个文件夹复制到现场电脑。
2. 双击运行 `MapFanSim.exe`。
3. 在“设置”页面填写现场服务器连接信息。
4. 使用“测试连接”验证服务器目录和 `slaverMB_1.map` 是否可访问。

现场电脑不需要执行 `.ps1` 脚本，也不需要 PowerShell 5.1。

## 5. 清理构建产物

```powershell
.\scripts\clean.ps1
```

清理内容：

- `build`
- `dist`
- `release`
- Python 缓存
- 运行日志和临时输出文件
