#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nikke 中文名称映射抓取器

目标：
1) 支持手动传入一个或多个 JSON URL；
2) 支持自动从 Blablalink 当前首页与入口 JS 发现“逻辑名称表路径”；
3) 在本地复刻站点对逻辑路径的 CDN 哈希换算规则，自动得到最新真实 JSON 地址；
4) 从这些 JSON 中提取 name_code → 中文名 的映射。
"""

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    import requests  # type: ignore
except Exception:
    requests = None


SCRIPTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(SCRIPTS_DIR, "..", "..", "..", ".."))
PLUGIN_DATA_DIR = os.path.join(ROOT_DIR, "data", "plugin_data", "qbot_nikkeinformation")
STORAGE_DIR = PLUGIN_DATA_DIR
DEFAULT_OUT = os.path.join(STORAGE_DIR, "nikke_names_zh.json")

DEFAULT_DISCOVER_HOME_URL = "https://www.blablalink.com/"
DEFAULT_CDN_BASE = "https://sg-tools-cdn.blablalink.com/"
DEFAULT_LOGICAL_TEMPLATES = [
    "/character/{l_lang}/nikke_list_{lang}_v2.json",
    "/character/{l_lang}/nikke_list_{lang}.json",
]
SEGMENT_SEEDS = [224737, 1000639, 2654435761, 2654435769, 1000621, 4294967291]
ENTRY_JS_PATTERNS = [
    re.compile(r'<script[^>]+type=["\']module["\'][^>]+src=["\']([^"\']*index-[^"\']+\.js)["\']', re.IGNORECASE),
    re.compile(r'src=["\']([^"\']*index-[^"\']+\.js)["\']', re.IGNORECASE),
]
LOGICAL_TEMPLATE_PATTERNS = [
    re.compile(r'["\'](/character/\{l_lang\}/nikke_list_\{lang\}(?:_[^"\']*)?\.json)["\']'),
    re.compile(r'(/character/\{l_lang\}/nikke_list_\{lang\}(?:_[^"\'\s)]*)?\.json)'),
]
SITE_LANGUAGE_ALIASES = {
    "zh": "zh-TW",
    "zh-cn": "zh-TW",
    "zh-hans": "zh-TW",
    "zh-sg": "zh-TW",
    "zh-tw": "zh-TW",
    "zh-hk": "zh-TW",
    "tw": "zh-TW",
    "cht": "zh-TW",
    "ja": "ja",
    "jp": "ja",
    "ko": "ko",
    "en": "en",
    "th": "th",
    "tr": "tr",
    "ru": "ru",
    "ar": "ar",
    "id": "id",
    "ms": "ms",
    "vi": "vi",
    "de": "de",
    "fr": "fr",
    "it": "it",
    "pt": "pt-BR",
    "pt-br": "pt-BR",
    "ptbr": "pt-BR",
    "es": "es-US",
    "es-us": "es-US",
    "eslt": "es-US",
    "es-lt": "es-US",
    "es_lt": "es-US",
}


def ensure_dir(p: str) -> None:
    if not os.path.exists(p):
        os.makedirs(p, exist_ok=True)


def build_headers(language: str = "zh-CN", accept: str = "*/*", referer: str = DEFAULT_DISCOVER_HOME_URL) -> Dict[str, str]:
    """
    复刻浏览器关键头。该站点通常不需要 Cookie，仅要求 UA/Referer/Origin。
    """
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    return {
        "accept": accept,
        "accept-language": language + ",zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "origin": "https://www.blablalink.com",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": referer,
        "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "User-Agent": ua,
    }


def unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        s = str(item).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def http_get_bytes(url: str, headers: Dict[str, str], timeout: int = 20) -> bytes:
    if requests is not None:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url: str, language: str, referer: Optional[str] = None) -> str:
    headers = build_headers(
        language=language,
        accept="text/html,application/javascript,text/javascript,*/*;q=0.8",
        referer=referer or DEFAULT_DISCOVER_HOME_URL,
    )
    raw = http_get_bytes(url, headers=headers, timeout=20)
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("utf-8", errors="ignore")


def fetch_json(url: str, language: str) -> Any:
    headers = build_headers(language=language)
    raw = http_get_bytes(url, headers=headers, timeout=20)
    # 一些 CDN 返回的 content-type 可能不是 application/json，但正文仍是 JSON
    return json.loads(raw.decode("utf-8"))


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def hash_seed_value(text: str, seed: int) -> int:
    """
    复刻前端 JS 里的滚动哈希：

        s = (s * 33 + charCode) & 4294967295

    注意：
    - JS 位运算最终落在 32 位有符号整数空间；
    - Python 的 `& 0xFFFFFFFF` 默认得到无符号值，
      这里需要额外转回有符号 32 位，才能与前端结果一致。
    """
    value = int(seed)
    for ch in text:
        value = ((value * 33) + ord(ch)) & 0xFFFFFFFF
        if value >= 0x80000000:
            value -= 0x100000000
    return value


def segment_prefix(text: str, seed: int) -> str:
    value = hash_seed_value(text, seed)
    mod = ((value % seed) + seed) % seed
    left = (mod // 26) % 26
    right = mod % 26
    return f"{chr(97 + left)}{chr(97 + right)}"


def segment_suffix(text: str, seed: int) -> str:
    value = hash_seed_value(text, seed)
    mod = ((value % seed) + seed) % seed
    return str(mod % 99).zfill(2)


def encode_general_path(path: str) -> str:
    original = str(path).lstrip("/")
    parts = [p for p in original.split("/") if p]
    if not parts:
        return original

    encoded: List[str] = []
    for idx, part in enumerate(parts):
        if idx == len(parts) - 1:
            filename_parts = part.split(".")
            if filename_parts:
                filename_parts.pop(0)
            suffix = ".".join(filename_parts)
            digest = md5_hex(original)
            encoded.append(f"{digest}.{suffix}" if suffix else digest)
        else:
            seed = SEGMENT_SEEDS[min(idx, len(SEGMENT_SEEDS) - 1)]
            encoded.append(f"{segment_prefix(original, seed)}-{segment_suffix(original, seed)}")
    return "/".join(encoded)


def encode_spine_path(path: str) -> str:
    original = str(path).lstrip("/")
    parts = [p for p in original.split("/") if p]
    if not parts:
        return original

    shared_prefix = "/".join(parts[:-1])
    encoded: List[str] = []
    for idx, part in enumerate(parts):
        if idx == len(parts) - 1:
            filename_parts = part.split(".")
            basename = filename_parts.pop(0) if filename_parts else part
            suffix = ".".join(filename_parts)
            digest = md5_hex(basename)
            encoded.append(f"{digest}.{suffix}" if suffix else digest)
        else:
            seed = SEGMENT_SEEDS[min(idx, len(SEGMENT_SEEDS) - 1)]
            encoded.append(f"{segment_prefix(shared_prefix, seed)}-{segment_suffix(shared_prefix, seed)}")
    return "/".join(encoded)


def logical_path_to_cdn_url(path: str) -> str:
    normalized = str(path).lstrip("/")
    if not normalized:
        return DEFAULT_CDN_BASE
    encoded = encode_spine_path(normalized) if normalized.startswith("spine") else encode_general_path(normalized)
    return urljoin(DEFAULT_CDN_BASE, encoded)


def normalize_site_language(language: str) -> Tuple[str, str]:
    raw = str(language or "en").strip()
    key = raw.split(",", 1)[0].strip().lower().replace("_", "-")
    site_lang = SITE_LANGUAGE_ALIASES.get(key)
    if not site_lang and key.startswith("zh"):
        site_lang = "zh-TW"
    if not site_lang:
        site_lang = "en"

    lower_lang = site_lang.lower()
    if site_lang == "zh-TW":
        lower_lang = "zh-tw"
    return site_lang, lower_lang


def resolve_logical_template(template: str, language: str) -> str:
    site_lang, lower_lang = normalize_site_language(language)
    out = str(template).strip()
    out = out.replace("{lang}", site_lang)
    out = out.replace("{l_lang}", lower_lang)
    # 前端有一个特殊逻辑：韩文资源路径会去掉 `_ko`
    if lower_lang == "ko":
        out = out.replace("_ko", "")
    return out


def discover_entry_js_url(home_html: str, home_url: str) -> Optional[str]:
    for pattern in ENTRY_JS_PATTERNS:
        m = pattern.search(home_html)
        if m:
            return urljoin(home_url, m.group(1))
    return None


def discover_logical_templates(js_text: str) -> List[str]:
    found: List[str] = []
    for pattern in LOGICAL_TEMPLATE_PATTERNS:
        found.extend(match.group(1) for match in pattern.finditer(js_text))
    found = [item for item in found if "nikke_list_" in item]
    return unique_preserve_order(found)


def discover_source_urls(language: str, home_url: str, extra_templates: Optional[List[str]] = None) -> List[str]:
    templates: List[str] = []

    try:
        home_html = fetch_text(home_url, language=language, referer=home_url)
        entry_js_url = discover_entry_js_url(home_html, home_url)
        if entry_js_url:
            print(f"[DISCOVER] 入口 JS：{entry_js_url}")
            js_text = fetch_text(entry_js_url, language=language, referer=home_url)
            js_templates = discover_logical_templates(js_text)
            if js_templates:
                print(f"[DISCOVER] 从入口 JS 提取到 {len(js_templates)} 个逻辑模板。")
            templates.extend(js_templates)
        else:
            print("[WARN] 未在首页找到入口 JS，改用内置逻辑模板回退。", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] 自动发现失败：{e}", file=sys.stderr)

    if extra_templates:
        templates.extend(extra_templates)
    if not templates:
        templates.extend(DEFAULT_LOGICAL_TEMPLATES)

    urls: List[str] = []
    for template in unique_preserve_order(templates):
        resolved_path = resolve_logical_template(template, language)
        resolved_url = logical_path_to_cdn_url(resolved_path)
        print(f"[DISCOVER] 逻辑模板：{template}")
        print(f"[DISCOVER] 实际地址：{resolved_url}")
        urls.append(resolved_url)

    urls = unique_preserve_order(urls)
    print(f"[DISCOVER] 自动发现候选数：{len(urls)}")
    return urls


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


def pick_localized_name_dict(obj: Dict[str, Any]) -> Optional[str]:
    """
    兼容这类结构：
    - {"name": "先雷伊"}
    - {"zh-TW": "先雷伊", "en": "Rei"}
    """
    if "name" in obj:
        nv = obj.get("name")
        if isinstance(nv, str) and nv.strip():
            return nv.strip()

    preferred_keys = ("zh-cn", "zh-tw", "zh-hans", "zh-hant", "zh", "tw", "cn")
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for key in preferred_keys:
        value = lowered.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for value in obj.values():
        if isinstance(value, str) and is_chinese_text(value):
            return value.strip()
    return None


def try_get_name(d: Dict[str, Any]) -> Union[str, None]:
    """
    识别多种可能的名称承载位置：
    - 直接字段：name / display_name / nikke_name
    - 嵌套字段：name_localkey / name_localekey / name_localvalues / locale_name 等
    """
    for key in ("name", "display_name", "nikke_name"):
        value = d.get(key)
        if isinstance(value, str) and is_chinese_text(value):
            return value.strip()

    for key, value in d.items():
        if not isinstance(value, dict):
            continue
        lowered_key = str(key).lower()
        if "name_locale" in lowered_key or "name_local" in lowered_key or "locale_name" in lowered_key:
            localized = pick_localized_name_dict(value)
            if isinstance(localized, str) and localized.strip():
                return localized.strip()

    return None


def collect_pairs(obj: Any, mapping: Dict[str, str]) -> None:
    """
    递归扫描，遇到同一 dict 中同时可解析到 code 和 name，则加入映射。
    """
    if isinstance(obj, dict):
        code = try_get_code(obj)
        name = try_get_name(obj)
        if code is not None and isinstance(name, str) and name:
            mapping[str(code)] = name
        for value in obj.values():
            collect_pairs(value, mapping)
    elif isinstance(obj, list):
        for item in obj:
            collect_pairs(item, mapping)


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
        help="手动指定要抓取的 JSON 链接；可重复传入多个。",
    )
    parser.add_argument(
        "--discover-auto",
        action="store_true",
        help="自动从 Blablalink 首页与入口 JS 发现当前版本的最新名称表。",
    )
    parser.add_argument(
        "--discover-home-url",
        default=DEFAULT_DISCOVER_HOME_URL,
        help=f"自动发现所使用的首页地址（默认：{DEFAULT_DISCOVER_HOME_URL}）",
    )
    parser.add_argument(
        "--discover-template",
        action="append",
        default=[],
        help="额外追加的逻辑 JSON 模板；可重复传入多个。",
    )
    parser.add_argument(
        "--language",
        default="zh-CN",
        help="期望的名称语言（默认 zh-CN）。用于 Accept-Language 与逻辑路径语言占位符解析。",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"输出映射文件路径（默认：{DEFAULT_OUT}）",
    )
    args = parser.parse_args()

    ensure_dir(STORAGE_DIR)

    source_urls = unique_preserve_order(args.url or [])
    if args.discover_auto:
        auto_urls = discover_source_urls(
            language=args.language,
            home_url=args.discover_home_url,
            extra_templates=args.discover_template,
        )
        source_urls = unique_preserve_order([*source_urls, *auto_urls])

    if not source_urls:
        print("未提供可用数据源：既没有 --url，也没有启用 --discover-auto。", file=sys.stderr)
        sys.exit(2)

    merged: Dict[str, str] = {}
    total_sources = 0
    total_added = 0

    for url in source_urls:
        total_sources += 1
        try:
            data = fetch_json(url, language=args.language)
        except Exception as e:
            print(f"[WARN] 抓取失败：{url} → {e}", file=sys.stderr)
            continue

        local_map: Dict[str, str] = {}
        collect_pairs(data, local_map)
        if not local_map:
            print(f"[WARN] {url} 未解析到任何 (name_code→中文名) 配对；请确认 JSON 结构。", file=sys.stderr)
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