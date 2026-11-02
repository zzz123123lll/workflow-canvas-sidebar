# Workflow Canvas

A visual AI project workbench for DeepSeek Harness. Define a goal, connect AI steps, keep discussing and revising, and deliver versioned files.

**Source version: v0.17.0 RC2 — marketplace installation candidate.** RC1 is publicly released; see [marketplace status and submission notes](docs/插件市场.md) for RC2 npm publication and listing status. The plugin is free; model accounts and optional video tools retain their own terms.

Built-in starters: sources to article, a configurable 30-second motion-graphics workflow, and a general Markdown/JSON file task. Projects keep their own goals, inputs, module conversations and results. Revisions preserve old files and invalidate only dependent steps. Current work and the selected delivery run are separate.

## Quick start

The tested baseline is Windows 11 x64 with DeepSeek Harness 0.2.1-rc.5. Install and open Harness once.

**Marketplace package, after the npm candidate is published:** add `dsh-workflow-canvas-sidebar@0.17.0-rc.2` in Harness → Plugins → Add Plugin, or run:

```powershell
dsh plugin --profile desktop add dsh-workflow-canvas-sidebar@0.17.0-rc.2
```

Restart Harness and open Canvas. The package includes Python and locked dependencies; it needs neither `install.cmd` nor an automatic `install` / `postinstall` script. RC2 uses the npm `next` tag, so select the explicit candidate version. While the marketplace listing is pending, the published ZIP remains available.

**ZIP installation:** install free PowerShell 7, download the complete Windows ZIP from Releases, verify SHA256SUMS, extract it to a stable directory, and run `install.cmd`. Restart Harness and open Canvas. The displayed version should match your downloaded package.

Choose Sources to Article, start with the original synthetic example, then replace it with project materials. Answer pending questions in the module workspace, review the result, and download the delivery. Python and locked core dependencies are bundled; no global Python or Node is needed for document tasks. Model execution still requires your Harness account and a network connection.

Video additionally requires Node, Chrome, FFmpeg and Remotion. Configure local paths in Project Overview → Environment and Video Tools. Remotion has separate [license conditions](https://www.remotion.dev/docs/license).

Data defaults to `%USERPROFILE%\.dsh-workflow-canvas`; machine configuration is excluded from releases. Upgrades preserve user definitions and data. Uninstall unregisters this plugin and retains data by default. Project sharing excludes accounts, conversation history, outputs and tool configuration; materials are opt-in.

Project materials are processed by the models and tools configured in Harness when you run tasks. The plugin has no separate model account. Diagnostics are generated locally and are never uploaded automatically. If your ZIP installation uses a custom data directory, follow the [marketplace migration notes](docs/插件市场.md) to keep using it.

## Development

Use Python 3.12, Node 24 and PowerShell 7. Create a virtual environment, install hash-locked `requirements-dev.lock.txt` and `requirements.lock.txt` separately, run `python scripts/verify.py`, then `python scripts/build.py --output dist`. Run `python scripts/npm_package.py --release-dir dist` to package the complete ZIP files into the npm `.tgz`, with `npm-manifest.json`. Builds use official hash-locked archives. Runtime binaries and user data do not belong in Git.

Use the repository issue templates for bugs, feature requests and compatibility. Include version/build and reproducible steps, not private conversations. See [CONTRIBUTING](CONTRIBUTING.md), [SECURITY](SECURITY.md), [architecture](docs/架构.md), and [Chinese setup guide](docs/安装与快速开始.md).

Project code: [MIT](LICENSE). Bundled components: [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md).
