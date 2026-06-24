# lark-reaction-as-user

给飞书/Lark 消息自动加一个合适的 reaction。只加表情反馈，不发文字消息。

## 为什么

线上沟通没有语气和表情。大家又都很忙，只回文字会让屏幕对面的同事觉得冰冷。

这个工具想做一件小事：在合适的时候补一个自然的 reaction，减少沟通摩擦，拉近彼此距离，同时尽量少打扰。

## 它怎么工作

它只看真人发来的消息：P2P 私聊，以及群聊里 `@我` / `@all` 的消息。bot / 应用消息和普通群消息都会跳过。

对每条候选消息，它默认读取截至目标消息的最近 10 条上下文，并把目标消息标出来，让 AI 从允许的表情里挑一个最合适的 reaction。

冷却机制会控制节奏：同一个聊天里短时间连续来了多条消息，不会每条都加 reaction，只会挑一条处理，像真人一样克制。

AI 也有兜底：先尝试 Claude，两次失败再试 Codex；还不行就用本地规则，最后在 `OK` / `Get` 里保守随机选一个。

## 个性化

偏好都放在 `preference.yaml`：

- 表情白名单：AI 只能从这些 reaction 里选。
- 表情黑名单：例如 `SMILE`、`THINKING` 这类可能有文化差异的表情默认不选。
- 冷却时间：控制同一个聊天多久最多加一次 reaction。
- 上下文条数：默认看目标消息之前最近 10 条。
- 常用表情：可以让 AI 优先参考你最近常用过的 reaction top N。
- 个人风格：比如“克制、友好、低打扰”。

## 怎么用

先确保 `lark-cli` 已经完成 user 登录。

试跑一次用 `./scripts/lark-reaction-as-user --once --dry-run`，不会真的加 reaction。

前台运行用 `./scripts/lark-reaction-as-user`。macOS 常驻运行用 `./scripts/install-launchagent.sh`。健康检查用 `./scripts/health.sh`。

默认会从 `lark-cli` 自动读取你的 open_id，不需要手动配置。

## 为什么是 Python

飞书操作全部走 `lark-cli`。Python 只负责本地状态、并发拉消息、JSON 解析和组织 AI 判断上下文。用 shell 写这些会更脆。

## 状态文件

本地状态和日志默认在 `~/.local/state/lark-reaction-as-user/`，这些文件不应该提交进 git。

## License

MIT
