# PyInstaller `onedir` Packaging

Windows 打包主路线已经切到 `PyInstaller onedir`。

## 产物形态

- `dist/VidMind/VidMind.exe`
- `dist/VidMind/_internal/...`
- `dist/VidMind/web/static/...`
- `dist/VidMind/runtime/base/...`
- `dist/VidMind/bin/ffmpeg.exe`

## 构建前提

- 使用 Python `3.12`
- 当前环境可安装本仓库的三个本地包
- 如果要随包分发 `ffmpeg`，请保证本机 `PATH` 里已有 `ffmpeg.exe` / `ffprobe.exe`
  - 或设置 `VIDEO_SUM_FFMPEG_DIR`

## 一键构建

```powershell
python .\packaging\pyinstaller\build_onedir.py
```

脚本会自动执行这些步骤：

1. 安装构建期依赖和本地包
2. 生成 sidecar managed runtime：`build/pyinstaller/runtime/base`
3. 尝试收集 `ffmpeg.exe` 与 `ffprobe.exe`
4. 执行 `PyInstaller`

## CUDA 运行时策略

- `onedir` 基础包默认只保证 CPU 路径可运行
- 设置页的一键安装会把 CUDA 版 `torch` 装到用户目录下的 managed runtime
- 本地 ASR 默认不随基础包分发；如用户需要，可在设置页按需安装到当前 runtime
- 安装路径默认在 `%LOCALAPPDATA%/vidmind/runtime/gpu-cu12x`
- 安装完成后需要重启应用生效

## 运行时目录

- 用户数据目录：`%LOCALAPPDATA%/vidmind/data`
- managed runtime：`%LOCALAPPDATA%/vidmind/runtime`
- 打包内置 seed runtime：`dist/VidMind/runtime/base`

## 旧版本迁移

- VidMind 会在首次启动时从旧目录 `%LOCALAPPDATA%/briefvid` 复制缺失的数据、任务产物、Cookies 和 managed runtime 到 `%LOCALAPPDATA%/vidmind`
- 迁移采用”只补缺、不覆盖、不删除旧目录”的策略，避免覆盖用户已经在新目录中产生的数据
- 桌面端偏好和登录会话会从旧的 Electron `BriefVid` userData 目录迁移到新的 `VidMind` userData 目录
