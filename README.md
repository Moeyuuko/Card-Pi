# Card Pi

Card Pi 是面向 CardputerZero（ARM64）的第三方 Pi 客户端，由独立开发者制作，非 Pi 官方出品。它在设备本机直接调用原版 [Pi](https://pi.dev/)，并把对话、项目和会话操作适配到 320×170 屏幕与实体键盘。

Card Pi 和终端 Pi 在同一设备账号下共用 Pi 的模型登录、配置、项目扩展和会话。配置方法沿用 [Pi 官方文档](https://docs.pi.dev/latest/configuration)。你可以继续在设备终端、ZeroDesk Shell 或 SSH 中直接运行 Pi，也可以用 Card Pi 打开同一项目下的已有会话。

## 界面演示

| 首页 | Markdown 聊天 |
| --- | --- |
| ![Card Pi 首页](screenshots/demo-home.png) | ![Card Pi Markdown 聊天](screenshots/demo-chat.png) |
| **拼音候选** | **显示设置** |
| ![Rime 拼音候选栏](screenshots/demo-pinyin.png) | ![Card Pi 显示设置](screenshots/demo-display.png) |

## 功能

- **小屏对话**。流式显示 Pi 回复。输入框可用 `↑/↓` 回找已发送的输入。按 `Fn+L`（外接键盘的 `Page Up`）或从会话菜单进入全屏历史。用方向键或 `F/X` 移动，长按连续滚动，`Fn+L/M` 整页翻动。命令默认以单行摘要显示，`[+]` 表示有折叠详情。在全屏历史中选中该行并按 `OK`，可查看完整命令、状态和输出。
- **直接操作 Pi**。菜单提供模型切换、常规设置、思考强度、模型轮换范围和服务商登录。按 `/` 打开 Pi 命令菜单，选择「/skill 选择技能」即可将技能指令填入聊天输入框。菜单还支持切换模型、设置思考等级、管理会话和选择提示词模板。需要登录或打开 Pi 原生设置时，可从同一菜单进入原生界面。
- **项目和会话管理**。浏览文件夹或输入路径启动 Pi，设置默认项目，继续最近项目。会话支持新建、恢复、改名，以及移入回收站后恢复或永久删除。
- **Rime（中州韵）离线拼音**。支持全拼、自然码双拼、简繁切换、中英文标点和候选翻页。界面可独立切换简体中文和 English。
- **检测和安装 Pi**。启动时复用可用的 Pi，缺失时提供安装按钮，从官方渠道下载到当前用户的目录。

## 安装

将 ARM64 的 `.deb` 复制到 CardputerZero，在包所在目录执行以下命令。已有旧版可用同一条命令升级。

```sh
sudo apt install ./card-pi_0.0.6_arm64.deb
```

未检测到 Pi 时，选择「安装 Pi」即可在 Card Pi 的内置终端运行 Pi 官方 Linux 安装器。设备没有兼容的 Node.js 或 npm 时，按官方安装器的提示选择安装方式。如果安装器询问是否立即启动 Pi，可输入 **n** 回到 Card Pi 的安装流程。安装器退出后按 **Esc** 重新检测。可从「设置 → Pi 设置 → Pi 核心信息」查看实际可执行文件路径。 [Pi 官方安装指南](https://docs.pi.dev/latest/quickstart#1-install-pi)

从源码构建时，在项目根目录运行 `python3 packaging/build_deb.py`。安装包会生成在 `dist/`。

## 快速开始

1. 在设备 HOME 打开 **Card Pi**。如果没有检测到 Pi，按 **6 安装**或 **OK** 打开官方安装器，按屏幕提示完成安装。安装器退出后按 **Esc** 重新检测。如果设备已有 Pi 但未被找到，可按 **7 指定 Pi** 选择可执行文件。
2. 首次配置模型时，进入「设置 → Pi 设置 → 登录服务商」，按 Pi 的提示完成登录，再按 **Ctrl+Q** 返回。已有 Pi 登录可直接使用。
3. 选择项目目录，然后新建或恢复会话。在项目目录页可按 **7** 输入绝对路径。
4. 在对话中按 **Ctrl+Space** 切换中英文。输入 `nihao` 后按 **Space** 或 **1–5** 选字，再按 **OK** 发送。候选栏打开时，直接按 **Enter** 会把原始字母放进输入框，继续保留中文模式。候选栏打开时可用 **Fn+F/X** 或 **Fn+L/M** 翻页。
5. 按 **Tab** 打开会话菜单。各级菜单可长按 **↑/↓** 或 **F/X** 连续移动，松键即停。全屏历史中按 **Esc** 返回对话，任意页面长按 **Esc** 返回 HOME。

首页左侧显示 Pi 核心版本，右上角每 8 秒在 Card Pi 版本与本机日期、时间和电量之间切换。聊天底栏左侧轮播当前模型、思考等级、缓存读写、上下文用量、输入输出 Token 和费用。右侧轮播快捷键，生成中显示 Esc 停止。首页的「升级 Pi」会执行当前 Pi 的 `pi update` 并实时显示终端输出，运行结束后按 **Esc** 返回。通过官方安装器安装的 Pi 支持用此命令更新。通过其他包管理器安装的 Pi，按该包管理器的方式更新。 [Pi 更新命令](https://docs.pi.dev/latest/cli#update-pi-or-packages)

## 设置

- **Pi 设置**。从首页「设置 → Pi 设置」或对话的 **Tab → Pi 设置** 打开。进入 Pi 原生页面后按 **Ctrl+Q** 返回菜单，Pi 自行保存原生设置。
- **显示设置**。从首页「设置 → 显示设置」或对话「Tab → 显示设置」进入。字体边缘默认「像素」，也可切换为「平滑」开启抗锯齿。打字机效果默认中速，可选关闭、慢速、中速或快速。生成结束后按 Esc 可立即显示剩余文字。Markdown 默认开启，支持标题、强调、列表、引用、链接文字、代码和简化表格。关闭后按原文显示。聊天字号可选 11、13、15 px，行距可选紧凑或宽松，适用于对话和全屏历史。终端字号可选 8、9、11 px，适用于 Pi 原生命令和升级日志。
- **输入法设置 · Rime 中州韵**。聊天、会话重命名、目录路径、Pi 可执行文件路径和扩展文本框都可按 **Ctrl+Space** 切换中英文。输入拼音后按 **Space** 或数字键选字，也可以按 **Enter** 输入原始字母。候选栏关闭后，按 **OK** 提交。可选简繁、全拼或双拼、候选数、中英文标点、字符宽度、模糊音、补全、整句候选和词频学习。恢复默认会保留已经学习的词库。首次使用某个方案时，设备可能需要短暂构建词库。
- **会话管理**。从首页「会话管理」选择项目。在会话列表选中会话，按 **7** 改名，按 **8** 确认移入回收站。进入「会话回收站」，选中记录后按 **OK** 恢复，或按 **8** 打开永久删除确认页。永久删除后无法恢复。

## 许可证

Card Pi 自有代码采用 [MIT 许可证](LICENSE)。第三方组件适用各自的许可证。

## 致谢

- 感谢 Mario Zechner、Earendil 和 Pi 贡献者提供可直接调用的 Pi 核心、RPC 接口及原生终端界面。 [Pi](https://github.com/earendil-works/pi)
- 感谢 Rime 和各方案维护者提供离线拼音引擎、简繁词库和双拼方案。 [Rime](https://rime.im/) · [袖珍简化字拼音](https://github.com/rime/rime-pinyin-simp) · [朙月拼音](https://github.com/rime/rime-luna-pinyin) · [自然码双拼](https://github.com/rime/rime-double-pinyin)
- 感谢 M5Stack、CardputerZero 社区和 Card HY2 项目提供设备接入参考，也感谢 Pillow、pyte 及字体维护者提供渲染和终端支持。 [CardputerZero](https://github.com/CardputerZero) · [Card HY2](https://github.com/Moeyuuko/card-hy2) · [Pillow](https://github.com/python-pillow/Pillow) · [pyte](https://github.com/selectel/pyte)
