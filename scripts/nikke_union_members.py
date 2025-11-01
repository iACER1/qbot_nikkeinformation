#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nikke 工会突袭成员出刀数据采集（插件内直连接口）

功能:
- 直连 GetUnionRaidData 接口，返回当前联盟成员的出刀记录（含 nickname、openid、total_damage 等）
- 将响应写入插件 storage 目录下的 union_raid_members_latest.json
- 控制台输出摘要，供上层插件解析（HTTP 状态码与 latest 路径）

合规提示:
- 请确保你对目标账号拥有授权，且抓取频率合理（建议每用户 ≥ 5 分钟），避免影响站点服务。
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


API_URL_DEFAULT = "https://api.blablalink.com/api/game/proxy/Game/GetUnionRaidData"

# 插件内路径
SCRIPTS_DIR = os.path.abspath(os.path.dirname(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
STORAGE_DIR = os.path.join(PLUGIN_ROOT, "storage")
COOKIE_DEFAULT = os.path.join(PLUGIN_ROOT, ".nikke_auth", "cookie.txt")


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
    parser.add_argument("--cookie-file", default=COOKIE_DEFAULT, help="Cookie 文件路径（默认：插件目录下 .nikke_auth/cookie.txt）")
    parser.add_argument("--extra-header", action="append", default=[], help='附加头部，格式 "Key=Value"，可重复传入')
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

    # 处理响应
    latest_path = os.path.join(STORAGE_DIR, "union_raid_members_latest.json")

    try:
        resp_json = resp.json()
    except ValueError:
        print(f"HTTP 状态码：{resp.status_code}")
        return

    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(resp_json, f, ensure_ascii=False, indent=2)

    # 控制台摘要，供上层解析
    print(f"union_raid_members_latest.json：{latest_path}")
    print(f"HTTP 状态码：{resp.status_code}")


if __name__ == "__main__":
    main()