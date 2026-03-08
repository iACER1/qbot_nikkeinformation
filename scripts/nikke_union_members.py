#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nikke 工会突袭成员出刀数据采集（插件内直连接口）

功能:
- 直连 GetUnionRaidData 接口，返回当前联盟成员的出刀记录（含 nickname、openid、total_damage 等）
- 将响应写入插件 storage 目录下的“本次请求独立结果文件”
- 控制台输出摘要，供上层插件解析（HTTP 状态码与结果路径）

合规提示:
- 请确保你对目标账号拥有授权，且抓取频率合理（建议每用户 ≥ 5 分钟），避免影响站点服务。
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests


API_URL_DEFAULT = "https://api.blablalink.com/api/game/proxy/Game/GetUnionRaidData"

# 插件内路径
SCRIPTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(SCRIPTS_DIR, "..", "..", "..", ".."))
PLUGIN_DATA_DIR = os.path.join(ROOT_DIR, "data", "plugin_data", "qbot_nikkeinformation")
STORAGE_DIR = PLUGIN_DATA_DIR
COOKIE_DEFAULT = os.path.join(PLUGIN_DATA_DIR, "cookie.txt")


def ensure_dir(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def read_cookie_file(path: str) -> str:
    """
    读取 cookie.txt，返回 Cookie 头部字符串
    - 支持两种格式：
      1) 以 "Cookie:" 开头的整行
      2) 仅键值对（以分号分隔）
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        raise ValueError("Cookie 文件为空：%s" % path)
    # 去掉可能的前缀
    content = re.sub(r"(?i)^cookie:\s*", "", content).strip()
    # 简单规范化分隔符
    content = "; ".join([seg.strip() for seg in content.split(";") if seg.strip()])
    return content


def parse_cookie_to_dict(cookie_str: str) -> Dict[str, str]:
    """
    将 Cookie 头部字符串解析为字典，便于提取 game_openid 等特定键
    """
    cookie_dict: Dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        cookie_dict[k.strip()] = v.strip()
    return cookie_dict


def format_cookie_dict(cookie_dict: Dict[str, str]) -> str:
    return "; ".join(
        f"{str(k).strip()}={str(v).strip()}"
        for k, v in cookie_dict.items()
        if str(k).strip() and v is not None and str(v).strip()
    )


def merge_response_cookies(
    original_cookie_str: str,
    session: requests.Session,
    response: requests.Response,
) -> Tuple[str, int]:
    merged = parse_cookie_to_dict(original_cookie_str)
    changed = 0

    def upsert(name: str, value: str) -> None:
        nonlocal changed
        key = str(name or "").strip()
        val = str(value or "").strip()
        if not key:
            return
        if not val:
            if key in merged:
                del merged[key]
                changed += 1
            return
        if merged.get(key) != val:
            merged[key] = val
            changed += 1

    try:
        for k, v in session.cookies.items():
            upsert(k, v)
    except Exception:
        pass

    try:
        for k, v in response.cookies.items():
            upsert(k, v)
    except Exception:
        pass

    return format_cookie_dict(merged), changed


def persist_response_cookies(
    cookie_path: str,
    original_cookie_str: str,
    session: requests.Session,
    response: requests.Response,
) -> int:
    merged_cookie_str, changed = merge_response_cookies(original_cookie_str, session, response)
    if changed <= 0 or not merged_cookie_str:
        return 0
    atomic_write_text(cookie_path, merged_cookie_str)
    return changed


def build_non_json_error(resp: requests.Response) -> str:
    raw = resp.text or ""
    snippet = re.sub(r"\s+", " ", raw).strip()[:200]
    lowered = snippet.lower()
    auth_markers = ("login", "sign in", "signin", "unauthorized", "forbidden", "access denied", "请登录", "登入", "登录")
    if resp.status_code in (401, 403) or any(token in lowered for token in auth_markers):
        return f"响应不是有效 JSON，疑似 Cookie 已失效、需要重新登录或请求被拦截（HTTP {resp.status_code}，响应片段：{snippet or '空'}）"
    return f"响应不是有效 JSON（HTTP {resp.status_code}，响应片段：{snippet or '空'}）"


def build_headers(
    cookie_str: str,
    page_url: str,
    language: str = "zh-TW",
    user_agent: Optional[str] = None,
    extra_headers: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, str]:
    """
    构造请求头。按 DevTools 请求复刻关键字段。
    """
    if user_agent is None:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"

    common_params = {
        "game_id": "16",
        "area_id": "global",
        "source": "pc_web",
        "intl_game_id": "29080",
        "language": language,
        "env": "prod",
        "data_statistics_scene": "outer",
        "data_statistics_page_id": page_url,
        "data_statistics_client_type": "pc_web",
        "data_statistics_lang": language,
    }

    headers: Dict[str, str] = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "origin": "https://www.blablalink.com",
        "referer": "https://www.blablalink.com/",
        "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "x-channel-type": "2",
        "x-common-params": json.dumps(common_params, ensure_ascii=False),
        "x-language": language,
        "User-Agent": user_agent,
        "Cookie": cookie_str,
        "Content-Type": "application/json",
    }

    if extra_headers:
        for k, v in extra_headers:
            headers[k] = v

    return headers


def sanitize_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "request"


def build_output_path(
    storage_dir: str,
    out_prefix: str,
    out_path: Optional[str] = None,
    request_id: Optional[str] = None,
) -> str:
    if out_path:
        return os.path.abspath(out_path)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    prefix = sanitize_filename_component(out_prefix or "union_raid_members_latest")
    req = sanitize_filename_component(request_id or uuid.uuid4().hex)
    return os.path.join(storage_dir, f"{timestamp}_{req}_{prefix}.json")


def atomic_write_text(path: str, content: str) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def atomic_write_json(path: str, obj: Any) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def cleanup_old_result_files(
    storage_dir: str,
    prefix: str,
    *,
    current_path: Optional[str] = None,
    suffix: str = ".json",
    keep: int = 20,
    max_age_seconds: int = 72 * 3600,
) -> int:
    if not os.path.isdir(storage_dir):
        return 0

    prefix = sanitize_filename_component(prefix)
    current_abs = os.path.abspath(current_path) if current_path else ""
    legacy_name = f"{prefix}{suffix}"
    now = time.time()
    candidates: List[Tuple[float, str]] = []

    for fn in os.listdir(storage_dir):
        if not (fn == legacy_name or fn.endswith(f"_{prefix}{suffix}")):
            continue
        full = os.path.abspath(os.path.join(storage_dir, fn))
        if current_abs and full == current_abs:
            continue
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        candidates.append((mtime, full))

    candidates.sort(key=lambda x: x[0], reverse=True)

    keep = max(0, int(keep))
    max_age_seconds = max(0, int(max_age_seconds))
    to_delete: List[str] = []

    for idx, (mtime, full) in enumerate(candidates):
        overflow = keep == 0 or idx >= keep
        too_old = max_age_seconds > 0 and (now - mtime) > max_age_seconds
        if overflow or too_old:
            to_delete.append(full)

    removed = 0
    for full in to_delete:
        try:
            os.remove(full)
            removed += 1
        except Exception:
            pass
    return removed


def exponential_backoff_request(
    method: str,
    url: str,
    session: requests.Session,
    headers: Dict[str, str],
    json_body: Dict[str, Any],
    timeout_sec: int = 15,
    max_retries: int = 3,
) -> requests.Response:
    """
    简单的指数退避重试：1s, 2s, 4s ...
    """
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                json=json_body,
                timeout=timeout_sec,
            )
            return resp
        except requests.RequestException:
            if attempt >= max_retries:
                raise
            time.sleep(delay)
            delay *= 2.0
    raise RuntimeError("请求失败且未抛出异常")


def main() -> None:
    parser = argparse.ArgumentParser(description="Nikke 工会成员出刀数据采集（插件内直连接口复刻）")
    parser.add_argument("--url", default=API_URL_DEFAULT, help="接口 URL（默认为 GetUnionRaidData）")
    parser.add_argument("--intl-open-id", default=None, help="国际 open_id；不传则尝试从 Cookie 的 game_openid 提取")
    parser.add_argument("--area-id", type=int, default=81, help="nikke_area_id，默认为 81")
    parser.add_argument("--language", default="zh-TW", help="x-language 与 x-common-params 中的 language（默认 zh-TW）")
    parser.add_argument("--page-url", required=True, help="页面 URL（用于 x-common-params.data_statistics_page_id）")
    parser.add_argument("--cookie-file", default=COOKIE_DEFAULT, help="Cookie 文件路径（默认：插件数据目录 cookie.txt）")
    parser.add_argument("--extra-header", action="append", default=[], help='附加头部，格式 "Key=Value"，可重复传入')
    parser.add_argument("--out-prefix", default="union_raid_members_latest", help="输出文件名前缀（默认 union_raid_members_latest）")
    parser.add_argument("--request-id", default="", help="可选，请求标识；用于生成独立输出文件名")
    parser.add_argument("--out-path", default="", help="可选，指定本次响应 JSON 的输出路径；未指定则自动生成独立文件")
    parser.add_argument("--retain-count", type=int, default=20, help="最多保留多少个同类结果文件（默认 20）")
    parser.add_argument("--retain-hours", type=int, default=72, help="结果文件保留时长（小时，默认 72；传 0 表示不按时间删除）")
    args = parser.parse_args()

    ensure_dir(STORAGE_DIR)

    # 读取 Cookie
    cookie_path = os.getenv("NIKKE_COOKIE_PATH", args.cookie_file).strip() or args.cookie_file
    cookie_str = read_cookie_file(cookie_path)
    cookie_dict = parse_cookie_to_dict(cookie_str)

    # intl_open_id：优先来自参数，否则取 Cookie 的 game_openid
    intl_open_id = args.intl_open_id or cookie_dict.get("game_openid")
    if not intl_open_id:
        raise ValueError("未提供 --intl-open-id，且 Cookie 中不存在 game_openid；请至少提供其一。")

    # 额外头部
    extra_headers: List[Tuple[str, str]] = []
    for kv in args.extra_header:
        if "=" not in kv:
            raise ValueError(f"--extra-header 格式错误，应为 Key=Value：{kv}")
        k, v = kv.split("=", 1)
        extra_headers.append((k.strip(), v.strip()))

    # 构造头
    headers = build_headers(
        cookie_str=cookie_str,
        page_url=args.page_url,
        language=args.language,
        extra_headers=extra_headers,
    )

    # 构造 Body（按 DevTools 请求）
    body = {
        "nikke_area_id": args.area_id,
        "intl_open_id": str(intl_open_id),
    }

    # 发起请求
    session = requests.Session()
    resp = exponential_backoff_request(
        method="POST",
        url=args.url,
        session=session,
        headers=headers,
        json_body=body,
        timeout_sec=15,
        max_retries=3,
    )

    try:
        cookie_updates = persist_response_cookies(cookie_path, cookie_str, session, resp)
        if cookie_updates > 0:
            print(f"[INFO] 已自动更新 Cookie：{cookie_path}（变更 {cookie_updates} 项）")
    except Exception as e:
        print(f"[WARN] 自动回写 Cookie 失败：{e}", file=sys.stderr)

    try:
        resp_json = resp.json()
    except ValueError:
        print(build_non_json_error(resp), file=sys.stderr)
        print(f"HTTP 状态码：{resp.status_code}", file=sys.stderr)
        sys.exit(3)

    output_path = build_output_path(
        storage_dir=STORAGE_DIR,
        out_prefix=args.out_prefix,
        out_path=args.out_path,
        request_id=args.request_id,
    )
    atomic_write_json(output_path, resp_json)

    removed = cleanup_old_result_files(
        storage_dir=STORAGE_DIR,
        prefix=args.out_prefix,
        current_path=output_path,
        keep=args.retain_count,
        max_age_seconds=args.retain_hours * 3600,
    )

    # 控制台摘要，供上层解析
    print(f"union_raid_members_latest.json：{output_path}")
    if removed > 0:
        print(f"[CLEANUP] 已清理旧结果文件：{removed} 个")
    print(f"HTTP 状态码：{resp.status_code}")


if __name__ == "__main__":
    main()