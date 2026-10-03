# 第三方组件

本项目 MIT 许可证只涵盖项目自有源码，不改变以下组件的许可。发布 EXE 使用未修改的上游二进制库。

| 组件 | 版本 | 许可与来源 |
| --- | --- | --- |
| Python | 3.10.11 | PSF License；https://www.python.org/downloads/release/python-31011/ |
| PySide6 / Shiboken6 | 6.11.2 | LGPL-3.0 / GPL 等可选许可；本发行使用 LGPL-3.0；https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt / QtWebEngine | 6.11.2 | Qt 模块依 LGPL-3.0 等适用许可；WebEngine 包含 Chromium 第三方组件；https://code.qt.io/cgit/qt/qtwebengine.git/ |
| requests | 2.34.2 | Apache-2.0；https://github.com/psf/requests |
| urllib3 | 2.7.0 | MIT；https://github.com/urllib3/urllib3 |
| certifi | 2026.7.22 | MPL-2.0（证书包）；https://github.com/certifi/python-certifi |
| charset-normalizer | 3.5.1 | MIT；https://github.com/jawah/charset_normalizer |
| idna | 3.19 | BSD-3-Clause；https://github.com/kjd/idna |
| PyInstaller bootloader | 6.22.3 | GPL-2.0+ 带允许独立分发组合程序的 Bootloader Exception；https://github.com/pyinstaller/pyinstaller |

完整许可文本随包放在 `licenses/` 中，QtWebEngine/Chromium 声明位于 `licenses/QtWebEngine/LICENSE.Chromium`。Chromium 各组件的具体版权与许可文本保存在 `licenses/QtWebEngine/third-party/`，基础 Qt 组件的第三方声明保存在 `licenses/Qt-base-third-party/`（Qt 官方文档静态副本；含对应 GFDL 文档许可）。本发行排除了未使用的 QML 插件及 Charts、Graphs、Quick3D、VirtualKeyboard 等可选模块。网页提取策略参考了 kiss-translator 的公开设计，相关项目：https://github.com/fishjar/kiss-translator 。不将任何第三方代码重新声明为 MIT。

## 修改及重新链接

允许为调试 LGPL 库修改或逆向分析本程序；本发行没有限制这种行为。所有 Qt/PySide 库均未修改。匹配版本的源代码可以从上述上游获取（PySide `v6.11.2`、Qt `v6.11.2`；Python 使用对应发行源码）。QtWebEngine 的 Chromium 部分包含在 QtWebEngine 上游源代码中。

此处提供本项目完整可构建源码和打包配置。需要替换 LGPL 库时，可在虚拟环境安装自己构建的兼容 PySide6/Qt 二进制（DLL、插件、资源），再按 README 重新构建 EXE；无需保留 requirements 中对应的上游版本锁定。也可将 spec 改为 PyInstaller onedir 布局，直接替换兼容的动态库后运行。单文件版只是运行时将动态库解压到临时目录后加载。
