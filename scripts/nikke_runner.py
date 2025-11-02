#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nikke 统一主程序（插件内脚本）

功能概述：
- 输入 openid（Base64）或 intl_open_id。
- 自动获取该用户“战力前十”的 name_code 并拉取详情。
- 控制台仅输出最终摘要。

工作流：
1) 调用 nikke_top_codes.py 获取前十 name_code（内部捕获不回显）。
2) 调用 nikke_api.py 拉取前十详情，并输出 JSON/CSV 路径与状态。

产物：
- storage/<timestamp>_TopCodes.json / .csv
- storage/<timestamp>_GetUserCharacterDetails.json / .csv
- storage/latest.json

合规：
- 目标用户需公开查询，Cookie 必须有效。
- 建议限频，每用户 ≥ 5 分钟一次。
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from typing import Optional, Tuple

# 路径常量（统一到 AstrBot 专用数据目录）
SCRIPTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(SCRIPTS_DIR, "..", "..", "..", ".."))
PLUGIN_DATA_DIR = os.path.join(ROOT_DIR, "data", "plugin_data", "qbot_nikkeinformation")
DATA_DIR = PLUGIN_DATA_DIR
TOP_CODES_PATH = os.path.join(SCRIPTS_DIR, "nikke_top_codes.py")
API_PATH = os.path.join(SCRIPTS_DIR, "nikke_api.py")


def ensure_dir(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def read_cookie_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        raise ValueError(f"Cookie 文件为空：{path}")
    # 规范化：去掉可能的 "Cookie:" 前缀，并标准化分号
    content = re.sub(r"(?i)^cookie:\s*", "", content).strip()
    content = "; ".join([seg.strip() for seg in content.split(";") if seg.strip()])
    return content


def parse_cookie_to_dict(cookie_str: str) -> dict:
    out = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def decode_intl_open_id_from_base64(openid_b64: str) -> Optional[str]:
    """将 URL 的 openid（Base64）解码为 '29080-XXXXXXXX...'，取连字符后的数字作为 intl_open_id。"""
    try:
        raw = base64.b64decode(openid_b64).decode("utf-8")
    except Exception:
        return None
    if "-" in raw:
        return raw.split("-", 1)[1]
    if raw.isdigit():
        return raw
    return None


def auto_page_url(openid_b64: str, type_: str = "combat") -> str:
    return f"https://www.blablalink.com/shiftyspad/nikke-list?type={type_}&openid={openid_b64}"


def run_subprocess(args_list, capture: bool) -> Tuple[int, str, str]:
    """运行子进程；capture=True 时捕获 stdout/stderr。返回 (returncode, stdout, stderr)。"""
    proc = subprocess.run(
        args_list,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        shell=False,
    )
    stdout = proc.stdout if capture and proc.stdout is not None else ""
    stderr = proc.stderr if capture and proc.stderr is not None else ""
    return proc.returncode, stdout, stderr


def parse_name_codes_from_stdout(stdout: str) -> Optional[str]:
    """从 nikke_top_codes.py 的打印行中提取 name_codes 参数串。"""
    m = re.search(r"用于\s+nikke_api\.py\s+的\s+--name-codes\s+参数值：([0-9,\s]+)", stdout)
    if m:
        return m.group(1).strip().replace(" ", "")
    return None


def find_latest_topcodes_json() -> Optional[str]:
    """在 storage 下找到最近的 *_TopCodes.json 文件。"""
    if not os.path.isdir(DATA_DIR):
        return None
    candidates = []
    for fn in os.listdir(DATA_DIR):
        if fn.endswith("_TopCodes.json"):
            full = os.path.join(DATA_DIR, fn)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                mtime = 0
            candidates.append((mtime, full))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def extract_codes_from_json(json_path: str) -> Optional[str]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return None
    val = obj.get("name_codes_arg")
    if isinstance(val, str) and val.strip():
        return val.strip()
    # 兜底：从 codes 数组拼接
    codes = obj.get("codes")
    if isinstance(codes, list) and codes:
        try:
            return ",".join(str(int(x)) for x in codes if x is not None)
        except Exception:
            return ",".join(str(x) for x in codes if x is not None)
    return None


def run_pipeline(intl_open_id: str, openid_b64: str, cookie_file: str, page_url: str, language: str, top_n: int, quiet: bool) -> Tuple[str, str, str, int]:
    """
    执行双副程序流水线：
    - 返回 (json_path, csv_path, latest_path, http_status)
    """
    # Step 1: 调用 nikke_top_codes.py，捕获输出，不在终端回显
    top_args = [
        sys.executable,
        TOP_CODES_PATH,
        "--intl-open-id",
        str(intl_open_id),
        "--openid-base64",
        str(openid_b64),
        "--page-url",
        str(page_url),
        "--cookie-file",
        str(cookie_file),
        "--top",
        str(top_n),
        "--language",
        str(language),
    ]
    rc, top_out, top_err = run_subprocess(top_args, capture=True)

    if not quiet:
        # 可选：将错误输出到调试日志，正常情况下不打印副程序输出
        if top_err.strip():
            print(f"[top_codes stderr] {top_err.strip()}", file=sys.stderr)

    if rc != 0:
        raise RuntimeError(f"获取前十 name_code 失败，exit={rc}；stderr={top_err}")

    # 从捕获的 stdout 中提取 name_codes 参数
    name_codes_arg = parse_name_codes_from_stdout(top_out)

    # 若 stdout 未提取到，尝试从最近的 TopCodes.json 读取
    if not name_codes_arg:
        json_path_latest = find_latest_topcodes_json()
        if json_path_latest:
            name_codes_arg = extract_codes_from_json(json_path_latest)

    if not name_codes_arg:
        raise RuntimeError("未能提取前十 name_code。请检查响应字段或该用户是否为空列表。")

    # Step 2: 调用 nikke_api.py 拉取详情；捕获输出以解析产物路径并最终回显摘要
    api_args = [
        sys.executable,
        API_PATH,
        "--intl-open-id",
        str(intl_open_id),
        "--name-codes",
        str(name_codes_arg),
        "--page-url",
        str(page_url),
        "--cookie-file",
        str(cookie_file),
        "--language",
        str(language),
    ]
    rc2, api_out, api_err = run_subprocess(api_args, capture=True)
    if rc2 != 0:
        raise RuntimeError(f"拉取详情失败，exit={rc2}；stderr={api_err}")

    # 解析产物路径与状态码
    m_json = re.search(r"已保存原始 JSON：([^\r\n]+)", api_out)
    m_csv = re.search(r"已保存扁平化 CSV：([^\r\n]+)", api_out)
    m_latest = re.search(r"最近一次响应写入：([^\r\n]+)", api_out)
    m_status = re.search(r"HTTP 状态码：(\d+)", api_out)

    json_path = m_json.group(1).strip() if m_json else ""
    csv_path = m_csv.group(1).strip() if m_csv else ""
    latest_path = m_latest.group(1).strip() if m_latest else ""
    http_status = int(m_status.group(1)) if m_status else 0

    # 最终摘要输出（仅此一步回显）
    print("已完成目标用户战力前十详情抓取")
    print(f"HTTP 状态码：{http_status}")
    print(f"JSON：{json_path}")
    print(f"CSV：{csv_path}")
    print(f"latest.json：{latest_path}")

    return json_path, csv_path, latest_path, http_status


def main() -> None:
    parser = argparse.ArgumentParser(description="Nikke 主程序：输入 openid → 自动取战力前十 → 拉取详情（插件内）")
    parser.add_argument("--openid-base64", required=True, help="URL 中的 openid（Base64）")
    parser.add_argument("--intl-open-id", default=None, help="国际 open_id（不提供则自动解码；解码失败再从 Cookie 兜底）")
    parser.add_argument("--type", default="combat", help="查询页 type（默认 combat，用于构造 page_url）")
    parser.add_argument("--top", type=int, default=10, help="取前 N 名（默认 10）")
    parser.add_argument("--language", default="en", help="language（默认 en）")
    parser.add_argument("--page-url", default=None, help="完整查询页 URL（不提供则自动构造）")
    parser.add_argument(
        "--cookie-file",
        default=os.path.join(PLUGIN_DATA_DIR, "cookie.txt"),
        help="Cookie 文件路径（默认：插件数据目录 cookie.txt）",
    )
    parser.add_argument("--quiet", action="store_true", help="静默模式：不打印副程序过程输出")
    args = parser.parse_args()

    ensure_dir(DATA_DIR)

    cookie_str = read_cookie_file(args.cookie_file)
    cookie_dict = parse_cookie_to_dict(cookie_str)

    intl_id = args.intl_open_id or decode_intl_open_id_from_base64(args.openid_base64) or cookie_dict.get("game_openid")
    if not intl_id:
        raise ValueError("无法确定 intl_open_id：请提供 --intl-open-id 或有效的 --openid-base64，或确保 Cookie 中包含 game_openid。")

    page_url = args.page_url or auto_page_url(args.openid_base64, args.type)

    run_pipeline(
        intl_open_id=str(intl_id),
        openid_b64=args.openid_base64,
        cookie_file=args.cookie_file,
        page_url=page_url,
        language=args.language,
        top_n=args.top,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()