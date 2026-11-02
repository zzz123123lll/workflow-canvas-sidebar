# Third-party notices

项目自己的代码采用根目录 MIT 许可。以下组件保留原许可，不被重新标为本项目 MIT。精确来源、版本与上游归档 SHA-256 见 dependencies.lock.json；许可证原文另附于 third_party/licenses，完整包内还保留运行时各自的原始 notices。

|组件|版本|许可|来源|
|---|---|---|---|
|CPython embeddable|3.12.10|Python-2.0 AND bundled-component-notices|https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip|
|annotated-types|0.8.0|MIT|https://pypi.org/project/annotated-types/0.8.0/|
|deepseek-harness-runtime-bin|0.2.1rc2|MIT|https://pypi.org/project/deepseek-harness-runtime-bin/0.2.1rc2/|
|deepseek-harness-sdk|0.2.1rc2|MIT|https://pypi.org/project/deepseek-harness-sdk/0.2.1rc2/|
|pydantic|2.14.2|MIT|https://pypi.org/project/pydantic/2.14.2/|
|pydantic_core|2.50.2|MIT|https://pypi.org/project/pydantic_core/2.50.2/|
|pypdf|6.14.2|BSD-3-Clause|https://pypi.org/project/pypdf/6.14.2/|
|typing_extensions|4.16.0|PSF-2.0|https://pypi.org/project/typing_extensions/4.16.0/|
|typing-inspection|0.4.4|MIT|https://pypi.org/project/typing-inspection/0.4.4/|

DeepSeek Harness runtime wheel 的原始 THIRD_PARTY_NOTICES.md 随包保留，描述其预编译组件与各自许可。Python LICENSE.txt 包含Python本体和捆绑组件的原始条款。构建不修改这些组件源码，唯一运行时配置变化是 python312._pth。

旧版11份第三方技能文档不随此次源码或安装包分发；MIT许可的qc-skill 0.1.3源码保留在server/vendor/qc，供旧程序QC模块兼容，包含其原始LICENSE与版权声明。来源固定为kajisho5/qc-skill提交69d904738256116d4ee8c496aa5c1c02a756007a，归档哈希、许可哈希与源码树哈希见dependencies.lock.json的vendored条目。逐项处理见docs/第三方技能分发处理.json；用户既有安装不会被删除。首发模块的职责与合成示例由本项目原创。

Node、Chrome、FFmpeg和Remotion是可选外部视频依赖，不在本包中。Remotion有独立许可条件，按实际用途核对 [官方许可页](https://www.remotion.dev/docs/license)；本项目MIT不改变它的条件。系统字体仅用于本机渲染，不分发字体文件。模型调用使用使用者的Harness账号，其服务条件与费用独立。
