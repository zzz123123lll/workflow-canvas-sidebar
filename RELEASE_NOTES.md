# v0.17.0 RC3

修复 RC2 超出市场解压体积上限的问题，保留现有工作流功能。

- 移除独立 Harness SDK 的大型可执行文件及传递依赖；正常 AI 步骤继续通过已安装 Harness 的宿主子会话和账号执行。
- 内置 CPython 3.12.10 和 pypdf 6.14.2；资料/文章/文件任务无需全局 Python 或 Node。
- 无宿主连接时提示打开或重启 Harness，保留原会话与文件，不要求另配 API key。
- ZIP 与 npm TGZ 使用相同文件；打包时验证解压总字节不超过 256 MiB，并写入 npm-manifest.json。
- 不通过安装钩子或首次启动下载、解压被移除的独立运行时。视频仍需使用者配置 Node、Chrome、FFmpeg、Remotion。

安装候选版：`dsh plugin --profile desktop add dsh-workflow-canvas-sidebar@0.17.0-rc.3`，完成后重启 Harness。

受测宿主基线 Windows 11 x64 / DeepSeek Harness 0.2.1-rc.5。构建、测试及发布证据随本次发行提供。RC2 的既有真实文章交付与视频示例保留作功能证据；新版验证范围以本次记录为准，不声称有外部真人试用。

市场[原申请 #201](https://github.com/polang233/dsh-plugin-index/issues/201)因 RC2 体积未收录，尚未完成代码复核；RC3 作为精简新版继续申请，不代表已获收录。
