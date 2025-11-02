#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nikke API 练度采集（插件内直连接口）

功能概述：
- 直连接口获取用户角色详情数据。
- 支持通过 Cookie 或参数提供 intl_open_id、name_codes。
- 输出最近一次响应 latest.json 与 HTTP 状态码。

合规：
- 请确保你对目标账号拥有授权，并设置合理抓取频率（建议每用户 ≥ 5 分钟）。
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


API_URL_DEFAULT = "https://api.blablalink.com/api/game/proxy/Game/GetUserCharacterDetails"

# 路径常量统一到 AstrBot 专用数据目录与插件数据 Cookie
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


def build_headers(
    cookie_str: str,
    page_url: str,
    language: str = "en",
    user_agent: Optional[str] = None,
    extra_headers: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, str]:
    """
    构造请求头。按你提供的 DevTools 请求复刻关键字段。
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


def parse_name_codes(text: str) -> List[int]:
    """
    解析 --name-codes 的输入（逗号分隔或空格分隔），返回整数列表。
    """
    if not text:
        return []
    items: List[int] = []
    for seg in re.split(r"[,\s]+", text.strip()):
        if not seg:
            continue
        try:
            items.append(int(seg))
        except ValueError:
            raise ValueError(f"非法 name_code：{seg}")
    return items


def flatten_dict(d: Dict[str, Any], parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    """
    扁平化嵌套字典，方便 CSV 导出。
    """
    items: List[Tuple[str, Any]] = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def guess_rows_from_response(resp_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    尝试从响应中提取角色详情列表：
    - 常见字段：character_details / data.character_details / result.character_details
    - 若未找到，回退为将整个 JSON 扁平化后作为单行
    """
    candidates = [
        resp_json.get("character_details"),
        resp_json.get("data", {}).get("character_details") if isinstance(resp_json.get("data"), dict) else None,
        resp_json.get("result", {}).get("character_details") if isinstance(resp_json.get("result"), dict) else None,
    ]
    rows: List[Dict[str, Any]] = []
    for lst in candidates:
        if isinstance(lst, list) and lst:
            for row in lst:
                if isinstance(row, dict):
                    rows.append(row)
            if rows:
                return rows

    # 回退：仅一行扁平化
    return [resp_json]


def write_csv(rows: List[Dict[str, Any]], out_path: str) -> None:
    if not rows:
        # 空数据也写一个空 CSV，确保有产物
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            f.write("")
        return

    # 统一所有字段集合
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
        except requests.RequestException as e:
            if attempt >= max_retries:
                raise
            time.sleep(delay)
            delay *= 2.0
    # 理论不会到这
    raise RuntimeError("请求失败且未抛出异常")


def main() -> None:
    parser = argparse.ArgumentParser(description="Nikke 练度采集（插件内直连接口复刻）")
    parser.add_argument("--url", default=API_URL_DEFAULT, help="接口 URL（默认为 GetUserCharacterDetails）")
    parser.add_argument("--intl-open-id", default=None, help="国际 open_id；不传则尝试从 Cookie 的 game_openid 提取")
    parser.add_argument("--name-codes", default="", help="逗号或空格分隔的 name_code 列表，例如：5066,5004,5124")
    parser.add_argument("--area-id", type=int, default=81, help="nikke_area_id，默认为 81")
    parser.add_argument("--language", default="en", help="x-language 与 x-common-params 中的 language（默认 en）")
    parser.add_argument("--page-url", required=True, help="页面 URL（用于 x-common-params.data_statistics_page_id）")
    parser.add_argument("--cookie-file", default=COOKIE_DEFAULT, help="Cookie 文件路径（默认：插件数据目录 cookie.txt）")
    parser.add_argument("--extra-header", action="append", default=[], help='附加头部，格式 "Key=Value"，可重复传入')
    parser.add_argument("--out-prefix", default="GetUserCharacterDetails", help="输出文件名前缀")
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

    # name_codes
    name_codes = parse_name_codes(args.name_codes)
    if not name_codes:
        print("警告：未提供 --name-codes，将尝试仅以 open_id 拉取；若接口需要具体角色编码，请补充 --name-codes。", file=sys.stderr)

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
        "intl_open_id": intl_open_id,
        "nikke_area_id": args.area_id,
        "name_codes": name_codes,
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
    latest_path = os.path.join(STORAGE_DIR, "latest.json")

    # 尝试解析为 JSON
    resp_text = resp.text
    try:
        resp_json = resp.json()
    except ValueError:
        # 返回的不是标准 JSON（例如 HTML 登录页），不落盘以避免产生冗余文件
        print(f"HTTP 状态码：{resp.status_code}")
        return

    # 仅写入 latest.json，避免每次查询生成冗余文件
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(resp_json, f, ensure_ascii=False, indent=2)

    print(f"最近一次响应写入：{latest_path}")
    print(f"HTTP 状态码：{resp.status_code}")
    # 如需速率限制或缓存，可在上层调用层控制（例如每 openid 每 ≥5 分钟仅请求一次）。


if __name__ == "__main__":
    main()