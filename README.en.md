# Workflow Canvas

A visual AI project workbench for DeepSeek Harness. Define a goal, connect AI steps, keep discussing and revising, and deliver versioned files.

**v0.17.0 RC1 — free, open-source release candidate.** Tested functionality and remaining environment coverage are reported separately. The plugin is free; model accounts and optional video tools retain their own terms.

Built-in starters: sources to article, a configurable 30-second motion-graphics workflow, and a general Markdown/JSON file task. Projects keep their own goals, inputs, module conversations and results. Revisions preserve old files and invalidate only dependent steps. Current work and the selected delivery run are separate.

## Quick start

The tested baseline is Windows 11 x64 with DeepSeek Harness 0.2.1-rc.5. Install and open Harness once, and install free PowerShell 7. Download the complete Windows ZIP from Releases, verify SHA256SUMS, extract it to a stable directory, and run `install.cmd`. Restart Harness and open Canvas.

Choose Sources to Article, start with the original synthetic example, then replace it with project materials. Answer pending questions in the module workspace, review the result, and download the delivery. Python and locked core dependencies are bundled; no global Python or Node is needed for document tasks. Model execution still requires your Harness account and a network connection.

Video additionally requires Node, Chrome, FFmpeg and Remotion. Configure local paths in Project Overview → Environment and Video Tools. Remotion has separate [license conditions](https://www.remotion.dev/docs/license).

Data defaults to `%USERPROFILE%\.dsh-workflow-canvas`; machine configuration is excluded from releases. Upgrades preserve user definitions and data. Uninstall unregisters this plugin and retains data by default. Project sharing excludes accounts, conversation history, outputs and tool configuration; materials are opt-in.

## Development

Use Python 3.12, Node 24 and PowerShell 7. Create a virtual environment, install hash-locked `requirements-dev.lock.txt` and `requirements.lock.txt` separately, run `python scripts/verify.py`, then `python scripts/build.py --output dist`. Builds fetch official hash-locked archives and normalize ZIP metadata. Runtime binaries and user data do not belong in Git.

Use the repository issue templates for bugs, feature requests and compatibility. Include version/build and reproducible steps, not private conversations. See [CONTRIBUTING](CONTRIBUTING.md), [SECURITY](SECURITY.md), [architecture](docs/架构.md), and [Chinese setup guide](docs/安装与快速开始.md).

Project code: [MIT](LICENSE). Bundled components: [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md).
