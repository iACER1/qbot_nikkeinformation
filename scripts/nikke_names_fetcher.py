#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nikke 中文名称映射抓取器
- 目标：从静态 CDN JSON（如 sg-tools-cdn.blablalink.com/.../*.json）中提取 name_code → 中文名 的映射。
- 用法（示例）：
    python data/plugins/qbot_nikkeinformation/scripts/nikke_names_fetcher.py ^
        --url https://sg-tools-cdn.blablalink.com/vm-36/bj-70/6223a9fbfd3be53b48587c934a91f686.json ^
        --out data/plugins/qbot_nikkeinformation/storage/nikke_names_zh.json

说明
- 这些 CDN JSON 通常是“公共库/素材库”数据快照，条目中存在：
    name_code: 5115
    name_localkey / name_localekey: { name: "先雷伊" }
  或其它相近结构。本脚本会递归扫描 JSON，自动识别并产出映射字典。
- 之后 main.py 的汇总逻辑将优先用该映射把 code 替换为中文名。
"""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Tuple, Union

import requests


SCRIPTS_DIR = os.path.abspath(os.path.dirname(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
STORAGE_DIR = os.path.join(PLUGIN_ROOT, "storage")
DEFAULT_OUT = os.path.join(STORAGE_DIR, "nikke_names_zh.json")


def ensure_dir(p: str) -> None:
    if not os.path.exists(p):
        os.makedirs(p, exist_ok=True)


def build_headers(language: str = "zh-CN") -> Dict[str, str]:
    """
    复刻浏览器关键头。该 CDN 通常不需要 Cookie，仅要求 UA/Referer/Origin。
    """
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    return {
        "accept": "*/*",
        "accept-language": language + ",zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "origin": "https://www.blablalink.com",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": "https://www.blablalink.com/",
        "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "User-Agent": ua,
    }


def is_chinese_text(s: str) -> bool:
    try:
        return bool(re.search(r"[\u4e00-\u9fff]", s))
    except Exception:
        return False


def try_get_code(d: Dict[str, Any]) -> Union[int, None]:
    for key in ("name_code", "code", "nikke_code", "nameCode"):
        if key in d:
            try:
                return int(str(d.get(key)))
            except Exception:
                continue
    return None


def try_get_name(d: Dict[str, Any]) -> Union[str, None]:
    """
    识别多种可能的名称承载位置：
    - name_localkey / name_localekey / name_locale_key ... 下的 { name: "中文" }
    - 直接的 name / display_name（若为中文）
    """
    # 直接 name / display_name
    for key in ("name", "display_name", "nikke_name"):
        v = d.get(key)
        if isinstance(v, str) and is_chinese_text(v):
            return v.strip()

    # 嵌套的本地化对象
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        lk = k.lower()
        if ("name_locale" in lk or "name_local" in lk or "locale_name" in lk) and "name" in v:
            nv = v.get("name")
            if isinstance(nv, str) and nv.strip():
                return nv.strip()

    return None


def collect_pairs(obj: Any, mapping: Dict[str, str]) -> None:
    """
    递归扫描，遇到同一 dict 中同时可解析到 code 和 name，则加入映射。
    """
    if isinstance(obj, dict):
        code = try_get_code(obj)
        nm = try_get_name(obj)
        if code is not None and isinstance(nm, str) and nm:
            mapping[str(code)] = nm
        for v in obj.values():
            collect_pairs(v, mapping)
    elif isinstance(obj, list):
        for it in obj:
            collect_pairs(it, mapping)


def fetch_json(url: str, language: str) -> Any:
    headers = build_headers(language=language)
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    # 一些 CDN 返回的 content-type 可能不是 application/json，但正文仍是 JSON
    return resp.json()
    

def merge_mappings(base: Dict[str, str], extra: Dict[str, str]) -> Dict[str, str]:
    out = dict(base)
    out.update(extra)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Nikke 中文名称映射抓取器（code→中文名）")
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="要抓取的 CDN JSON 链接；可重复传入多个。",
    )
    parser.add_argument(
        "--language",
        default="zh-CN",
        help="期望的名称语言（默认 zh-CN）。用于 Accept-Language 头。",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"输出映射文件路径（默认：{DEFAULT_OUT}）",
    )
    args = parser.parse_args()

    ensure_dir(STORAGE_DIR)

    if not args.url:
        print("未提供 --url。你可以从 DevTools/网络面板复制 CDN JSON 链接，然后重复传入多个 --url。", file=sys.stderr)
        sys.exit(2)

    merged: Dict[str, str] = {}
    total_sources = 0
    total_added = 0

    for url in args.url:
        total_sources += 1
        try:
            data = fetch_json(url, language=args.language)
        except Exception as e:
            print(f"[WARN] 抓取失败：{url} → {e}", file=sys.stderr)
            continue
        local_map: Dict[str, str] = {}
        collect_pairs(data, local_map)
        if not local_map:
            print(f"[WARN] {url} 未解析到任何 (name_code→中文名) 配对；请确认 JSON 结构或语言是否为 zh-CN。", file=sys.stderr)
        else:
            print(f"[INFO] {url} 解析到 {len(local_map)} 条配对。")
        before = len(merged)
        merged = merge_mappings(merged, local_map)
        total_added += (len(merged) - before)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"已写出映射：{args.out}")
    print(f"来源文件数：{total_sources}，新增映射数：{total_added}，当前总映射键数：{len(merged)}")


if __name__ == "__main__":
    main()