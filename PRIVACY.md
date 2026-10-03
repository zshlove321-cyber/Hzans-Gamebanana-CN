# 本地数据与分享

本项目没有自建数据收集服务器。访问 GameBanana 会向该站点发送请求；启用 DeepSeek 翻译时，将待翻译内容发送到设置中指定的翻译服务。登录站点后的 Cookie 供本机内嵌浏览器与下载使用。

默认配置位置为 `%APPDATA%/banana-index/`。已有的 `%USERPROFILE%/.banana-index/` 或当前工作目录 `.banana-index/` 配置也可能继续使用；不可写时程序尝试其他可写位置。`BANANA_INDEX_CONFIG_DIR` 可以显式指定配置位置。浏览器还会使用 `%APPDATA%/banana-index/chromium/` 与 QtWebEngine 数据目录。

这些目录可能包含明文 API 密钥（settings.json）、站点 Cookie、浏览器登录状态、请求或翻译缓存、日志。密钥也可通过 `DEEPSEEK_API_KEY` 环境变量提供。不要分享这些文件或导出的环境变量。退出软件不会自动删除本机登录态。

本次发布采用明确允许的源码文件清单，未复制原项目的配置、浏览器档案、Cookie、缓存、日志、下载内容、截图、会话记录或代理任务记录。ZIP 与 EXE 使用同一份干净源码构建。校验报告仅显示结论与数量，不包含密钥或 Cookie 内容。

运行源码后会产生新的个人数据；`.gitignore` 能阻止常见文件被意外加入版本控制，但无法移除已经提交到历史中的秘密。后续发布仍需检查 Git 暂存内容。EXE 的运行数据不属于分享包，分享更新版本时应重新从干净源码构建。
