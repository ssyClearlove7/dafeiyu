# 大肥鱼 Dafeiyu

微信 AI 陪伴与群聊互动项目，包含持久记忆、人格与管理员教导、提醒、媒体处理和本地控制台的开发实现。

**当前版本：1.0.0rc1，开发预览。** 已有实现不代表全部完成真实微信、跨系统或长期稳定性验收；本仓库不承诺全部功能已可用于正式交付。当前独立接入组件不支持 GIF 表情发送。

## 源码与运行入口

- `dafy/`：产品入口与控制台；`V1.2.4/`：保留的原有模块。
- `requirements-product.txt`：产品运行依赖；`tools/prepare_component.py`：按清单下载并校验独立微信接入组件。
- `tools/build_product.py` 与 `packaging/`：安装和签名更新包构建资源。
- 源码入口为 `python -X utf8 -m dafy`。使用前需准备 Python、安装依赖及对应平台接入组件；这不等同于已发布的一键安装包。
- 使用者自行准备专用微信账号，并在控制台配置自己的模型服务、密钥和管理员信息；模型服务费用由使用者承担。

本仓库是当前开发源码的独立脱敏副本，未复制原仓库历史、私人配置、密钥、聊天记录、记忆库、运行缓存或个人素材。所含人设为已提交的基线；公开示例不包含已使用账号的当前教导和成长记录。

## 来源与许可

基于 [wc-chatbot 上游项目](https://github.com/chenchen070206-droid/a-chatbot-base-on-deepseek) 开发，保留原作者版权声明。项目原有代码及本仓库新增代码采用 [MIT License](LICENSE)，允许使用、修改、分发和销售，需保留相应版权及许可声明。

独立微信组件来自 [agent-wechat](https://github.com/thisnick/agent-wechat)，版本和校验信息见 `packaging/wechat.lock.json`。第三方软件各自适用其许可，见 `packaging/WECHAT-THIRD-PARTY-NOTICES.txt` 及相关文件中的声明；本仓库的 MIT 许可不替代第三方许可。
