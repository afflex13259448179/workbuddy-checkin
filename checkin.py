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
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_BASE = "https://www.codebuddy.cn"
CHECKIN_STATUS_URL = f"{API_BASE}/v2/billing/meter/checkin-status"
DAILY_CHECKIN_URL = f"{API_BASE}/v2/billing/meter/daily-checkin"
REQUEST_TIMEOUT = 20

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


def _write_step_summary(results: list[tuple[str, bool]], ok: list[str], bad: list[str]) -> None:
    """把逐账号结果写进 GitHub Actions 的 Job Summary。

    部分失败时 job 仍然是绿的，所以这里是唯一稳定可见的失败信号，不能省。
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## WorkBuddy 每日签到",
        "",
        f"- 运行时间（runner 本地）：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
        f"- 结果：**成功 {len(ok)}/{len(results)}**",
        "",
        "| 账号 | 结果 |",
        "| --- | --- |",
    ]
    for name, okflag in results:
        lines.append(f"| {name} | {'✅ 成功' if okflag else '❌ 失败'} |")
    lines.append("")
    if bad:
        lines.append(
            f"> ⚠️ 失败账号：**{', '.join(bad)}** —— 通常是该账号 token 已被服务端回收，"
            "需要重新登录该账号并刷新 `WORKBUDDY_ACCOUNTS`。"
        )
    else:
        lines.append("> ✅ 全部账号签到成功。")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
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


def _request_json(url: str, creds: dict, method: str = "POST") -> dict | None:
    req = urllib.request.Request(
        url,
        data=b"{}",
        method=method.upper(),
        headers=_build_headers(creds),
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            return json.loads(err_body) if err_body else None
        except Exception:
            return None
    except Exception:
        return None


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

def checkin_one(account: dict) -> bool:
    name = account["account_name"]
    candidates = account["candidates"]
    log(f"--- 账号: {name} ({len(candidates)} 个候选 token) ---")

    for idx, creds in enumerate(candidates, 1):
        # 1. 查询状态（只读，用于判断 token 是否有效）
        status_raw = _request_json(CHECKIN_STATUS_URL, creds)
        if status_raw is None:
            log(f"    token #{idx} ({creds['_source']}) 无效（401/网络失败），尝试下一个...")
            continue

        # 2. 领取签到
        if already_checked_in(status_raw):
            log(f"[ok] {name}: 今日已签到，无需重复领取。")
            return True
        status = unwrap_data(status_raw)
        if status:
            log(f"    状态: active={status.get('active')}, streak_days={status.get('streak_days')}")

        result_raw = _request_json(DAILY_CHECKIN_URL, creds)
        if already_checked_in(result_raw):
            log(f"[ok] {name}: 今日已签到。")
            return True
        result = unwrap_data(result_raw)
        if result is None:
            log(f"[!] {name}: 领取失败，尝试下一个 token...")
            continue

        success = result.get("success", True)
        credit = result.get("credit", result.get("today_credit", result.get("points")))
        streak = result.get("streak_days")
        message = result.get("message") or ""
        if success is False:
            log(f"[!] {name}: 领取未成功: {message or result}")
            return False

        log(f"[ok] {name}: 签到成功! credit={credit}, streak_days={streak} {message}")
        return True

    log(f"[x] {name}: 所有候选 token 均无效，需重新登录该账号刷新 token")
    return False


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
    results: list[tuple[str, bool]] = []
    for account in accounts:
        name = account["account_name"]
        try:
            results.append((name, checkin_one(account)))
        except Exception as e:
            # 单个账号的未预期异常不应让整条 job 崩溃并抛出难看的 traceback
            log(f"[x] {name}: 未预期异常 {type(e).__name__}: {e}")
            results.append((name, False))

    log("=" * 56)
    ok = [n for n, s in results if s]
    bad = [n for n, s in results if not s]
    log(f"汇总: 成功 {len(ok)}/{len(results)}" + (f"，失败: {', '.join(bad)}" if bad else ""))
    _write_step_summary(results, ok, bad)

    # 失败策略：只有「全部账号都失败」才算致命故障，才让 job 报红。
    # 原因：只有任一账号失败就报红，会让单个 token 被服务端回收这种次级故障
    # 放大成每天一封的红色告警邮件（9/17–9/19 连续三天的误报就是这么来的）。
    if not bad:
        log("结果: 全部账号签到成功。")
        return True
    if ok:
        gha_cmd(
            "warning",
            f"部分账号签到失败: {', '.join(bad)}（成功 {len(ok)}/{len(results)}）。"
            "这些账号的 token 可能已被服务端回收，请重新登录该账号并刷新 Secret WORKBUDDY_ACCOUNTS。",
        )
        log("结果: 部分失败（不致命，job 保持绿色，详见 Job Summary）。")
        return True

    gha_cmd("error", f"全部 {len(results)} 个账号签到失败，请检查凭证有效性与接口可用性。")
    log("结果: 全部失败，判定为致命故障。")
    return False


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(0 if main() else 1)
