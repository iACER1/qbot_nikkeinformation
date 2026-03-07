#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nikke 用户战力前十 name_code 提取（插件内脚本）

迁移说明：
- 本脚本已移动至插件目录：/AstrBot/data/plugins/qbot_nikkeinformation/scripts
- 产物统一输出至：/AstrBot/data/plugins/qbot_nikkeinformation/storage
- 默认 Cookie 文件：/AstrBot/data/plugins/qbot_nikkeinformation/.nikke_auth/cookie.txt
  可用 --cookie-file 或环境变量 NIKKE_COOKIE_PATH 覆盖。

功能：
- 调用 GetUserCharacters 接口，获取目标用户全部妮姬的基础信息。
- 按“战力/战斗力”字段降序排序，取前 N（默认 10），输出前 N 的 name_code 列表与简单 CSV。
- 作为上游模块，生成的 name_code 串用于 nikke_api.py 的详情拉取。

合规：
- 仅在用户授权且公开可查询的前提下使用；请遵守限频与站点条款。
"""

import argparse
import base64
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


API_URL_DEFAULT = "https://api.blablalink.com/api/game/proxy/Game/GetUserCharacters"

# 插件内路径
SCRIPTS_DIR = os.path.abspath(os.path.dirname(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
STORAGE_DIR = os.path.join(PLUGIN_ROOT, "storage")
COOKIE_DEFAULT = os.path.join(PLUGIN_ROOT, ".nikke_auth", "cookie.txt")


def ensure_dir(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def read_cookie_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        raise ValueError(f"Cookie 文件为空：{path}")
    content = re.sub(r"(?i)^cookie:\s*", "", content).strip()
    content = "; ".join([seg.strip() for seg in content.split(";") if seg.strip()])
    return content


def parse_cookie_to_dict(cookie_str: str) -> Dict[str, str]:
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
    ensure_dir(os.path.dirname(cookie_path) or ".")
    with open(cookie_path, "w", encoding="utf-8") as f:
        f.write(merged_cookie_str)
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
    language: str = "en",
    user_agent: Optional[str] = None,
    extra_headers: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, str]:
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


def exponential_backoff_request(
    method: str,
    url: str,
    session: requests.Session,
    headers: Dict[str, str],
    json_body: Dict[str, Any],
    timeout_sec: int = 15,
    max_retries: int = 3,
) -> requests.Response:
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


def decode_intl_open_id_from_base64(openid_b64: str) -> Optional[str]:
    """
    将 URL 的 openid（Base64）解码为 '29080-XXXXXXXX...' 格式，取连字符后的数字作为 intl_open_id。
    """
    try:
        raw = base64.b64decode(openid_b64).decode("utf-8")
    except Exception:
        return None
    if "-" in raw:
        return raw.split("-", 1)[1]
    if raw.isdigit():
        return raw
    return None


def pick_power_value(row: Dict[str, Any]) -> Optional[int]:
    """
    从角色项中提取战力值（多字段兼容）。
    兼容字段：power, combat, combat_power, max_power, fight_power, score, power_score
    """
    candidates = ["power", "combat", "combat_power", "max_power", "fight_power", "score", "power_score"]
    for key in candidates:
        v = row.get(key)
        if v is None:
            continue
        try:
            iv = int(float(str(v)))
            return iv
        except Exception:
            continue
    return None


def extract_name_code(row: Dict[str, Any]) -> Optional[int]:
    """
    从角色项中提取 name_code（多字段兼容）。
    """
    candidates = ["name_code", "code", "nikke_code", "nameCode"]
    for key in candidates:
        v = row.get(key)
        if v is None:
            continue
        try:
            iv = int(str(v))
            return iv
        except Exception:
            continue
    return None


def flatten_dict(d: Dict[str, Any], parent: str = "", sep: str = ".") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in d.items():
        nk = f"{parent}{sep}{k}" if parent else k
        if isinstance(v, dict):
            out.update(flatten_dict(v, nk, sep))
        else:
            out[nk] = v
    return out


def write_csv(rows: List[Dict[str, Any]], out_path: str) -> None:
    if not rows:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            f.write("")
        return
    all_keys: List[str] = []
    flat_rows: List[Dict[str, Any]] = []
    for r in rows:
        fr = flatten_dict(r)
        flat_rows.append(fr)
        for k in fr.keys():
            if k not in all_keys:
                all_keys.append(k)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        for fr in flat_rows:
            writer.writerow(fr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Nikke 用户战力前 N 名的 name_code 提取（插件内）")
    parser.add_argument("--url", default=API_URL_DEFAULT, help="接口 URL（默认为 GetUserCharacters）")
    parser.add_argument("--intl-open-id", default=None, help="国际 open_id；优先使用此参数")
    parser.add_argument("--openid-base64", default=None, help="URL 中的 openid（Base64），自动解码为 intl_open_id")
    parser.add_argument("--area-id", type=int, default=81, help="nikke_area_id，默认为 81")
    parser.add_argument("--language", default="en", help="x-language 与统计字段 language（默认 en）")
    parser.add_argument("--page-url", required=True, help="页面 URL（用于 x-common-params.data_statistics_page_id）")
    parser.add_argument("--cookie-file", default=COOKIE_DEFAULT, help="Cookie 文件路径（默认：插件目录下 .nikke_auth/cookie.txt）")
    parser.add_argument("--extra-header", action="append", default=[], help='附加头部，格式 "Key=Value"，可重复传入')
    parser.add_argument("--top", type=int, default=10, help="取前 N 名（默认 10）")
    parser.add_argument("--out-prefix", default="TopCodes", help="输出文件名前缀")
    args = parser.parse_args()

    ensure_dir(STORAGE_DIR)

    # 读取 Cookie（优先环境变量覆盖）
    cookie_path = os.getenv("NIKKE_COOKIE_PATH", args.cookie_file).strip() or args.cookie_file
    cookie_str = read_cookie_file(cookie_path)
    cookie_dict = parse_cookie_to_dict(cookie_str)

    # intl_open_id 优先参数，其次 openid-base64 解码，最后从 Cookie 的 game_openid 兜底
    intl_open_id = args.intl_open_id
    if not intl_open_id and args.openid_base64:
        intl_from_b64 = decode_intl_open_id_from_base64(args.openid_base64)
        if intl_from_b64:
            intl_open_id = intl_from_b64
    if not intl_open_id:
        intl_open_id = cookie_dict.get("game_openid")
    if not intl_open_id:
        raise ValueError("未提供 --intl-open-id / --openid-base64，且 Cookie 中不存在 game_openid；无法确定目标用户。")

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

    # 构造 Body
    body = {
        "intl_open_id": intl_open_id,
        "nikke_area_id": args.area_id,
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

    # 停用逐次落盘：不再生成时间戳 JSON/CSV

    # 解析 JSON
    try:
        data = resp.json()
    except ValueError:
        print(build_non_json_error(resp), file=sys.stderr)
        print(f"HTTP 状态码：{resp.status_code}", file=sys.stderr)
        sys.exit(3)

    # 可能的列表字段名称（尽量兼容）
    candidates = [
        data.get("characters"),
        data.get("data", {}).get("characters") if isinstance(data.get("data"), dict) else None,
        data.get("data", {}).get("character_list") if isinstance(data.get("data"), dict) else None,
        data.get("character_list"),
        data.get("list"),
        data.get("data", {}).get("list") if isinstance(data.get("data"), dict) else None,
    ]

    rows: List[Dict[str, Any]] = []
    for lst in candidates:
        if isinstance(lst, list) and lst:
            for r in lst:
                if isinstance(r, dict):
                    rows.append(r)
            if rows:
                break

    if not rows:
        msg = ""
        if isinstance(data, dict):
            for key in ("message", "msg", "error_message", "error"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    msg = val.strip()
                    break
        print(f"HTTP 状态码：{resp.status_code}", file=sys.stderr)
        if msg:
            print(f"接口未返回角色列表：{msg}", file=sys.stderr)
        else:
            print("接口未返回可解析的角色列表。", file=sys.stderr)
        sys.exit(4)

    # 为每行提取 name_code 与 power
    normalized: List[Dict[str, Any]] = []
    for r in rows:
        name_code = extract_name_code(r)
        power = pick_power_value(r)
        name = r.get("name") or r.get("nikke_name") or r.get("display_name")
        nr = dict(r)  # 保留原字段
        nr["_name_code"] = name_code
        nr["_power"] = power
        if name is not None:
            nr["_name"] = name
        normalized.append(nr)

    # 排序并取前 N
    normalized.sort(key=lambda x: (x.get("_power") or 0), reverse=True)
    top_n = normalized[: max(1, args.top)]

    # 收集 name_codes（过滤 None）
    top_codes = [str(x.get("_name_code")) for x in top_n if x.get("_name_code") is not None]
    name_codes_arg = ",".join(top_codes)

    # 增补输出字段
    out_obj = {
        "intl_open_id": intl_open_id,
        "top": args.top,
        "count_extracted": len(rows),
        "codes": top_codes,
        "name_codes_arg": name_codes_arg,
        "items": top_n,
        "http_status": resp.status_code,
    }

    # 不再写入时间戳 JSON/CSV，避免产生冗余文件

    # 控制台输出可直接粘贴的参数
    print(f"HTTP 状态码：{resp.status_code}")
    print(f"用于 nikke_api.py 的 --name-codes 参数值：{name_codes_arg}")
    if not name_codes_arg:
        print("警告：未能提取任何 name_code，请检查响应字段名称或该用户是否为空列表。")


if __name__ == "__main__":
    main()