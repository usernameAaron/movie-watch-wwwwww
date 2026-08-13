# -*- coding: utf-8 -*-
"""Minimal GitHub Actions watcher for MOViE MOViE / 奥德赛.

Based on the data-source approach in perbright/movie-movie (GPL-3.0).
This modified version adds persistent incremental state, strict source-halting,
and Feishu application-bot notifications suitable for GitHub Actions.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests


TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_STATE = {
    "schema_version": 1,
    "initialized": False,
    "current_fingerprints": [],
    "ever_seen_fingerprints": {},
    "successfully_notified_fingerprints": [],
    "disappeared_fingerprints": [],
    "pending_notifications": [],
    "notification_attempts": {},
    "notification_events": {},
    "last_successful_check": None,
    "last_notification": None,
    "source_halted": False,
    "source_halt_reason": None,
    "source_halted_at": None,
    "halt_notification_attempted": False,
    "feishu_token_api_calls": 0,
    "feishu_message_api_calls": 0,
}


class TransientSourceError(RuntimeError):
    """A source error that may be retried by the next scheduled workflow."""


class SourceRiskError(RuntimeError):
    """A source condition that must persistently halt future access."""


class NotificationError(RuntimeError):
    """A sanitized Feishu failure."""


@dataclass(frozen=True)
class FetchResult:
    cinema_name: str
    sessions: list[dict[str, str]]


def now_shanghai() -> datetime:
    return datetime.now(TZ)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(DEFAULT_STATE)
    value = load_json(path)
    state = copy.deepcopy(DEFAULT_STATE)
    state.update(value)
    return state


def atomic_save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def stable_fingerprint(cinema_id: str, movie_name: str, session: dict[str, Any]) -> str:
    seq = str(session.get("seqNo") or session.get("seq") or "").strip()
    if seq:
        return f"seq:{seq}"
    fields = [
        cinema_id,
        movie_name,
        str(session.get("date", "")),
        str(session.get("time", "")),
        str(session.get("hall", "")),
        str(session.get("language", "")),
        str(session.get("format", "")),
    ]
    return "sha256:" + hashlib.sha256("+".join(fields).encode("utf-8")).hexdigest()


def _session_is_future(date_text: str, time_text: str, current: datetime) -> bool:
    try:
        show_date = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        return False
    if show_date < current.date():
        return False
    if show_date > current.date():
        return True
    try:
        show_time = datetime.strptime(time_text, "%H:%M").time()
    except ValueError:
        return True
    return datetime.combine(show_date, show_time, tzinfo=TZ) >= current


def extract_future_sessions(
    data: dict[str, Any], config: dict[str, Any], current: datetime
) -> FetchResult:
    cinema_id = str(config["cinema_id"])
    expected = str(config.get("cinema_name_must_contain", "MOViE MOViE"))
    cinema_data = data.get("cinemaData")
    cinema_name = str(cinema_data.get("nm", "")).strip() if isinstance(cinema_data, dict) else ""
    if not cinema_name or expected not in cinema_name:
        raise SourceRiskError("影院身份不一致")

    show_data = data.get("showData")
    if not isinstance(show_data, dict) or not isinstance(show_data.get("movies"), list):
        raise SourceRiskError("响应缺少正常排片字段")

    allowed_names = {str(config["movie_name"]).strip()}
    allowed_names.update(str(x).strip() for x in config.get("movie_aliases", []) if str(x).strip())
    selected = [m for m in show_data["movies"] if isinstance(m, dict) and str(m.get("nm", "")).strip() in allowed_names]
    sessions: list[dict[str, str]] = []
    for movie in selected:
        movie_name = str(movie.get("nm", "")).strip()
        shows = movie.get("shows")
        if not isinstance(shows, list):
            raise SourceRiskError("目标影片缺少正常排片字段")
        for show in shows:
            if not isinstance(show, dict):
                raise SourceRiskError("排片日期字段异常")
            date_text = str(show.get("showDate", "")).strip()
            plist = show.get("plist")
            if not isinstance(plist, list):
                raise SourceRiskError("场次列表字段异常")
            for item in plist:
                if not isinstance(item, dict):
                    raise SourceRiskError("场次字段异常")
                time_text = str(item.get("tm", "")).strip()
                if not _session_is_future(date_text, time_text, current):
                    continue
                session = {
                    "cinema_id": cinema_id,
                    "cinema_name": cinema_name,
                    "movie_name": movie_name,
                    "date": date_text,
                    "time": time_text,
                    "hall": str(item.get("th", "")).strip(),
                    "language": str(item.get("lang", "")).strip(),
                    "format": str(item.get("tp", "")).strip(),
                    "seq": str(item.get("seqNo", "")).strip(),
                }
                session["fingerprint"] = stable_fingerprint(cinema_id, movie_name, session)
                sessions.append(session)
    unique = {item["fingerprint"]: item for item in sessions}
    return FetchResult(cinema_name, sorted(unique.values(), key=lambda x: (x["date"], x["time"], x["fingerprint"])))


def fetch_source(config: dict[str, Any], current: datetime) -> FetchResult:
    """Make exactly one source request. This function never retries."""
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://m.maoyan.com/cinema/{config['cinema_id']}",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        response = requests.get(
            str(config["api_url"]),
            params={"cinemaId": str(config["cinema_id"]), "movieId": 0},
            headers=headers,
            timeout=float(config.get("timeout_seconds", 15)),
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise TransientSourceError("猫眼网络连接或超时失败") from exc
    except requests.RequestException as exc:
        raise TransientSourceError("猫眼请求失败") from exc

    if response.status_code in {403, 418, 429}:
        raise SourceRiskError(f"HTTP {response.status_code} 风控响应")
    if response.status_code >= 400:
        raise TransientSourceError(f"猫眼 HTTP {response.status_code}")
    text = response.text.lstrip().lower()
    if any(mark in text[:2000] for mark in ("captcha", "滑块", "验证码", "verify")):
        raise SourceRiskError("检测到 CAPTCHA 或验证页面")
    try:
        data = response.json()
    except ValueError as exc:
        raise SourceRiskError("返回非 JSON 验证页面") from exc
    if not isinstance(data, dict):
        raise SourceRiskError("返回 JSON 结构异常")
    return extract_future_sessions(data, config, current)


def _event_id(items: list[dict[str, str]]) -> str:
    material = "\n".join(sorted(f"{item['kind']}|{item['fingerprint']}" for item in items))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def build_schedule_message(cinema_name: str, movie_name: str, items: list[dict[str, str]], current: datetime) -> str:
    lines = [
        "【MOViE MOViE《奥德赛》排片提醒】",
        f"影院名称：{cinema_name}",
        f"影片名称：{movie_name}",
    ]
    for date_text in sorted({item["date"] for item in items}):
        lines.append(f"\n观影日期：{date_text}")
        for item in sorted((x for x in items if x["date"] == date_text), key=lambda x: (x["time"], x["fingerprint"])):
            label = "恢复上架" if item["kind"] == "restored" else "新增场次"
            details = " / ".join(x for x in (item["hall"], item["language"], item["format"]) if x) or "详情以购票平台为准"
            lines.append(f"- {label}：{item['time']}｜{details}")
    lines.extend([
        f"\n检测时间：{current.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "发现新排片，可能已开放购票，请立即人工打开购票平台确认。",
    ])
    return "\n".join(lines)


def send_feishu(message: str, stats: dict[str, int] | None = None) -> None:
    """Send one application-bot P2P text message; never print credentials."""
    stats = stats if stats is not None else {"token": 0, "message": 0}
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    receive_id = os.environ.get("FEISHU_RECEIVE_ID", "").strip()
    if not (app_id and app_secret and receive_id):
        raise NotificationError("飞书 GitHub Secrets 未完整配置")
    try:
        stats["token"] += 1
        token_response = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=15,
        )
        token_data = token_response.json()
        if token_response.status_code != 200 or token_data.get("code") != 0:
            raise NotificationError("飞书鉴权明确失败")
        token = str(token_data.get("tenant_access_token", "")).strip()
        if not token:
            raise NotificationError("飞书鉴权响应缺少令牌")
        stats["message"] += 1
        message_response = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": message}, ensure_ascii=False)},
            timeout=15,
        )
        message_data = message_response.json()
        message_id = str(((message_data.get("data") or {}).get("message_id") or "")).strip()
        if message_response.status_code != 200 or message_data.get("code") != 0 or not message_id:
            raise NotificationError("飞书消息接口明确失败")
    except NotificationError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise NotificationError("飞书请求失败或返回格式异常") from exc


def _save_if_changed(path: Path, before: dict[str, Any], after: dict[str, Any], dry_run: bool) -> bool:
    if dry_run or before == after:
        return False
    atomic_save_json(path, after)
    return True


def _mark_halted(
    state: dict[str, Any], reason: str, current: datetime, notifier: Callable[..., None]
) -> None:
    state["source_halted"] = True
    state["source_halt_reason"] = reason
    state["source_halted_at"] = current.isoformat(timespec="seconds")
    if state.get("halt_notification_attempted"):
        return
    state["halt_notification_attempted"] = True
    stats = {"token": 0, "message": 0}
    try:
        notifier(
            "【电影监控来源已停止，需要人工处理】\n"
            f"停止原因：{reason}\n检测时间：{current.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            "程序不会继续访问数据源；请人工检查后通过 clear_source_halt=true 清除停止状态。",
            stats,
        )
        state["halt_notification_result"] = "success"
    except NotificationError as exc:
        state["halt_notification_result"] = "failed"
        state["halt_notification_error"] = str(exc)
    finally:
        state["feishu_token_api_calls"] += stats["token"]
        state["feishu_message_api_calls"] += stats["message"]


def _queue_changes(state: dict[str, Any], result: FetchResult) -> tuple[list[dict[str, str]], bool]:
    current_map = {item["fingerprint"]: item for item in result.sessions}
    previous = set(state.get("current_fingerprints") or [])
    current_set = set(current_map)
    ever = dict(state.get("ever_seen_fingerprints") or {})
    disappeared = set(state.get("disappeared_fingerprints") or [])
    queued = {
        item["fingerprint"]
        for event in state.get("pending_notifications") or []
        for item in event.get("items") or []
    }

    for fingerprint in previous - current_set:
        disappeared.add(fingerprint)

    changes: list[dict[str, str]] = []
    for fingerprint in sorted(current_set - previous):
        if fingerprint in queued:
            continue
        item = dict(current_map[fingerprint])
        item["kind"] = "restored" if fingerprint in ever and fingerprint in disappeared else "new"
        changes.append(item)
        disappeared.discard(fingerprint)

    for fingerprint, item in current_map.items():
        ever.setdefault(fingerprint, item)
    material_changed = (
        not state.get("initialized")
        or previous != current_set
        or ever != state.get("ever_seen_fingerprints")
        or disappeared != set(state.get("disappeared_fingerprints") or [])
    )
    state["initialized"] = True
    state["current_fingerprints"] = sorted(current_set)
    state["ever_seen_fingerprints"] = ever
    state["disappeared_fingerprints"] = sorted(disappeared)
    if changes:
        event_id = _event_id(changes)
        state["pending_notifications"].append({"event_id": event_id, "items": changes})
        state["notification_attempts"].setdefault(event_id, 0)
        state["notification_events"].setdefault(event_id, {"status": "pending", "last_error": None})
        material_changed = True
    return changes, material_changed


def _attempt_pending(
    state: dict[str, Any], config: dict[str, Any], cinema_name: str, current: datetime,
    notifier: Callable[..., None], dry_run: bool,
) -> tuple[str, bool]:
    pending = state.get("pending_notifications") or []
    if not pending:
        return "none", False
    event = pending[0]
    event_id = event["event_id"]
    attempts = int(state["notification_attempts"].get(event_id, 0))
    message = build_schedule_message(cinema_name, str(config["movie_name"]), event["items"], current)
    if dry_run:
        print("[DRY-RUN] Would send one merged Feishu message:\n" + message)
        return "dry-run", False
    if attempts >= 3:
        # The third failed attempt already persisted the exhausted status.
        # Later schedules must fail visibly without another send or state commit.
        return "exhausted", False

    state["notification_attempts"][event_id] = attempts + 1
    stats = {"token": 0, "message": 0}
    try:
        notifier(message, stats)
    except NotificationError as exc:
        state["notification_events"][event_id] = {"status": "failed", "last_error": str(exc)}
        if attempts + 1 >= 3:
            state["notification_events"][event_id]["status"] = "exhausted"
        return "failed", True
    finally:
        state["feishu_token_api_calls"] += stats["token"]
        state["feishu_message_api_calls"] += stats["message"]

    notified = set(state.get("successfully_notified_fingerprints") or [])
    notified.update(item["fingerprint"] for item in event["items"])
    state["successfully_notified_fingerprints"] = sorted(notified)
    state["last_notification"] = current.isoformat(timespec="seconds")
    state["notification_events"][event_id] = {"status": "success", "last_error": None}
    state["pending_notifications"] = pending[1:]
    return "success", True


def print_schedule(result: FetchResult) -> None:
    print(f"影院名称：{result.cinema_name}")
    if not result.sessions:
        print("《奥德赛》当前没有未来排片。")
        return
    print("《奥德赛》全部未来排片：")
    for item in result.sessions:
        details = " / ".join(x for x in (item["hall"], item["language"], item["format"]) if x) or "详情缺省"
        print(f"- {item['date']} {item['time']}｜{details}")


def run_monitor(
    config_path: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
    clear_source_halt: bool = False,
    current: datetime | None = None,
    fetcher: Callable[[dict[str, Any], datetime], FetchResult] = fetch_source,
    notifier: Callable[..., None] = send_feishu,
) -> int:
    config = load_json(config_path)
    before = load_state(state_path)
    state = copy.deepcopy(before)
    current = (current or now_shanghai()).astimezone(TZ)

    if clear_source_halt:
        if dry_run:
            print("错误：dry-run 不得修改正式停止状态；请将 dry_run=false 后重试。")
            return 1
        state.update({
            "source_halted": False,
            "source_halt_reason": None,
            "source_halted_at": None,
            "halt_notification_attempted": False,
        })
        state.pop("halt_notification_result", None)
        state.pop("halt_notification_error", None)
        _save_if_changed(state_path, before, state, False)
        print("来源停止状态已由人工明确清除；本次不访问数据源。")
        return 0

    if state.get("source_halted"):
        if not dry_run and not state.get("halt_notification_attempted"):
            _mark_halted(
                state,
                str(state.get("source_halt_reason") or "原因未记录"),
                current,
                notifier,
            )
            _save_if_changed(state_path, before, state, False)
        print(f"电影监控来源已停止：{state.get('source_halt_reason') or '原因未记录'}；本次未访问数据源。")
        return 2

    try:
        result = fetcher(config, current)
    except SourceRiskError as exc:
        reason = str(exc)
        if dry_run:
            print(f"[DRY-RUN] 检测到必须停源的条件：{reason}；未修改正式状态。")
            return 2
        state["last_successful_check"] = None
        state["source_halted"] = True
        state["source_halt_reason"] = reason
        state["source_halted_at"] = current.isoformat(timespec="seconds")
        atomic_save_json(state_path, state)
        _mark_halted(state, reason, current, notifier)
        atomic_save_json(state_path, state)
        print(f"检测到必须停源的条件：{reason}；已持久化停止且不会自动重试。")
        return 2
    except TransientSourceError as exc:
        print(f"临时查询失败：{exc}；等待下一个 GitHub Actions 周期。")
        return 1

    print_schedule(result)
    changes, material_changed = _queue_changes(state, result)
    outcome, notification_changed = _attempt_pending(
        state, config, result.cinema_name, current, notifier, dry_run
    )
    if material_changed or notification_changed:
        state["last_successful_check"] = current.isoformat(timespec="seconds")
    saved = _save_if_changed(state_path, before, state, dry_run)
    print(f"本轮未来场次：{len(result.sessions)}；新增/恢复：{len(changes)}；提醒结果：{outcome}；状态写入：{'是' if saved else '否'}")
    if outcome in {"failed", "exhausted"}:
        return 1
    return 0


def send_test_notification(notifier: Callable[..., None] = send_feishu) -> int:
    stats = {"token": 0, "message": 0}
    try:
        notifier(
            "【电影监控测试】\nMOViE MOViE《奥德赛》GitHub Actions 飞书提醒通道测试。\n"
            "本消息仅用于连通性验证，不代表当前存在新增排片。",
            stats,
        )
    except NotificationError as exc:
        print(f"电影监控测试消息发送失败：{exc}")
        return 1
    print(f"电影监控测试消息发送成功；Token接口调用次数={stats['token']}；消息接口调用次数={stats['message']}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MOViE MOViE《奥德赛》GitHub Actions 排片监控")
    base = Path(__file__).resolve().parent
    parser.add_argument("--config", type=Path, default=base / "config.json")
    parser.add_argument("--state", type=Path, default=base / "state" / "movie_watch_state.json")
    parser.add_argument("--dry-run", action="store_true", help="查询并显示，但不发飞书、不改正式状态")
    parser.add_argument("--clear-source-halt", action="store_true", help="人工清除持久化停源状态；本次不访问数据源")
    parser.add_argument("--test-notification", action="store_true", help="仅发送一条带测试标识的飞书消息")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.test_notification:
        if args.dry_run or args.clear_source_halt:
            print("错误：测试消息不能与 dry-run 或 clear-source-halt 同时使用。")
            return 1
        return send_test_notification()
    return run_monitor(
        args.config.resolve(), args.state.resolve(),
        dry_run=args.dry_run, clear_source_halt=args.clear_source_halt,
    )


if __name__ == "__main__":
    raise SystemExit(main())
