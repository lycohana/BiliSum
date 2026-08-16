# 给 AI / Agent：安装 BiliSum 视频理解 Skill

本页用于让 AI / Agent 安装仓库内的 `bilisum-video-understanding` skill。安装后可以通过 BiliSum CLI 总结 B 站、YouTube 和本地视频，也可以读取转写、章节和已有任务状态。

## 推荐安装方式

在需要使用 skill 的项目目录中执行：

```bash
npx skills add https://github.com/lycohana/BiliSum --skill bilisum-video-understanding
```

安装完成后，确认目标 skills 目录中存在：

```text
bilisum-video-understanding/
├── SKILL.md
└── agents/
    └── openai.yaml
```

如果当前 Agent 不会立即重新扫描 skills，请新建任务或重启对应的 Agent 客户端。

## 使用 BiliSum CLI 安装

先确认 CLI 可用：

```bash
bilisum --version
```

如果命令不存在，可以安装 npm 包：

```bash
npm install -g bilisum
```

然后根据目标位置选择一种安装方式：

```bash
bilisum skill install --project   # 当前项目的 .agents/skills/
bilisum skill install --global    # $CODEX_HOME/skills/ 或 ~/.codex/skills/
bilisum skill install --path ./agent-skills/bilisum-video-understanding
```

在可交互终端中也可以直接运行 `bilisum skill install`，再选择安装位置。AI、CI 或其他非交互环境必须显式传入 `--project`、`--global` 或 `--path`；不带目标参数时只会显示候选路径，不会写入文件。

只在用户明确需要全局安装时使用 `--global`，其余情况优先安装到当前项目。

## 验证

查看 npm 包内置的 skill 路径：

```bash
bilisum skill path
```

安装后可以让 Agent 处理一个视频，也可以直接验证 CLI：

```bash
bilisum doctor
bilisum brief "https://www.bilibili.com/video/BV1xxxx" --format json --quiet
```

如果没有安装桌面端，首次使用前需要初始化 CLI 独立环境：

```bash
bilisum env setup
bilisum --setting
```

`bilisum --setting` 会打开设置页。API Key 应在设置页中填写，不要要求用户把密钥发送到对话中。

## 更新 Skill

目标目录已存在时，安装器默认不会覆盖。确认需要更新后执行：

```bash
bilisum skill install --project --force
```

`--force` 会更新由 BiliSum 管理的 `SKILL.md` 和 `agents/openai.yaml`，不会删除目录中的其他文件。全局或自定义安装同样可以追加 `--force`。

## Skill 的命令选择

| 用户请求 | CLI 命令 |
|----------|----------|
| 完整理解、知识笔记或后续问答 | `bilisum summarize <source> --format json --quiet` |
| 简短概览和要点 | `bilisum brief <source> --format json --quiet` |
| 完整转写 | `bilisum transcribe <source> --format json --quiet` |
| 查询未完成任务 | `bilisum status <task-id> --json` |
| 查看近期任务 | `bilisum tasks --json` |

具体的输出字段、异常处理和多 P 视频规则以安装后的 `SKILL.md` 为准。CLI 的完整说明见 [CLI 使用指南](../cli.md)。
