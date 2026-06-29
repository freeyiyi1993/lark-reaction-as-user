#!/usr/bin/env python3
"""User-visible Lark reaction poller for lark-cc-bridge.

Bot websocket events cannot see human P2P chats. This sidecar polls as the
authorized user and reacts through lark-send-as-user without starting auth.
"""
from __future__ import annotations

import json
import os
import argparse
import fcntl
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
os.chmod(STATE_DIR, 0o700)

USER_OPEN_ID = "ou_c4353252ba42d7c22523d71cef4f7d90"
CURSOR_FILE = STATE_DIR / "user_reaction_cursor"
CHAT_CURSOR_STATE_FILE = STATE_DIR / "user_reaction_chat_cursor.json"
REACTED_FILE = STATE_DIR / "user_reacted"
PENDING_REPLIES_FILE = STATE_DIR / "user_pending_replies.json"
CHAT_REACTION_STATE_FILE = STATE_DIR / "user_reaction_chat_state.json"
LOG_FILE = LOG_DIR / "user-reaction-poller.log"
LOCK_FILE = STATE_DIR / "user_reaction_poller.lock"
HEARTBEAT_FILE = STATE_DIR / "user_reaction_poller_heartbeat.json"

POLL_INTERVAL_SEC = 15
LOOKBACK_INIT_SEC = 300
P2P_SCAN_LIMIT = 5
P2P_FETCH_WORKERS = 8
LLM_REACTION_WORKERS = 4
CHAT_REACTION_COOLDOWN_SEC = 300
REACTION_CHOICES = ["OK", "THUMBSUP", "JIAYI", "DONE", "Yes", "Get", "LAUGH", "LOL", "LOVE", "HUSKY", "AWESOMEN"]
EXPLICIT_JIAYI_RE = re.compile(r"(?:\+|＋)\s*1|加\s*(?:一|1)")

RUNNING = True
LOCK_FH = None
API_FAILURES: list[dict] = []


def log(line: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a") as f:
        f.write(f"[{ts}] {line}\n")


def write_heartbeat(status: str, **fields):
    payload = {
        "pid": os.getpid(),
        "status": status,
        "updated_ts": int(time.time()),
        **fields,
    }
    HEARTBEAT_FILE.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def run_json(cmd: list[str], timeout: int = 25) -> dict | None:
    global API_FAILURES
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log(f"cmd failed rc={r.returncode} cmd={cmd[:4]} stderr={r.stderr[:240]}")
            API_FAILURES.append({
                "ts": int(time.time()),
                "cmd": " ".join(cmd[:4]),
                "rc": r.returncode,
                "stderr": r.stderr[:240],
            })
            API_FAILURES = API_FAILURES[-50:]
            return None
        return json.loads(r.stdout)
    except Exception as e:
        log(f"cmd exception cmd={cmd[:4]} err={e}")
        API_FAILURES.append({
            "ts": int(time.time()),
            "cmd": " ".join(cmd[:4]),
            "rc": -1,
            "stderr": f"{type(e).__name__}: {str(e)[:200]}",
        })
        API_FAILURES = API_FAILURES[-50:]
        return None


def read_cursor() -> int:
    try:
        return int(CURSOR_FILE.read_text().strip())
    except Exception:
        return int(time.time()) - LOOKBACK_INIT_SEC


def write_cursor(ts: int):
    CURSOR_FILE.write_text(str(ts))


def read_chat_cursors() -> dict[str, int]:
    try:
        data = json.loads(CHAT_CURSOR_STATE_FILE.read_text())
        return {str(k): int(v) for k, v in data.items()}
    except Exception:
        return {}


def write_chat_cursors(cursors: dict[str, int]):
    CHAT_CURSOR_STATE_FILE.write_text(json.dumps(cursors, sort_keys=True))


def read_reacted() -> set[str]:
    if not REACTED_FILE.exists():
        return set()
    return {ln.strip() for ln in REACTED_FILE.read_text().splitlines() if ln.strip()}


def mark_reacted(msg_id: str):
    with REACTED_FILE.open("a") as f:
        f.write(msg_id + "\n")


def read_pending_replies() -> dict[str, dict]:
    try:
        data = json.loads(PENDING_REPLIES_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_pending_replies(pending: dict[str, dict]):
    PENDING_REPLIES_FILE.write_text(json.dumps(pending, ensure_ascii=False, sort_keys=True))
    os.chmod(PENDING_REPLIES_FILE, 0o600)


def add_pending_reply(
    msg_id: str,
    body: str,
    chat_id: str,
    ts: int,
    *,
    text: str = "",
    context: list[dict] | None = None,
    at_me: bool = False,
):
    pending = read_pending_replies()
    if msg_id not in pending:
        pending[msg_id] = {
            "body": body,
            "chat_id": chat_id,
            "create_ts": ts,
            "first_pending_ts": int(time.time()),
            "attempts": 0,
            "needs_generation": not bool(body),
            "text": text,
            "context": context or [],
            "at_me": at_me,
        }
        write_pending_replies(pending)
        log(f"pending reply queued msg={msg_id} chat={chat_id}")


def read_chat_state() -> dict[str, int]:
    try:
        data = json.loads(CHAT_REACTION_STATE_FILE.read_text())
        return {str(k): int(v) for k, v in data.items()}
    except Exception:
        return {}


def write_chat_state(state: dict[str, int]):
    CHAT_REACTION_STATE_FILE.write_text(json.dumps(state, sort_keys=True))


def should_react_chat(chat_state: dict[str, int], chat_id: str, ts: int) -> bool:
    last_ts = chat_state.get(chat_id, 0)
    return ts <= 0 or last_ts <= 0 or ts - last_ts >= CHAT_REACTION_COOLDOWN_SEC


def mark_chat_reacted(chat_state: dict[str, int], chat_id: str, ts: int):
    chat_state[chat_id] = max(ts, chat_state.get(chat_id, 0))
    write_chat_state(chat_state)


def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_create_time(s: str) -> int:
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return int((dt - timedelta(hours=8)).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def extract_text(msg: dict) -> str:
    content = msg.get("content", "") or msg.get("body", {}).get("content", "")
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "text" in parsed:
            return parsed["text"]
    except Exception:
        pass
    return content or ""


def is_human_sender(sender: dict) -> bool:
    return (sender or {}).get("sender_type") == "user"


def list_recent_chat_context(chat_id: str, limit: int = 10) -> list[dict]:
    out = run_json([
        "lark-cli", "im", "+chat-messages-list",
        "--as", "user",
        "--chat-id", chat_id,
        "--page-size", str(limit),
        "--sort", "desc",
        "--format", "json",
    ], timeout=12)
    messages = ((out or {}).get("data", {}) or {}).get("messages", []) or []
    return list(reversed(messages))


def format_context(messages: list[dict], target_msg_id: str) -> str:
    rows = []
    for msg in messages[-10:]:
        sender = msg.get("sender", {}) or {}
        who = sender.get("name") or sender.get("sender_type") or sender.get("id") or "unknown"
        marker = " <TARGET>" if msg.get("message_id") == target_msg_id else ""
        text = extract_text(msg).replace("\n", " ")[:240]
        rows.append(f"- [{msg.get('create_time', '?')}] {who}{marker}: {text}")
    return "\n".join(rows)


def parse_llm_reaction(output: str) -> str | None:
    text = output.strip()
    try:
        data = json.loads(text)
        emoji = data.get("emoji_type")
    except Exception:
        m = re.search(r'"emoji_type"\s*:\s*"([^"]+)"', text)
        emoji = m.group(1) if m else text.splitlines()[0].strip().strip("`\"'") if text else None
    return emoji if emoji in REACTION_CHOICES else None


def deterministic_reaction(text: str) -> str | None:
    return "JIAYI" if EXPLICIT_JIAYI_RE.search(text or "") else None


def fallback_reaction(text: str) -> str:
    lower = (text or "").lower()
    if re.search(r"(lol|lmao|haha|哈哈|笑死|笑疯|xswl|233)", lower):
        return "LAUGH"
    if re.search(r"(done|fixed|resolved|完成|搞定|已处理|处理好了|修好了|已修|done了)", lower):
        return "DONE"
    if re.search(r"(awesome|cool|amazing|牛|太强|厉害|漂亮|赞爆)", lower):
        return "AWESOMEN"
    if re.search(r"(love|喜欢|爱了|贴贴|抱抱)", lower):
        return "LOVE"
    if re.search(r"(lgtm|sgtm|approve|赞同|支持|可以合|可以上|没问题|没毛病)", lower):
        return "THUMBSUP"
    if re.search(r"(yes|yep|是的|对的|可以|行|同意)", lower):
        return "Yes"
    if re.search(r"(got\s*it|收到|了解|明白|get了|知道了)", lower):
        return "Get"
    return "OK"


def run_claude_json(prompt: str, timeout: int, label: str, attempts: int = 2) -> subprocess.CompletedProcess:
    last = subprocess.CompletedProcess(["claude"], 1, "", "")
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log(f"claude {label} timeout attempt={attempt}")
            last = subprocess.CompletedProcess(["claude"], 124, "", "timeout")
        else:
            if result.returncode == 0:
                return result
            log(f"claude {label} failed attempt={attempt} rc={result.returncode} out={(result.stdout + result.stderr)[:240]}")
            last = result
        if attempt < attempts:
            time.sleep(2)
    return last


def choose_reaction(text: str, msg: dict, context_messages: list[dict], *, at_me: bool = False) -> str:
    target_msg_id = msg.get("message_id", "")
    fixed_emoji = deterministic_reaction(text)
    if fixed_emoji:
        log(f"deterministic reaction msg={target_msg_id} emoji={fixed_emoji}")
        return fixed_emoji

    prompt = f"""你是飞书消息 reaction 选择器。根据上下文判断这条目标消息最适合加哪个 reaction。

只能从这些合法 emoji_type 中选一个:
{", ".join(REACTION_CHOICES)}

选择原则:
- 不要默认都选 THUMBSUP；只有明确认可/赞同时才点赞。
- 普通确认/收到类可选 OK 或 Get；是/同意类可选 Yes。
- 明确要求 +1 / 加一时必须选 JIAYI；其它赞同/支持可选 THUMBSUP。
- 轻松好笑可选 LAUGH 或 LOL；明确完成可选 DONE。
- 亲近/喜欢/情绪支持可选 LOVE；轻松卖萌可选 HUSKY；强正反馈或 Cool 语义可选 AWESOMEN。
- 如果上下文不足，选 OK，不要过度热情。

是否群聊 @我: {str(at_me).lower()}

最近上下文，<TARGET> 是要加 reaction 的消息:
{format_context(context_messages, target_msg_id)}

目标消息文本:
{text[:500]}

只输出 JSON，不要解释:
{{"emoji_type":"OK"}}
"""
    try:
        r = run_claude_json(prompt, 45, f"reaction msg={target_msg_id}")
        if r.returncode == 0:
            payload = json.loads(r.stdout)
            result = payload.get("result", r.stdout)
            emoji = parse_llm_reaction(result)
            if emoji:
                log(f"llm reaction msg={target_msg_id} emoji={emoji}")
                return emoji
        emoji = fallback_reaction(text)
        log(f"llm reaction fallback msg={target_msg_id} emoji={emoji} rc={r.returncode} out={(r.stdout + r.stderr)[:240]}")
        return emoji
    except Exception as e:
        log(f"llm reaction exception msg={target_msg_id} err={type(e).__name__}: {str(e)[:160]}")
    return fallback_reaction(text)


def parse_llm_batch_reactions(output: str) -> dict[str, str]:
    text = output.strip()
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, flags=re.S)
        data = json.loads(m.group(0)) if m else {}
    rows = data.get("reactions") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    parsed = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        msg_id = str(row.get("message_id") or "")
        emoji = str(row.get("emoji_type") or "")
        if msg_id and emoji in REACTION_CHOICES:
            parsed[msg_id] = emoji
    return parsed


def choose_reactions_batch(jobs: list[tuple[dict, list[dict], bool]]) -> dict[str, str]:
    if not jobs:
        return {}
    if len(jobs) == 1:
        msg, context, at_me = jobs[0]
        return {msg.get("message_id", ""): choose_reaction(extract_text(msg), msg, context, at_me=at_me)}

    fixed = {}
    targets = []
    fallback = {}
    for msg, context, at_me in jobs:
        msg_id = msg.get("message_id", "")
        text = extract_text(msg)
        fixed_emoji = deterministic_reaction(text)
        if msg_id and fixed_emoji:
            fixed[msg_id] = fixed_emoji
            log(f"deterministic reaction batch msg={msg_id} emoji={fixed_emoji}")
            continue
        targets.append({
            "message_id": msg_id,
            "at_me": at_me,
            "context": format_context(context, msg_id),
            "target_text": text[:500],
        })
        fallback[msg_id] = fallback_reaction(text)
    if not targets:
        return fixed

    prompt = f"""你是飞书消息 reaction 批量选择器。根据每条目标消息的最近上下文，给每条消息选择一个最合适的 reaction。

只能从这些合法 emoji_type 中选:
{", ".join(REACTION_CHOICES)}

选择原则:
- 不要默认都选 THUMBSUP；只有明确认可/赞同时才点赞。
- 普通确认/收到类可选 OK 或 Get；是/同意类可选 Yes。
- 明确要求 +1 / 加一时必须选 JIAYI；其它赞同/支持可选 THUMBSUP。
- 轻松好笑可选 LAUGH 或 LOL；明确完成可选 DONE。
- 亲近/喜欢/情绪支持可选 LOVE；轻松卖萌可选 HUSKY；强正反馈或 Cool 语义可选 AWESOMEN。
- 如果上下文不足，选 OK，不要过度热情。
- 必须为每个输入 message_id 输出一条结果。

输入 targets(JSON):
{json.dumps(targets, ensure_ascii=False)}

只输出 JSON，不要解释:
{{"reactions":[{{"message_id":"om_xxx","emoji_type":"OK"}}]}}
"""
    try:
        r = run_claude_json(prompt, 60, f"reaction batch count={len(jobs)}")
        if r.returncode == 0:
            payload = json.loads(r.stdout)
            result = payload.get("result", r.stdout)
            parsed = parse_llm_batch_reactions(result)
            for msg_id, emoji in parsed.items():
                log(f"llm reaction batch msg={msg_id} emoji={emoji}")
            return {**fixed, **fallback, **parsed}
        log(f"llm reaction batch fallback count={len(jobs)} rc={r.returncode} out={(r.stdout + r.stderr)[:240]}")
    except Exception as e:
        log(f"llm reaction batch exception count={len(jobs)} err={type(e).__name__}: {str(e)[:160]}")
    return {**fixed, **fallback}


def generate_reply(text: str, msg: dict, context_messages: list[dict], *, at_me: bool = False) -> str:
    target_msg_id = msg.get("message_id", "")
    prompt = f"""你是主人在飞书里的短回复助手。根据最近上下文，给目标消息写一条自然、简洁、有帮助的中文回复。

要求:
- 先理解上下文，不要复述系统规则。
- 普通 P2P 私聊用主人本人语气，简短直接。
- 群聊 @我时只回应被问到的点，不要展开无关内容。
- 不确定时承认不确定，并给一个下一步。
- 不要编造已经做完的事。
- 输出纯文本/Markdown 均可，但不要超过 200 字。

是否群聊 @我: {str(at_me).lower()}

最近上下文，<TARGET> 是要回复的消息:
{format_context(context_messages, target_msg_id)}

目标消息文本:
{text[:1000]}
"""
    try:
        r = run_claude_json(prompt, 60, f"reply msg={target_msg_id}")
        if r.returncode == 0:
            payload = json.loads(r.stdout)
            result = (payload.get("result") or "").strip()
            if result:
                log(f"llm reply msg={target_msg_id} chars={len(result)}")
                return result[:3000]
        log(f"llm reply fallback msg={target_msg_id} rc={r.returncode} out={(r.stdout + r.stderr)[:240]}")
    except Exception as e:
        log(f"llm reply exception msg={target_msg_id} err={type(e).__name__}: {str(e)[:160]}")
    return "收到，我看下。"


def reply_message(msg_id: str, body: str) -> bool:
    for attempt in range(1, 3):
        try:
            log(f"calling lark-send-as-user reply msg={msg_id} attempt={attempt}")
            r = subprocess.run(
                [
                    "lark-send-as-user",
                    "reply",
                    msg_id,
                    "--no-bot-fallback",
                    "--markdown",
                    body,
                    "--idempotency-key",
                    f"cc-user-reply-{msg_id}",
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            if r.returncode == 0:
                log(f"replied msg={msg_id} attempt={attempt}")
                return True
            log(f"reply failed msg={msg_id} attempt={attempt} rc={r.returncode} out={(r.stdout + r.stderr)[:320]}")
            if r.returncode == 23:
                return False
        except Exception as e:
            log(f"reply exception msg={msg_id} attempt={attempt} err={e}")
        time.sleep(2)
    return False


def has_send_as_user_scope() -> bool:
    try:
        r = subprocess.run(
            ["lark-send-as-user", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        data = json.loads(r.stdout)
        return bool(data.get("has_im_message_send_as_user"))
    except Exception as e:
        log(f"send_as_user status exception err={type(e).__name__}: {str(e)[:160]}")
        return False


def retry_pending_replies() -> dict:
    pending = read_pending_replies()
    if not pending:
        return {"sent": 0, "pending": 0, "skipped": 0, "scope_ready": has_send_as_user_scope()}
    if not has_send_as_user_scope():
        return {"sent": 0, "pending": len(pending), "skipped": len(pending), "scope_ready": False}

    sent = 0
    skipped = 0
    for msg_id, item in list(pending.items()):
        body = str(item.get("body") or "")
        if not body:
            text = str(item.get("text") or "")
            context = item.get("context") if isinstance(item.get("context"), list) else []
            if not text:
                pending.pop(msg_id, None)
                continue
            body = generate_reply(text, {"message_id": msg_id}, context, at_me=bool(item.get("at_me")))
            item["body"] = body
            item["needs_generation"] = False
        if int(item.get("attempts") or 0) >= 3:
            skipped += 1
            continue
        item["attempts"] = int(item.get("attempts") or 0) + 1
        if reply_message(msg_id, body):
            pending.pop(msg_id, None)
            sent += 1
            log(f"pending reply sent msg={msg_id}")
        elif item["attempts"] >= 3:
            item["blocked_ts"] = int(time.time())
            log(f"pending reply retained after attempts msg={msg_id} attempts={item['attempts']}")
        if msg_id in pending:
            pending[msg_id] = item

    write_pending_replies(pending)
    return {"sent": sent, "pending": len(pending), "skipped": skipped, "scope_ready": True}


def pending_summary() -> dict:
    pending = read_pending_replies()
    now = int(time.time())
    rows = []
    for msg_id, item in pending.items():
        first_pending_ts = int(item.get("first_pending_ts") or now)
        rows.append({
            "message_id": msg_id,
            "chat_id": item.get("chat_id", ""),
            "attempts": int(item.get("attempts") or 0),
            "needs_generation": bool(item.get("needs_generation")),
            "first_pending_ts": first_pending_ts,
            "age_sec": max(0, now - first_pending_ts),
            "blocked_ts": item.get("blocked_ts"),
        })
    rows.sort(key=lambda row: row["first_pending_ts"])
    return {
        "count": len(rows),
        "blocked_count": sum(1 for row in rows if row["attempts"] >= 3),
        "needs_generation_count": sum(1 for row in rows if row["needs_generation"]),
        "oldest_age_sec": rows[0]["age_sec"] if rows else 0,
        "sample": rows[:5],
    }


def list_recent_p2p_chats(limit: int = P2P_SCAN_LIMIT) -> list[dict]:
    out = run_json([
        "lark-cli", "im", "+chat-list",
        "--as", "user",
        "--types", "p2p",
        "--sort", "active_time",
        "--page-size", str(limit),
        "--format", "json",
    ])
    return ((out or {}).get("data", {}) or {}).get("chats", []) or []


def list_chat_messages_since(chat_id: str, since_ts: int) -> list[dict]:
    out = run_json([
        "lark-cli", "im", "+chat-messages-list",
        "--as", "user",
        "--chat-id", chat_id,
        "--start", iso_utc(since_ts),
        "--end", iso_utc(int(time.time()) + 60),
        "--page-size", "50",
        "--sort", "asc",
        "--format", "json",
    ], timeout=10)
    return ((out or {}).get("data", {}) or {}).get("messages", []) or []


def search_at_me_since(since_ts: int) -> tuple[list[dict], bool, bool]:
    out = run_json([
        "lark-cli", "im", "+messages-search",
        "--as", "user",
        "--at-chatter-ids", USER_OPEN_ID,
        "--sender-type", "user",
        "--start", iso_utc(since_ts),
        "--end", iso_utc(int(time.time()) + 60),
        "--page-size", "50",
        "--page-all",
        "--page-limit", "40",
        "--format", "json",
    ], timeout=45)
    if out is None:
        return [], False, False
    data = (out.get("data", {}) or {})
    return data.get("messages", []) or [], True, bool(data.get("has_more"))


def search_p2p_since(since_ts: int) -> tuple[list[dict], bool, bool]:
    out = run_json([
        "lark-cli", "im", "+messages-search",
        "--as", "user",
        "--chat-type", "p2p",
        "--sender-type", "user",
        "--start", iso_utc(since_ts),
        "--end", iso_utc(int(time.time()) + 60),
        "--page-size", "50",
        "--page-all",
        "--page-limit", "40",
        "--format", "json",
    ], timeout=45)
    if out is None:
        return [], False, False
    data = (out.get("data", {}) or {})
    return data.get("messages", []) or [], True, bool(data.get("has_more"))


def react(msg_id: str, emoji: str) -> str:
    try:
        r = subprocess.run(
            ["lark-send-as-user", "reaction", msg_id, emoji],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode != 0:
            out = r.stdout + r.stderr
            if "code\": 231017" in out or "not supported for this message type" in out:
                log(f"react skipped unsupported msg={msg_id} emoji={emoji} rc={r.returncode} out={out[:240]}")
                return "skip"
            if "code\": 231001" in out or "reaction type is invalid" in out:
                log(f"react skipped invalid_emoji msg={msg_id} emoji={emoji} rc={r.returncode} out={out[:240]}")
                return "skip"
            log(f"react failed msg={msg_id} emoji={emoji} rc={r.returncode} out={out[:240]}")
            return "retry"
        return "ok"
    except Exception as e:
        log(f"react exception msg={msg_id} emoji={emoji} err={e}")
        return "retry"


def poll_once() -> dict:
    api_failure_start = len(API_FAILURES)
    write_heartbeat("retry_pending")
    pending_retry = retry_pending_replies()
    write_heartbeat("load_state")
    cursor = read_cursor()
    chat_cursors = read_chat_cursors()
    reacted = read_reacted()
    chat_state = read_chat_state()
    max_ts = cursor
    count = 0
    reply_count = 0
    suppressed = 0
    reaction_failed = False
    context_cache: dict[str, list[dict]] = {}

    chat_jobs = []
    write_heartbeat("list_recent_p2p_chats")
    for chat in list_recent_p2p_chats():
        chat_id = chat.get("chat_id", "")
        if not chat_id:
            continue
        chat_cursor = chat_cursors.get(chat_id, cursor)
        chat_jobs.append((chat, chat_cursor))

    chat_results = []
    write_heartbeat("fetch_p2p_messages", chat_count=len(chat_jobs))
    with ThreadPoolExecutor(max_workers=P2P_FETCH_WORKERS) as executor:
        futures = {
            executor.submit(list_chat_messages_since, chat.get("chat_id", ""), chat_cursor): (chat, chat_cursor)
            for chat, chat_cursor in chat_jobs
        }
        for future in as_completed(futures):
            chat, chat_cursor = futures[future]
            try:
                messages = future.result()
            except Exception as e:
                log(f"chat fetch exception chat={chat.get('chat_id')} err={e}")
                messages = []
            chat_results.append((chat, chat_cursor, messages))

    p2p_candidates = []
    chat_max_ts_by_id: dict[str, int] = {}
    chat_cursor_by_id: dict[str, int] = {}
    chat_failed_by_id: dict[str, bool] = {}
    planned_reaction_chats: set[str] = set()

    for chat, chat_cursor, messages in chat_results:
        chat_id = chat.get("chat_id", "")
        chat_max_ts = chat_cursor
        chat_cursor_by_id[chat_id] = chat_cursor
        chat_failed_by_id[chat_id] = False
        if messages and chat_id not in context_cache:
            context_cache[chat_id] = list_recent_chat_context(chat_id)
        for msg in messages:
            ts = parse_create_time(msg.get("create_time", ""))
            chat_max_ts = max(chat_max_ts, ts)
            max_ts = max(max_ts, ts)
            msg_id = msg.get("message_id", "")
            sender = msg.get("sender", {}) or {}
            if not msg_id or msg_id in reacted or not is_human_sender(sender) or sender.get("id") == USER_OPEN_ID:
                continue
            if not should_react_chat(chat_state, chat_id, ts):
                suppressed += 1
                mark_reacted(msg_id)
                reacted.add(msg_id)
                log(f"suppressed p2p cooldown chat={chat_id} msg={msg_id} ts={ts} last={chat_state.get(chat_id, 0)}")
                continue
            if chat_id in planned_reaction_chats:
                suppressed += 1
                log(f"suppressed p2p pending_chat_reaction chat={chat_id} msg={msg_id} ts={ts}")
                continue
            planned_reaction_chats.add(chat_id)
            p2p_candidates.append((chat, msg, context_cache.get(chat_id, messages)))
        chat_max_ts_by_id[chat_id] = chat_max_ts

    p2p_emojis: dict[str, str] = {}
    write_heartbeat("choose_p2p_reactions", candidate_count=len(p2p_candidates))
    p2p_emojis = choose_reactions_batch([(msg, context, False) for _chat, msg, context in p2p_candidates])

    write_heartbeat("send_p2p_reactions", candidate_count=len(p2p_candidates))
    p2p_reply_jobs = []
    for chat, msg, context in p2p_candidates:
        chat_id = chat.get("chat_id", "")
        msg_id = msg.get("message_id", "")
        ts = parse_create_time(msg.get("create_time", ""))
        emoji = p2p_emojis.get(msg_id, "OK")
        reaction_status = react(msg_id, emoji)
        if reaction_status == "ok":
            mark_reacted(msg_id)
            reacted.add(msg_id)
            mark_chat_reacted(chat_state, chat_id, ts)
            count += 1
            p2p_reply_jobs.append((msg, context, chat_id, ts))
            log(f"reacted p2p emoji={emoji} chat={chat_id} name={chat.get('name')} msg={msg_id}")
        elif reaction_status == "skip":
            mark_reacted(msg_id)
            reacted.add(msg_id)
            log(f"skipped p2p permanent_reaction_failure emoji={emoji} chat={chat_id} msg={msg_id}")
        else:
            reaction_failed = True
            chat_failed_by_id[chat_id] = True

    write_heartbeat("update_p2p_cursors")
    for chat_id, chat_max_ts in chat_max_ts_by_id.items():
        chat_cursor = chat_cursor_by_id.get(chat_id, cursor)
        if chat_max_ts > chat_cursor and not chat_failed_by_id.get(chat_id, False):
            chat_cursors[chat_id] = chat_max_ts

    write_heartbeat("search_at_me")
    at_me_messages, at_me_ok, at_me_has_more = search_at_me_since(cursor)
    write_heartbeat("process_at_me", message_count=len(at_me_messages))
    at_me_reply_jobs = []
    at_me_candidates = []
    planned_at_me_chats: set[str] = set()
    for msg in at_me_messages:
        ts = parse_create_time(msg.get("create_time", ""))
        max_ts = max(max_ts, ts)
        msg_id = msg.get("message_id", "")
        chat_id = msg.get("chat_id", "")
        sender = msg.get("sender", {}) or {}
        if not msg_id or msg_id in reacted or not is_human_sender(sender) or sender.get("id") == USER_OPEN_ID:
            continue
        if not should_react_chat(chat_state, chat_id, ts):
            suppressed += 1
            mark_reacted(msg_id)
            reacted.add(msg_id)
            log(f"suppressed at_me cooldown chat={chat_id} msg={msg_id} ts={ts} last={chat_state.get(chat_id, 0)}")
            continue
        if chat_id in planned_at_me_chats:
            suppressed += 1
            log(f"suppressed at_me pending_chat_reaction chat={chat_id} msg={msg_id} ts={ts}")
            continue
        planned_at_me_chats.add(chat_id)
        if chat_id not in context_cache:
            context_cache[chat_id] = list_recent_chat_context(chat_id)
        context = context_cache.get(chat_id, [])
        at_me_candidates.append((msg, context, chat_id, ts))

    at_me_emojis = choose_reactions_batch([(msg, context, True) for msg, context, _chat_id, _ts in at_me_candidates])
    for msg, context, chat_id, ts in at_me_candidates:
        msg_id = msg.get("message_id", "")
        emoji = at_me_emojis.get(msg_id, "OK")
        reaction_status = react(msg_id, emoji)
        if reaction_status == "ok":
            mark_reacted(msg_id)
            reacted.add(msg_id)
            mark_chat_reacted(chat_state, chat_id, ts)
            count += 1
            at_me_reply_jobs.append((msg, context, chat_id, ts))
            log(f"reacted at_me emoji={emoji} chat={msg.get('chat_id')} msg={msg_id}")
        elif reaction_status == "skip":
            mark_reacted(msg_id)
            reacted.add(msg_id)
            log(f"skipped at_me permanent_reaction_failure emoji={emoji} chat={chat_id} msg={msg_id}")
        else:
            reaction_failed = True

    write_heartbeat("search_p2p")
    p2p_search_messages, p2p_search_ok, p2p_search_has_more = search_p2p_since(cursor)
    write_heartbeat("process_p2p_search", message_count=len(p2p_search_messages))
    p2p_search_reply_jobs = []
    p2p_search_candidates = []
    planned_p2p_search_chats: set[str] = set()
    for msg in p2p_search_messages:
        ts = parse_create_time(msg.get("create_time", ""))
        max_ts = max(max_ts, ts)
        msg_id = msg.get("message_id", "")
        chat_id = msg.get("chat_id", "")
        sender = msg.get("sender", {}) or {}
        if not msg_id or msg_id in reacted or not is_human_sender(sender) or sender.get("id") == USER_OPEN_ID:
            continue
        if not should_react_chat(chat_state, chat_id, ts):
            suppressed += 1
            mark_reacted(msg_id)
            reacted.add(msg_id)
            log(f"suppressed p2p_search cooldown chat={chat_id} msg={msg_id} ts={ts} last={chat_state.get(chat_id, 0)}")
            continue
        if chat_id in planned_p2p_search_chats:
            suppressed += 1
            log(f"suppressed p2p_search pending_chat_reaction chat={chat_id} msg={msg_id} ts={ts}")
            continue
        planned_p2p_search_chats.add(chat_id)
        if chat_id not in context_cache:
            context_cache[chat_id] = list_recent_chat_context(chat_id)
        context = context_cache.get(chat_id, [])
        p2p_search_candidates.append((msg, context, chat_id, ts))

    p2p_search_emojis = choose_reactions_batch([(msg, context, False) for msg, context, _chat_id, _ts in p2p_search_candidates])
    for msg, context, chat_id, ts in p2p_search_candidates:
        msg_id = msg.get("message_id", "")
        emoji = p2p_search_emojis.get(msg_id, "OK")
        reaction_status = react(msg_id, emoji)
        if reaction_status == "ok":
            mark_reacted(msg_id)
            reacted.add(msg_id)
            mark_chat_reacted(chat_state, chat_id, ts)
            count += 1
            p2p_search_reply_jobs.append((msg, context, chat_id, ts))
            log(f"reacted p2p_search emoji={emoji} chat={chat_id} msg={msg_id}")
        elif reaction_status == "skip":
            mark_reacted(msg_id)
            reacted.add(msg_id)
            log(f"skipped p2p_search permanent_reaction_failure emoji={emoji} chat={chat_id} msg={msg_id}")
        else:
            reaction_failed = True

    all_reply_jobs = [
        *[(msg, context, chat_id, ts, False) for msg, context, chat_id, ts in p2p_reply_jobs],
        *[(msg, context, chat_id, ts, False) for msg, context, chat_id, ts in p2p_search_reply_jobs],
        *[(msg, context, chat_id, ts, True) for msg, context, chat_id, ts in at_me_reply_jobs],
    ]
    write_heartbeat("reply_all", job_count=len(all_reply_jobs))
    for msg, context, chat_id, ts, at_me_reply in all_reply_jobs:
        msg_id = msg.get("message_id", "")
        text = extract_text(msg)
        reply_body = generate_reply(text, msg, context, at_me=at_me_reply)
        if reply_message(msg_id, reply_body):
            reply_count += 1
        else:
            add_pending_reply(msg_id, reply_body, chat_id, ts, text=text, context=context, at_me=at_me_reply)

    write_heartbeat("write_state")
    write_chat_state(chat_state)
    write_chat_cursors(chat_cursors)
    if max_ts > cursor and p2p_search_ok and at_me_ok and not p2p_search_has_more and not at_me_has_more and not reaction_failed:
        write_cursor(max_ts)
    elif max_ts > cursor:
        log(
            "cursor held back "
            f"p2p_ok={p2p_search_ok} p2p_has_more={p2p_search_has_more} "
            f"at_me_ok={at_me_ok} at_me_has_more={at_me_has_more} "
            f"reaction_failed={reaction_failed} cursor={cursor} max_ts={max_ts}"
        )
    return {
        "reactions": count,
        "replies": reply_count,
        "pending_retry": pending_retry,
        "suppressed": suppressed,
        "cursor": max_ts,
        "api_failures": API_FAILURES[api_failure_start:],
    }


def backtest_chat(chat_id: str, limit: int) -> dict:
    messages = list_recent_chat_context(chat_id, limit)
    planned_ts = 0
    results = []

    for msg in messages:
        msg_id = msg.get("message_id", "")
        ts = parse_create_time(msg.get("create_time", ""))
        sender = msg.get("sender", {}) or {}
        text = extract_text(msg)
        row = {
            "message_id": msg_id,
            "create_time": msg.get("create_time", ""),
            "sender": sender.get("name") or sender.get("sender_type") or sender.get("id") or "unknown",
            "text_preview": text.replace("\n", " ")[:120],
        }

        if not msg_id:
            row.update({"would_react": False, "reason": "missing_message_id"})
        elif not is_human_sender(sender):
            row.update({"would_react": False, "reason": "non_user_sender"})
        elif sender.get("id") == USER_OPEN_ID:
            row.update({"would_react": False, "reason": "self_message"})
        elif planned_ts and ts and ts - planned_ts < CHAT_REACTION_COOLDOWN_SEC:
            row.update({"would_react": False, "reason": "cooldown"})
        else:
            emoji = choose_reaction(text, msg, messages)
            reply_body = generate_reply(text, msg, messages)
            row.update({
                "would_react": True,
                "emoji_type": emoji,
                "would_reply": True,
                "reply_preview": reply_body.replace("\n", " ")[:200],
                "reason": "eligible",
            })
            planned_ts = ts or planned_ts
        results.append(row)

    return {
        "chat_id": chat_id,
        "limit": limit,
        "cooldown_sec": CHAT_REACTION_COOLDOWN_SEC,
        "message_count": len(messages),
        "planned_reactions": sum(1 for row in results if row.get("would_react")),
        "planned_replies": sum(1 for row in results if row.get("would_reply")),
        "results": results,
    }


def stop(_signum, _frame):
    global RUNNING
    RUNNING = False


def main():
    parser = argparse.ArgumentParser(description="Poll Lark messages and add user reactions.")
    parser.add_argument("--backtest-chat", help="Run reaction selection against recent messages in a chat without sending.")
    parser.add_argument("--pending-summary", action="store_true", help="Print pending reply metadata without reply bodies.")
    parser.add_argument("--retry-pending-once", action="store_true", help="Try sending pending replies once if send_as_user scope is available.")
    parser.add_argument("--limit", type=int, default=10, help="Recent message count for --backtest-chat.")
    args = parser.parse_args()

    if args.retry_pending_once:
        print(json.dumps(retry_pending_replies(), ensure_ascii=False, indent=2))
        return

    if args.pending_summary:
        print(json.dumps(pending_summary(), ensure_ascii=False, indent=2))
        return

    if args.backtest_chat:
        print(json.dumps(backtest_chat(args.backtest_chat, args.limit), ensure_ascii=False, indent=2))
        return

    global LOCK_FH
    LOCK_FH = LOCK_FILE.open("w")
    try:
        fcntl.flock(LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"another poller holds lock; exit pid={os.getpid()}")
        return
    LOCK_FH.write(str(os.getpid()))
    LOCK_FH.flush()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"start pid={os.getpid()} interval={POLL_INTERVAL_SEC}s")
    write_heartbeat("started")
    while RUNNING:
        t0 = time.time()
        write_heartbeat("polling")
        try:
            result = poll_once()
            elapsed = time.time() - t0
            write_heartbeat("ok", elapsed_sec=round(elapsed, 3), result=result)
            if result["reactions"] or result.get("pending_retry", {}).get("sent", 0):
                log(f"poll result={result}")
        except Exception as e:
            elapsed = time.time() - t0
            write_heartbeat("error", elapsed_sec=round(elapsed, 3), error=f"{type(e).__name__}: {str(e)[:240]}")
            log(f"poll exception err={type(e).__name__}: {str(e)[:240]}")
            time.sleep(2)
            continue
        time.sleep(max(1.0, POLL_INTERVAL_SEC - elapsed))
    write_heartbeat("stopped")
    log(f"stopped pid={os.getpid()}")


if __name__ == "__main__":
    main()
