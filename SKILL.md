---
name: lark-reaction-as-user
description: 用 Lark / 飞书 user 身份轮询最近 P2P 与群聊 @我消息，并自动选择合适 reaction。用户说「飞书自动 reaction」「lark reaction as user」「给我装反应轮询」「用我的 user token 加 reaction」「lark-reaction-as-user」时使用。这个 skill 只依赖 lark-cli，只做 reaction，不发送文字消息；支持 preference.yaml 个性化表情白名单/黑名单、冷却时间、常用表情 topN 和 launchd 常驻运行。不要用于普通 Lark API 查询、bot websocket 订阅或非 reaction 的自动发送场景。
metadata:
  type: infrastructure
---

> 不实现【第 1 项 固定 artifact】，因为本 skill 是本地工具适配层，主要产出是 reaction、heartbeat、logs 和 backtest JSON。
>
> 不实现【第 3 项 新建 vs 更新】的常规文档模式，因为本 skill 不固定写用户文档；`preference.yaml` 是可编辑配置，状态文件只记录上次扫描时间和已处理消息。

## 概念解释

- **user 身份**：用用户自己的 Lark 授权调用 API，而不是 bot；能看到 bot websocket 看不到的 P2P 聊天。
- **reaction**：飞书消息上的 emoji 反馈，例如 `OK`、`THUMBSUP`、`DONE`；比发文字低打扰。
- **上次扫描时间**：本地记录“已经扫到哪一条消息”；成功处理后更新，失败时停住，下一轮重试。

## 使用流程

### Step 0: 检查状态

```bash
./scripts/health.sh
```

检查项：`lark-cli`、user 登录、reaction dry-run、模型 CLI。

### Step 1: 先 dry-run / backtest

```bash
./scripts/lark-reaction-as-user --once --dry-run
./scripts/lark-reaction-as-user --backtest-chat oc_xxx --limit 10
```

`open_id` 默认从 `lark-cli auth status --json` 自动读取；只有失败时才传 `--user-open-id ou_xxx`。

### Step 2: 调整偏好

优先改 `preference.yaml`，不要把个人偏好硬编码进脚本。支持：

- `emoji_allowlist`
- `emoji_blacklist`
- `chat_cooldown_sec`
- `context_messages`
- `top_reactions`
- `style_hint`

### Step 3: 正式运行

```bash
./scripts/lark-reaction-as-user
```

需要常驻时安装 launchd：

```bash
./scripts/install-launchagent.sh
```

### Step 4: reaction 选择规则

- 只处理真人发来的 P2P 私聊，以及群聊里 `@我` / `@all` 的消息。
- 跳过 bot / 应用消息和普通群消息。
- 每条候选消息读截至目标消息的最近 10 条上下文，并用 `<TARGET>` 标记目标消息。
- 大模型只从允许的 `emoji_type` 中选一个。
- 默认黑名单：`SMILE,THINKING`。
- 支持从 `preference.yaml` 配置黑白名单和个人风格。
- 支持让模型优先参考目标上下文里用户常用的 reaction。
- 有 chat 级冷却机制，短时间多条消息不会每条都加 reaction。
- 模型失败时走本地规则，最终在 `OK` / `Get` 中随机兜底。

### Step 5: 任务结束后自动进化询问

完成一次调试 / 封装后，只问这一句：

```text
这次结果哪里要改？1) 内容（这个工具本身有问题）2) 流程（skill 设计有问题）3) 都还行。回数字就好。
```

回 2 时，先展示 `SKILL.md` diff，用户确认后才改，并在 `## evolution log` 追加一行。

## 关键纪律

- 只依赖 `lark-cli`。
- 不发送文字消息。
- reaction 本身也是对外可见动作，live 运行前先 dry-run。
- 不把本地 state/logs 提交进 git。

## 参考资料

> URL 按可信度分级：[官网]=主页，几乎肯定对；[搜索]=构造性搜索链接，肯定可用；[⚠️ 记忆]=模型记忆，可能过时或错误，发布前请核对。

- **Lark Open Platform**
  - 🏠 [官网](https://open.larksuite.com/) `[官网]`
- **Feishu Open Platform**
  - 🏠 [官网](https://open.feishu.cn/) `[官网]`
- **Lark message reaction API**
  - 🔍 [搜索](https://www.google.com/search?q=Lark+message+reaction+API) `[搜索]`

## evolution log

<!-- 每次基于用户反馈修改本 SKILL.md 时追加一行：YYYY-MM-DD · 改了什么 · why -->
