# 工作流画布 · Workflow Canvas

把一个目标交给多个 AI 步骤协作，持续讨论、局部修改，最后拿到能使用的文件。

这是 DeepSeek Harness 的可视化项目工作台。适合研究写作、资料整理和配置后的短视频制作。项目保存目标、资料、流程、会话和成果版本；画布负责分工，模块工作区负责协作，成果页负责阅读、审阅与下载。

**当前源码：v0.17.0 RC2，市场安装候选版。** RC1 已公开发布，RC2 的 npm 发布与市场收录状态见[插件市场说明](docs/插件市场.md)。欢迎直接试用并反馈，不收取插件费用；模型服务和可选视频工具按各自条件使用。

[English](README.en.md) · [安装与快速开始](docs/安装与快速开始.md) · [插件市场](docs/插件市场.md) · [视频配置](docs/视频配置.md) · [升级与恢复](docs/升级与恢复.md) · [兼容与限制](docs/兼容矩阵.md)

## 你可以做什么

- **资料到文章**：读资料、核对事实、写正文和来源，审阅后交付。
- **简短动态图形视频**：分镜/字幕、原创工程、真实渲染，再改片尾或字幕。
- **自定义文件任务**：修改一个通用 AI 模块，让它输出 Markdown、JSON 或自己的文件。
- 继续同一模块的对话；原生聊天修改回到画布，登记为新版本。
- 上游改稿只更新受影响步骤，旧版保留；共享知识经确认后用于后续项目。
- 当前工作和交付版分开选择，旧待办有明确的历史/替代状态。

## 安装

受测平台为 Windows 11 x64，宿主基线 DeepSeek Harness 0.2.1-rc.5。请先安装并启动 Harness 一次。

**市场渠道**：在 Harness「插件 → 添加插件」填写 `dsh-workflow-canvas-sidebar@0.17.0-rc.2`，或运行：

```powershell
dsh plugin --profile desktop add dsh-workflow-canvas-sidebar@0.17.0-rc.2
```

重启 Harness 后打开“画布”。市场包包含 Python 和锁定依赖，无需额外运行 `install.cmd`，也没有安装时自动执行的 `install` / `postinstall` 脚本。RC2 使用 npm 的 `next` 候选标签，请明确指定版本；现在可直接按包名安装，市场收录进度见[申请 #201](https://github.com/polang233/dsh-plugin-index/issues/201)。

**ZIP 渠道**：先安装免费的 PowerShell 7，然后：

1. 从本仓库的 **Releases** 下载 Windows x64 完整 ZIP，并核对 SHA256SUMS。
2. 解压到固定目录，例如 `C:\Tools\WorkflowCanvas`，双击 `install.cmd`。
3. 安装完成后重启 Harness，打开左侧“画布”，核对显示版本与下载包一致。
4. 在项目总览选“资料到文章”，先用原创合成示例完成真实 AI 任务，再换成自己的资料。

完整包自带 Python 与锁定依赖，核心安装无需全局Python或Node，依赖齐全时不会再次联网pip安装。模型运行仍需联网和Harness账号。Node、Chrome、FFmpeg及Remotion仅用于视频，见[配置步骤](docs/视频配置.md)。

第一次目标可写：“把资料整理为给新志愿者看的300字说明，保留来源，不补造日期地点。”需要回答时打开对应模块；普通讨论、回复待办、创建修订是三个不同操作。

## 数据与升级

默认数据位于 `%USERPROFILE%\.dsh-workflow-canvas`，机器路径保存在不入发行包的 `canvas-home.json`。安装只登记本插件；不会更改宿主账号或清空会话。升级覆盖插件文件时保留此配置，再运行安装程序；内置模板只补缺，不覆盖用户定义。

任务资料会按你的操作交给 Harness 配置的模型或工具处理；本插件不另设模型账号。诊断报告在本地生成，不会自动上传。市场安装沿用已有数据目录；从自定义目录的 ZIP 安装切换时，按[市场说明](docs/插件市场.md)继续使用原目录。

卸载默认解除插件登记并保留数据；`-ShowPlan` 可先查看。分享项目默认只导出定义，项目资料需主动选择；不附账号、会话或机器工具配置。

## 开发与贡献

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.lock.txt
.\.venv\Scripts\python -m pip install --require-hashes -r requirements.lock.txt
.\.venv\Scripts\python scripts/verify.py
.\.venv\Scripts\python scripts/build.py --output dist
.\.venv\Scripts\python scripts/npm_package.py --release-dir dist
```

开发依赖与运行锁分别安装，详见[开发指南](docs/开发指南.md)。构建从官方锁定的Python ZIP和wheel组装运行时，不复制维护者的环境；npm 打包从完整 ZIP 取得相同文件，产物为 `.tgz` 与 `npm-manifest.json`。manifest 记录源码摘要、提交和资产哈希。无账号 CI 只验证其实际覆盖范围。

遇到问题请使用仓库的Bug/兼容报告模板，附版本、build、宿主版本和复现步骤。项目总览可以生成本地脱敏诊断，不要求提交私有对话。详见 [CONTRIBUTING](CONTRIBUTING.md) 与 [SECURITY](SECURITY.md)。

本项目代码为 [MIT](LICENSE)；第三方组件见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。
