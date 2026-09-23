#!/usr/bin/env python3
"""
WorkBuddy 每日签到脚本（多账号版）
- 本地运行：自动扫描 WorkBuddy 客户端 auth 目录下所有 *.info 登录态文件，
  按 uid 去重，每个账号取最新的 token，逐一签到（无需来回切换登录）
- 云端运行：通过环境变量 WORKBUDDY_ACCOUNTS（JSON 数组）或
  WORKBUDDY_ACCESS_TOKEN（单账号）注入凭证
纯 Python 标准库，零第三方依赖。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_BASE = "https://www.codebuddy.cn"
CHECKIN_STATUS_URL = f"{API_BASE}/v2/billing/meter/checkin-status"
DAILY_CHECKIN_URL = f"{API_BASE}/v2/billing/meter/daily-checkin"
REQUEST_TIMEOUT = 20

# 网络抖动保护：接口偶发不可达时，单次请求重试若干次。
# 只对「网络类」错误重试 —— 401 是确定性的，重试没有意义。
REQUEST_RETRIES = 3
RETRY_BACKOFF_SECONDS = (2, 5)

# 若所有账号都因「网络不可达」失败（说明不是凭证问题，而是链路/接口临时故障），
# 整体等待后重试一轮。2026-09-23 的故障就是这样：runner 侧到接口
# 连续超时约 40 分钟，5 个账号在 100 秒内全部撞上，而本机同时刻完全正常。
ALL_NETWORK_RETRY_DELAY = 90

REASON_NETWORK = "network"
REASON_AUTH = "auth"
REASON_OTHER = "other"

_AUTH_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "CodeBuddyExtension" / "Data/Public/auth"
_MAC_AUTH_DIR = Path.home() / "Library/Application Support/CodeBuddyExtension" / "Data/Public/auth"
_CONFIG_FILE = Path(__file__).resolve().parent / "config.json"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)


def in_actions() -> bool:
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("GITHUB_STEP_SUMMARY"))


def gha_cmd(kind: str, msg: str) -> None:
    """输出 GitHub Actions annotation。

    注意：workflow command 必须是**整行的开头**，不能带时间戳前缀，
    所以这里不能走 log()，否则 GitHub 识别不到。
    """
    if not in_actions():
        return
    # annotation 不支持换行，压成单行
    flat = " ".join(str(msg).split())
    try:
        print(f"::{kind}::{flat}", flush=True)
    except Exception:
        pass


def _write_step_summary(results: list[tuple[str, bool, str]], ok: list[str], bad: list[str]) -> None:
    """把逐账号结果写进 GitHub Actions 的 Job Summary，并同时打进 run 日志。

    部分失败时 job 仍然是绿的，所以这是唯一稳定可见的失败信号，不能省。
    之所以还要打到 stdout：Job Summary 没有公开的 REST 接口可读取，
    写进日志才能在事后用 API 复核（也方便直接翻日志排查）。
    """
    reason_txt = {REASON_NETWORK: "🌐 网络不可达", REASON_AUTH: "🔑 凭证失效", REASON_OTHER: "❓ 其他"}
    bad_kinds = {r[2] for r in results if not r[1]}

    lines = [
        "## WorkBuddy 每日签到",
        "",
        f"- 运行时间（runner 本地）：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
        f"- 结果：**成功 {len(ok)}/{len(results)}**",
        "",
        "| 账号 | 结果 | 失败原因 |",
        "| --- | --- | --- |",
    ]
    for name, okflag, reason in results:
        mark = "✅ 成功" if okflag else "❌ 失败"
        lines.append(f"| {name} | {mark} | {reason_txt.get(reason, '') if not okflag else '-'} |")
    lines.append("")
    if bad:
        if bad_kinds == {REASON_NETWORK}:
            lines.append(
                f"> ⚠️ 失败账号：**{', '.join(bad)}** —— 全部是**网络不可达**。"
                "这**不是**凭证问题：runner 到 `www.codebuddy.cn` 的链路或接口临时故障。"
                "无需刷新 token；若持续出现，说明该链路已长期不可用。"
            )
        elif REASON_AUTH in bad_kinds:
            lines.append(
                f"> ⚠️ 失败账号：**{', '.join(bad)}** —— 含**凭证失效**（服务端 401）。"
                "需重新登录对应账号并刷新 `WORKBUDDY_ACCOUNTS`。"
            )
        else:
            lines.append(f"> ⚠️ 失败账号：**{', '.join(bad)}**，详见上方原因列。")
    else:
        lines.append("> ✅ 全部账号签到成功。")
    block = "\n".join(lines)

    if in_actions():
        try:
            print(block, flush=True)
        except Exception:
            pass

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(block + "\n")
    except Exception as e:
        log(f"[!] 写 Job Summary 失败: {e}")


# ---------------------------------------------------------------- 凭证加载

def _parse_auth_data(data: dict, source: str) -> dict | None:
    """从 auth 文件 JSON 结构中提取凭证。"""
    token = (data.get("auth", {}).get("accessToken") or "").strip()
    if not token:
        return None
    account = data.get("account") or {}
    return {
        "access_token": token,
        "account_name": account.get("nickname") or "未知账号",
        "uid": (account.get("uid") or "").strip() or None,
        "domain": (data.get("auth", {}).get("domain") or "www.codebuddy.cn").strip(),
        "enterprise_id": (account.get("enterpriseId") or account.get("enterprise_id") or "").strip() or None,
        "_source": source,
    }


def _creds_from_env() -> list[dict]:
    """CI 环境：优先 WORKBUDDY_ACCOUNTS（JSON 数组，多账号），兼容单 token。"""
    accounts_raw = os.environ.get("WORKBUDDY_ACCOUNTS", "").strip()
    if accounts_raw:
        try:
            arr = json.loads(accounts_raw)
            out = []
            for item in arr:
                token = (item.get("access_token") or "").strip()
                if not token:
                    continue
                out.append({
                    "access_token": token,
                    "account_name": item.get("account_name") or item.get("uid") or "账号",
                    "uid": (item.get("uid") or "").strip() or None,
                    "domain": (item.get("domain") or "www.codebuddy.cn").strip(),
                    "enterprise_id": (item.get("enterprise_id") or "").strip() or None,
                    "_source": "env:WORKBUDDY_ACCOUNTS",
                })
            return out
        except Exception as e:
            log(f"[!] WORKBUDDY_ACCOUNTS 解析失败: {e}")

    token = os.environ.get("WORKBUDDY_ACCESS_TOKEN", "").strip()
    if not token:
        return []
    return [{
        "access_token": token,
        "account_name": os.environ.get("WORKBUDDY_ACCOUNT_NAME", "环境变量账号"),
        "uid": os.environ.get("WORKBUDDY_UID", "").strip() or None,
        "domain": os.environ.get("WORKBUDDY_DOMAIN", "www.codebuddy.cn").strip(),
        "enterprise_id": os.environ.get("WORKBUDDY_ENTERPRISE_ID", "").strip() or None,
        "_source": "env",
    }]


def _creds_from_config() -> list[dict]:
    """本地 config.json：支持 {"accounts": [...]} 多账号，也兼容旧单账号格式。"""
    if not _CONFIG_FILE.exists():
        return []
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[!] 读取 config.json 失败: {e}")
        return []

    if isinstance(data.get("accounts"), list):
        out = []
        for item in data["accounts"]:
            token = (item.get("access_token") or "").strip()
            if not token:
                continue
            out.append({
                "access_token": token,
                "account_name": item.get("account_name") or item.get("uid") or "账号",
                "uid": (item.get("uid") or "").strip() or None,
                "domain": (item.get("domain") or "www.codebuddy.cn").strip(),
                "enterprise_id": (item.get("enterprise_id") or "").strip() or None,
                "_source": "config.json",
            })
        return out

    # 兼容旧单账号格式
    token = (data.get("access_token") or "").strip()
    if not token:
        return []
    return [{
        "access_token": token,
        "account_name": data.get("account_name") or "配置账号",
        "uid": (data.get("uid") or "").strip() or None,
        "domain": (data.get("domain") or "www.codebuddy.cn").strip(),
        "enterprise_id": (data.get("enterprise_id") or "").strip() or None,
        "_source": "config.json",
    }]


def _creds_from_local_auth_files() -> list[dict]:
    """扫描本地 auth 目录所有 *.info 文件，按 uid 去重。

    同一账号可能存在多个历史 token（有效期各异、有的已 401），
    因此每个账号保留全部候选 token，按文件时间从新到旧排序，
    签到时逐个尝试直到命中有效 token。
    """
    accounts: dict[str, list[dict]] = {}
    for auth_dir in (_AUTH_DIR, _MAC_AUTH_DIR):
        if not auth_dir.is_dir():
            continue
        for f in sorted(auth_dir.glob("*.info"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            creds = _parse_auth_data(data, f.name)
            if not creds:
                continue
            key = creds["uid"] or creds["access_token"]
            accounts.setdefault(key, []).append(creds)

    result = []
    for key, candidates in accounts.items():
        # account 的显示名取第一个非空的名字
        name = next((c["account_name"] for c in candidates if c["account_name"] != "未知账号"),
                    candidates[0]["account_name"])
        result.append({"account_name": name, "candidates": candidates})
    return result


def _normalize(flat: list[dict]) -> list[dict]:
    """把单凭证列表转成 {account_name, candidates} 结构，与 auth 目录扫描结果统一。"""
    return [{"account_name": c["account_name"], "candidates": [c]} for c in flat]


def load_all_credentials() -> list[dict]:
    # CI 优先环境变量，其次 config.json，最后扫描本地 auth 目录
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
        creds = _creds_from_env()
        if creds:
            return _normalize(creds)
        creds = _creds_from_config()
        if creds:
            return _normalize(creds)
    else:
        creds = _creds_from_config()
        if creds:
            return _normalize(creds)
        creds = _creds_from_env()
        if creds:
            return _normalize(creds)
    return _creds_from_local_auth_files()


# ---------------------------------------------------------------- 请求

def _build_headers(creds: dict) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {creds['access_token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy-Checkin/1.2",
    }
    if creds.get("uid"):
        headers["X-User-Id"] = creds["uid"]
    if creds.get("domain"):
        headers["X-Domain"] = creds["domain"]
    eid = creds.get("enterprise_id")
    if eid:
        headers["X-Enterprise-Id"] = eid
        headers["X-Tenant-Id"] = eid
    return headers


def _request_once(url: str, creds: dict, method: str = "POST") -> tuple[dict | None, str | None]:
    """发一次请求，返回 (payload, err)。

    err 的含义（关键：必须区分，否则无法判断是凭证坏了还是网络断了）：
      None        —— 拿到了响应（可能是业务错误码，但链路是通的）
      "network"   —— 超时 / 连接失败 / DNS 失败，属于可重试的瞬时故障
      "http:<code>" —— 服务端返回了非 2xx，401/403 意味着 token 确定性失效
    """
    req = urllib.request.Request(
        url,
        data=b"{}",
        method=method.upper(),
        headers=_build_headers(creds),
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return (json.loads(raw) if raw else {}), None
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            return (json.loads(err_body) if err_body else None), f"http:{e.code}"
        except Exception:
            return None, f"http:{e.code}"
    except Exception as e:
        return None, "network"


def _request_json(url: str, creds: dict, method: str = "POST") -> tuple[dict | None, str | None]:
    """带重试的请求：仅对网络类错误重试，HTTP 层错误立即返回。"""
    last_err: str | None = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        payload, err = _request_once(url, creds, method)
        if err != REASON_NETWORK:
            return payload, err
        last_err = err
        if attempt < REQUEST_RETRIES:
            wait = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            log(f"    网络异常（第 {attempt}/{REQUEST_RETRIES} 次尝试），{wait}s 后重试...")
            time.sleep(wait)
    return None, last_err


def _msg_of(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("message") or payload.get("msg") or "")


def already_checked_in(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    msg = _msg_of(payload)
    if code == 10001 or "已签到" in msg or "已经签到" in msg:
        return True
    data = payload.get("data")
    if isinstance(data, dict) and (data.get("today_checked_in") or data.get("checked_in")):
        return True
    return bool(payload.get("today_checked_in") or payload.get("checked_in"))


def unwrap_data(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code is not None and code not in (0, 200):
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


# ---------------------------------------------------------------- 单账号签到流程

def checkin_one(account: dict) -> tuple[bool, str]:
    """签到单个账号，返回 (是否成功, 失败原因)。

    失败原因取值：""（成功）| REASON_NETWORK | REASON_AUTH | REASON_OTHER。
    区分「网络不可达」与「token 失效」至关重要：前者是环境问题，不需要换 token。
    """
    name = account["account_name"]
    candidates = account["candidates"]
    log(f"--- 账号: {name} ({len(candidates)} 个候选 token) ---")

    errs: list[str | None] = []
    for idx, creds in enumerate(candidates, 1):
        # 1. 查询状态（只读，用于判断 token 是否有效 / 链路是否可达）
        status_raw, err = _request_json(CHECKIN_STATUS_URL, creds)
        if err is not None:
            errs.append(err)
            if err == REASON_NETWORK:
                log(f"    token #{idx} ({creds['_source']}) 网络不可达/超时"
                    f"（已重试 {REQUEST_RETRIES} 次）")
            else:
                log(f"    token #{idx} ({creds['_source']}) 服务端拒绝（{err}），该 token 疑似已失效")
            continue
        errs.append(None)

        # 2. 领取签到
        if already_checked_in(status_raw):
            log(f"[ok] {name}: 今日已签到，无需重复领取。")
            return True, ""
        status = unwrap_data(status_raw)
        if status:
            log(f"    状态: active={status.get('active')}, streak_days={status.get('streak_days')}")

        result_raw, err2 = _request_json(DAILY_CHECKIN_URL, creds)
        if err2 == REASON_NETWORK:
            errs.append(err2)
            log(f"[!] {name}: 领取阶段网络不可达/超时（已重试 {REQUEST_RETRIES} 次），尝试下一个 token...")
            continue
        if already_checked_in(result_raw):
            log(f"[ok] {name}: 今日已签到。")
            return True, ""
        result = unwrap_data(result_raw)
        if result is None:
            log(f"[!] {name}: 领取失败（{err2 or '响应无法解析'}），尝试下一个 token...")
            continue

        success = result.get("success", True)
        credit = result.get("credit", result.get("today_credit", result.get("points")))
        streak = result.get("streak_days")
        message = result.get("message") or ""
        if success is False:
            log(f"[!] {name}: 领取未成功: {message or result}")
            return False, REASON_OTHER

        log(f"[ok] {name}: 签到成功! credit={credit}, streak_days={streak} {message}")
        return True, ""

    # 全部候选 token 都走完了。若每个候选的失败原因都是网络类，则判定为链路故障而非凭证失效。
    all_network = bool(errs) and all(e == REASON_NETWORK for e in errs)
    if all_network:
        log(f"[x] {name}: 网络不可达，未能完成签到（凭证本身大概率没问题）")
        return False, REASON_NETWORK
    log(f"[x] {name}: 所有候选 token 均无效，需重新登录该账号刷新 token")
    return False, REASON_AUTH


def _run_round(accounts: list[dict]) -> list[tuple[str, bool, str]]:
    """对所有账号跑一轮签到，返回 [(账号名, 是否成功, 失败原因)]。"""
    results: list[tuple[str, bool, str]] = []
    for account in accounts:
        name = account["account_name"]
        try:
            okflag, reason = checkin_one(account)
        except Exception as e:
            # 单个账号的未预期异常不应让整条 job 崩溃并抛出难看的 traceback
            log(f"[x] {name}: 未预期异常 {type(e).__name__}: {e}")
            okflag, reason = False, REASON_OTHER
        results.append((name, okflag, reason))
    return results


def main() -> bool:
    log("=" * 56)
    log(f"  WorkBuddy 每日签到（多账号）--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 56)

    accounts = load_all_credentials()
    if not accounts:
        log("[x] 未找到任何账号凭证：本地无 auth 文件，也未配置 config.json / 环境变量")
        gha_cmd("error", "未找到任何账号凭证：Secret WORKBUDDY_ACCOUNTS 可能为空或格式错误。")
        return False

    log(f"共发现 {len(accounts)} 个账号，开始逐一签到...")
    results = _run_round(accounts)
    ok = [n for n, s, _ in results if s]

    # 全部失败且原因全是「网络不可达」→ 判定为链路/接口临时抖动，而非凭证问题。
    # 这种故障窗口往往只有几十分钟，隔一会儿重试一轮就有机会救回来。
    if not ok and all(r[2] == REASON_NETWORK for r in results):
        log(f"[!] 全部 {len(results)} 个账号均为网络不可达。"
            f"{ALL_NETWORK_RETRY_DELAY}s 后整体重试一轮...")
        time.sleep(ALL_NETWORK_RETRY_DELAY)
        results = _run_round(accounts)
        ok = [n for n, s, _ in results if s]

    log("=" * 56)
    bad = [n for n, s, _ in results if not s]
    log(f"汇总: 成功 {len(ok)}/{len(results)}" + (f"，失败: {', '.join(bad)}" if bad else ""))
    _write_step_summary(results, ok, bad)

    # 失败策略：只有「全部账号都失败」才算致命故障，才让 job 报红。
    # 原因：只要任一账号失败就报红，会让单个 token 被服务端回收这种次级故障
    # 放大成每天一封的红色告警邮件（9/17–9/19 连续三天的误报就是这么来的）。
    if not bad:
        log("结果: 全部账号签到成功。")
        return True
    if ok:
        gha_cmd(
            "warning",
            f"部分账号签到失败: {', '.join(bad)}（成功 {len(ok)}/{len(results)}）。"
            "详见 Job Summary 的失败原因列（区分网络不可达与凭证失效）。",
        )
        log("结果: 部分失败（不致命，job 保持绿色，详见 Job Summary）。")
        return True

    # 全失败：必须区分「网络不可达」与「凭证失效」，否则会把链路故障误诊为 token 过期。
    if all(r[2] == REASON_NETWORK for r in results):
        gha_cmd(
            "error",
            f"全部 {len(results)} 个账号因【网络不可达】失败（已重试 {REQUEST_RETRIES} 次并额外整体重试一轮）。"
            "这不是凭证问题：runner 到 www.codebuddy.cn 的链路或接口临时不可用。",
        )
        log("结果: 全部失败，原因为网络不可达（非凭证问题）。")
    else:
        gha_cmd("error", f"全部 {len(results)} 个账号签到失败，请检查凭证有效性与接口可用性。")
        log("结果: 全部失败，判定为致命故障。")
    return False


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(0 if main() else 1)
