# Codex 本地迁移与合并

一个面向 macOS 的 Codex skill，用于把 Codex 桌面应用的本地会话与相关资源从一台 Mac 迁移到另一台 Mac，并与目标电脑现有数据合并。

它会在原电脑生成带校验信息的迁移包；在新电脑预检来源、识别项目对应关系、保留目标机已有数据，再通过备份、合并和迁后核验完成导入。新电脑无需预先安装此 skill，迁移包内含独立导入工具。

## 功能

- 导出本地会话、归档记录、agent 关系、skill、选定资源和附件索引。
- 为迁移包生成 SHA-256，并在导入前检查压缩包及文件清单。
- 对已有目标数据做备份；按保守规则合并，默认保留目标机上的冲突内容。
- 支持预检、状态核对、完整验证和有条件的回滚。
- 遇到无法确认的项目对应关系或会话分歧时，先列出供用户决定。

## 安装

下载仓库中的 `codex-local-migration.skill` 与 `codex-local-migration.skill.sha256`，放在同一目录。然后：

- macOS：打开 `install.command`。
- 其他终端：运行 `python3 install.py`。

安装器会校验 skill 包，并将其放到当前用户的 `${CODEX_HOME:-~/.codex}/skills/codex-local-migration`。遇到本机已有的不同版本时会停止，不会静默覆盖。

安装后，在 Codex 新聊天中调用：

```text
$codex-local-migration
```

## 迁移流程

1. 在原电脑调用 skill，生成迁移 ZIP、SHA-256 校验文件和随包说明。
2. 将 ZIP 与校验文件复制到新电脑。
3. 在新电脑把迁移包交给 Codex，按预检结果确认项目与冲突处理；也可以使用包内的 `导入迁移.command`。
4. 按提示保存工作、退出应用并等待正式导入及核验完成。

详细步骤见 [SKILL.md](codex-local-migration/SKILL.md)、[导出说明](codex-local-migration/references/export.md) 和 [导入说明](codex-local-migration/references/import.md)。

## 范围与数据边界

- 目标平台为 macOS 上的 Codex 桌面应用本地数据。它不迁移普通 ChatGPT 云端聊天，也不复制登录状态或云端账号数据。
- 项目工作区文件默认需要单独迁移；导出报告会列明被排除或无法找到的内容。
- 凭据容器、浏览器登录资料和配置中识别出的密钥字段会排除。为保持聊天记录原文完整，历史消息正文不会自动改写，可能仍含用户曾输入的隐私内容。
- 随包的脚本会接触本地会话数据库和文件。请先阅读源码及说明，只把自己的迁移包交给可信目标设备。
- 不兼容的数据结构会停止相应写入；导入过程会先备份目标数据，并在校验失败时尝试回滚。

## 目录

- `codex-local-migration/`：skill 源文件。
- `codex-local-migration.skill`：可安装的 skill 包。
- `install.py`、`install.command`：校验并安装 skill 的安装器。

---

此项目由个人维护，与 OpenAI 无隶属或官方背书关系。
