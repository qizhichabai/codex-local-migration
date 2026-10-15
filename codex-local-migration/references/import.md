# 新电脑导入与验收

目标需先安装并启动一次 Codex 桌面应用。所有工作区先单独恢复；缺失文件不要用空目录冒充。保留原迁移 ZIP 与外置 `.sha256`。

## 使用已安装 skill

```sh
python3 /实际skill目录/scripts/migration.py prepare --archive /实际迁移包.zip --output /本次导入报告目录
```

默认查找旁边的 `.zip.sha256`。也支持 `--sha256-file /实际校验文件`；校验不一致不解压。已人工核对并解压的包用 `--bundle /解压后的/Codex本地迁移`。

预检输出 `目标预检报告.json`、`导入预检与决策.md`、`decisions.json`。`prepare` 不写目标数据库。脚本只自动确定唯一的已登记真实路径或可跨电脑 Git 身份。不能匹配的源项目与根目录，询问用户真实新路径后填写。

```json
{
  "path_map": {"/旧工作区": "/实际新工作区"},
  "project_map": {"源项目ID": "实际目标项目ID或new"},
  "conflicts": {"thread:源会话ID": "keep_both"},
  "file_overrides": {},
  "desktop_owner_map": {}
}
```

低层数据库项目 ID 与桌面旧版项目 ID 可能不同；读真实 `projects`/`project_roots` 和桌面映射，不把界面编号直接当作数据库编号。同一路径可能登记多个项目，必须选择实际目标。

`preserve-target` 会保留已冲突的本机文件、整套技能、项目元数据和 UI 设置；同 ID 正文分歧依然待决定。逐项审阅使用 `--conflict-policy ask`，把清单中的完整键写到 `conflicts`。`file_overrides` 只能引用已审阅的合并文件，不能直接将源 `config.toml` 换用户名后覆盖。

`project_map` 中的 `new` 表示在目标登记新项目，使用真实已恢复路径，保留原目录文件。没有清楚的用户意图时先说明再取得对应关系，不因目录名称相似静默归并。

## 准备独立终端入口

决策填妥后：

```sh
python3 /实际skill目录/scripts/migration.py import --bundle /实际解压目录/Codex本地迁移 --decisions /实际决策.json --output /本次导入报告目录 --launch
```

如果从 ZIP 开始也可使用 `--archive`。只在需要用户路径输入的独立终端使用 `--interactive`，不要在无交互工具管道中挂起。

`--launch` 只准备启动文件，不代表已迁移。用 `open -a Terminal /实际报告目录/退出应用后导入.command` 打开它。用户保存工作后在终端按回车，程序请求应用退出并等待数据库释放；需要时手动完全退出相关 CLI。保持终端打开，不提前重开应用。

占用等待有明确的进程 ID 与状态，默认 30 分钟超时。已捕获的桌面后台服务只有在主应用退出、进程仍是原身份且成为孤立进程后才请求 SIGTERM；其他 CLI 不处理。不要绕过占用检测、改传假的 `--home`，或热替换活跃数据库。

## 不安装目标 skill 的入口

校验 ZIP 后解压，打开包内的 `导入迁移.command`。它通过目标机器的 Python 运行相同工具，以保留本机版本为默认策略，交互询问路径和歧义项目。按提示保存工作并退出应用。新电脑无需复制源用户名或源 Python 路径。

如果包内工具需要审阅或 Python 无法找到，将 `迁移说明.md` 交给新电脑 GPT。不要让用户为完成一个正常导入重写工具。

## 状态与结果验证

```sh
python3 /实际工具目录/migration.py status --bundle /实际包目录
python3 /实际工具目录/migration.py verify --bundle /实际包目录
```

skill 内工具位于 `scripts/`；解压包内工具位于包根。非默认数据根加 `--codex-home`。

- `prepared`：只完成准备，可能仍有待决策项。
- `waiting_for_app_exit`：尚未写入；查看 `blockers` 与进程 ID。
- `merging`：开始备份与合并，保持应用关闭。
- `completed`：正式目标通过完整验证，已写完成报告。
- `failed`：读取原因与实际目标状态。原合并器写入失败会自动回滚；后验失败也尝试回滚。不要把失败后的包状态视为成功。

`status` 只快速核对导入记录、源 ID 和记录文件存在性；不会单凭它报告完整成功。`verify` 对源全文、原有目标前缀、项目/归档/父子关系和数据库作核验。新生成的包在导入记录中保存了原有目标会话基线；旧版格式包不一定有这份持久基线，应说明该限制，依靠旧正式报告或另做核对，不能把基线数 0 当作“原有数据已全部验证”。

成功输出 `正式迁移完成报告.md`、`正式迁移完成报告.json`、`正式迁移验证报告.json`、`迁移运行状态.json`；备份位于报告目录的 `backups/`。底层原始档案位于目标 `.codex/migration-originals/`，去重记录位于 `migration-imports/`。保留这些文件。

如果应用内仍找不到会话，核对项目入口、归档和子 agent 的显示方式。数据库验证与界面可见性分开报告，不重新导入已存在的 30 份或其他既有会话。

## 回滚

完全退出应用及相关 CLI 后，使用完成报告中的真实 journal：

```sh
python3 /实际工具目录/migration.py rollback --journal /实际备份目录/rollback.json
```

迁移后文件被用户或应用改动时，默认拒绝覆盖这些新更改。先报告改动范围，取得明确决定并另做备份后，才考虑低层工具的强制选项。回滚只撤销本次迁移，不改动其他任务。
