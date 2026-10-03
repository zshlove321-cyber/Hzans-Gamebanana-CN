# Hzans-Gamebanana-CN · GameBanana 中文索引

Windows 桌面客户端，提供 GameBanana 中文索引、条目详情、内嵌网页浏览和可选的 DeepSeek 汉化。当前版本：**v0.1.2**。项目与 GameBanana、DeepSeek 无官方关联。

由 **Hzans** 于 [GitHub](https://github.com/zshlove321-cyber/Hzans-Gamebanana-CN) 开源发布。**软件免费，没有任何付费购买渠道，谨防诈骗。**

## 功能

- 首次启动显示开源及防诈骗提示，点击「我已知晓」、× 或按 Esc 关闭后不再自动弹出；「设置 → 关于」可随时查看作者与赞助信息。
- 按游戏、条目类型和排序浏览、搜索，查看作者说明、预览图和文件。
- 同时提供「跳转下载页」和「直接下载」，直接下载无需先打开原网页。
- 蓝色下载进度条、取消下载、失败或取消后重试、关闭下载提示。
- 网页标签可关闭、重新打开；主窗口点击一次右上角 × 即关闭。
- 使用自己的 DeepSeek API 密钥翻译字段与网页；无密钥也可以浏览和下载。

## 使用 EXE

从 [GitHub Releases](https://github.com/zshlove321-cyber/Hzans-Gamebanana-CN/releases) 下载 `GameBananaIndex-v0.1.2-win-x64.zip`，解压后双击同名 EXE。Windows 10/11 64 位，无需安装 Python，也不需要管理员权限。首次启动需要解压内置浏览器组件，请等待片刻。

EXE 未进行商业代码签名。只从你认可的项目发布页下载，并与发布的 SHA256 校验值核对。不要以管理员身份启动。

翻译功能在「设置」中填写自己的 API 密钥，调用费用由你的服务账户承担。站点访问、下载与登录受 GameBanana 自身规则和网络状况影响。所有浏览器状态与配置在使用者本机生成，发布包不携带发布者的账号。

公开条目通常无需登录。详情遇到临时空响应会自动重试；仅当站点明确要求认证时才弹窗提示「网页登录」。请在软件内登录，返回条目后重新查看；网页会话会自动同步给索引请求。网页验证单独提示，不将普通接口异常、404 或权限错误一律当成未登录。

## 从源码运行

需要 Python 3.10–3.14 64 位，推荐 Python 3.10。命令在源码根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m client.app
```

也可以双击 `run.bat`，它会创建虚拟环境、安装锁定版本的依赖，然后启动。`--safe-mode` 可关闭内嵌浏览器；`--no-webengine` 可用于排查浏览器故障。

## 构建 EXE

在 Windows x64 上执行（macOS/Linux 无法交叉生成这个 Windows EXE）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\packaging\build.ps1 -Python .\.venv\Scripts\python.exe
```

生成 `dist/GameBananaIndex-v0.1.2-win-x64.exe`。构建只收集 Python 模块和明确列出的静态资源，禁止加入配置目录、Cookie、缓存或整个工作目录。分享时附上本项目 LICENSE、THIRD_PARTY_NOTICES.md 和 `licenses/`；MIT 只适用于本项目自有源码，第三方组件遵循各自许可。

版本变更时同步 `client/__init__.py`、`packaging/version_info.txt` 的产品/文件版本和 README/CHANGELOG。文件名会根据源码版本自动生成。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*download*.py"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_window_close.py"
.\.venv\Scripts\python.exe tests/test_ui_smoke.py
```

离线测试使用伪造的 API 返回和明确标注的测试密钥，不需要实际服务密钥。完整 EXE 离线健康检查与隐私扫描见 `tools/verify_public_release.py`。

## 发布到 GitHub

这个源码目录就是仓库根目录。只上传此目录的内容；EXE ZIP 与 SHA256SUMS.txt 放到版本标签 `v0.1.2` 的 Releases 附件，不把发布 EXE 和日常配置加入 Git 仓库。提交前检查暂存区，避免上传运行后产生的个人文件。

仓库已准备 README、MIT LICENSE、CHANGELOG、贡献说明、安全与隐私说明，以及 Windows 构建工作流。工作流只构建并保存 artifact，不自动创建公开 Release。

## 许可与数据

自有源码采用 [MIT](LICENSE)。第三方许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。用户数据位置与分享注意事项见 [PRIVACY.md](PRIVACY.md)。

## 支持作者

创作和维护不易，如果这个项目对你有帮助，欢迎请 **Hzans** 喝杯咖啡 ☕。赞助完全自愿，不影响软件的免费使用和功能。

[爱发电 · 赞助 Hzans](https://afdian.com/a/Hzans777)
