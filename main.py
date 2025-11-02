# -*- coding: utf-8 -*-
import os
import shutil
import re
import json
import base64
import asyncio
from typing import Optional, Tuple, Dict, Any, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
from .ai_reply import build_bind_system_prompt, build_info_system_prompt

# 存储路径（AstrBot 数据目录 data/plugin_data/qbot_nikkeinformation）
PLUGIN_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(PLUGIN_DIR, "..", "..", ".."))
PLUGIN_DATA_DIR = os.path.join(ROOT_DIR, "data", "plugin_data", "qbot_nikkeinformation")
STORAGE_DIR = PLUGIN_DATA_DIR
BINDINGS_PATH = os.path.join(STORAGE_DIR, "bindings.json")
# Cookie 默认路径：插件数据目录的 cookie.txt
COOKIE_FILE_DEFAULT = os.path.join(STORAGE_DIR, "cookie.txt")

# 脚本常量（插件内脚本作为后端）
RUNNER_SCRIPT = os.path.join(PLUGIN_DIR, "scripts", "nikke_runner.py")
# 工会战进度采集脚本
RUNNER_UNION_RAID_SCRIPT = os.path.join(PLUGIN_DIR, "scripts", "nikke_union_raid.py")
# 工会突袭成员出刀采集脚本
RUNNER_UNION_MEMBERS_SCRIPT = os.path.join(PLUGIN_DIR, "scripts", "nikke_union_members.py")
# 名称映射文件与抓取脚本
NAMES_MAP_PATH = os.path.join(STORAGE_DIR, "nikke_names_zh.json")
NAMES_FETCHER_SCRIPT = os.path.join(PLUGIN_DIR, "scripts", "nikke_names_fetcher.py")
# 工会成员映射文件（openid/nickname → 成员）
UNION_MEMBERS_MAP_PATH = os.path.join(STORAGE_DIR, "union_members_map.json")

# 迁移旧存储目录文件到当前存储目录（幂等）
def _migrate_storage_legacy() -> None:
    try:
        legacy_storage = os.path.join(PLUGIN_DIR, "storage")
        if os.path.isdir(legacy_storage):
            os.makedirs(STORAGE_DIR, exist_ok=True)
            for fn in os.listdir(legacy_storage):
                src = os.path.join(legacy_storage, fn)
                dst = os.path.join(STORAGE_DIR, fn)
                if not os.path.exists(dst):
                    try:
                        shutil.move(src, dst)
                    except Exception:
                        try:
                            shutil.copy2(src, dst)
                        except Exception:
                            pass
    except Exception:
        # 迁移失败不影响后续逻辑
        pass

# 迁移旧 cookie.txt 至插件数据目录（优先候选：项目根/.nikke_auth、插件内/.nikke_auth、工作目录/.nikke_auth）
def _migrate_cookie_legacy() -> None:
    try:
        new_cookie = os.path.join(STORAGE_DIR, "cookie.txt")
        if not os.path.isfile(new_cookie):
            candidates = [
                os.path.join(ROOT_DIR, ".nikke_auth", "cookie.txt"),
                os.path.join(PLUGIN_DIR, ".nikke_auth", "cookie.txt"),
                os.path.join(os.getcwd(), ".nikke_auth", "cookie.txt"),
            ]
            for src in candidates:
                if os.path.isfile(src):
                    os.makedirs(STORAGE_DIR, exist_ok=True)
                    try:
                        shutil.move(src, new_cookie)
                    except Exception:
                        try:
                            shutil.copy2(src, new_cookie)
                        except Exception:
                            pass
                    break
    except Exception:
        # 忽略 cookie 迁移失败
        pass

# 在模块加载时执行一次迁移（幂等）
_migrate_storage_legacy()
_migrate_cookie_legacy()


def _ensure_dir(p: str) -> None:
    if not os.path.isdir(p):
        os.makedirs(p, exist_ok=True)


def _load_bindings() -> Dict[str, Any]:
    _ensure_dir(STORAGE_DIR)
    if not os.path.isfile(BINDINGS_PATH):
        return {}
    try:
        with open(BINDINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_bindings(obj: Dict[str, Any]) -> None:
    _ensure_dir(STORAGE_DIR)
    with open(BINDINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def _load_names_map() -> Dict[str, str]:
    """
    读取中文名映射表 storage/nikke_names_zh.json；不存在则返回空。
    """
    try:
        with open(NAMES_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # 统一键为 str，值转为字符串展示
            return {str(k): str(v) for k, v in data.items() if v is not None}
    except Exception:
        pass
    return {}

def _decode_intl_open_id_from_b64(openid_b64: str) -> Optional[str]:
    """
    兼容无填充/URL-safe Base64 的 openid，自动补齐 '=' 并尝试 urlsafe 解码。
    解码后期望为 '29080-<intl_open_id>' 或直接为纯数字。
    """
    if not openid_b64:
        return None
    s = str(openid_b64).strip()
    # 去空白字符
    s = s.replace(" ", "").replace("\n", "").replace("\r", "")
    # 自动补齐 '=' 填充
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    raw = None
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(s).decode("utf-8")
            break
        except Exception:
            raw = None
    if not raw:
        return None
    if "-" in raw:
        return raw.split("-", 1)[1]
    if raw.isdigit():
        return raw
    return None


def _auto_page_url(openid_b64: str, type_: str = "combat") -> str:
    return f"https://www.blablalink.com/shiftyspad/nikke-list?type={type_}&openid={openid_b64}"

def _find_cookie_file() -> str:
    """
    尝试在多候选路径中寻找 Cookie 文件（按优先级）：
    1) 环境变量：NIKKE_COOKIE_FILE/NIKKE_COOKIE_PATH
    2) 插件数据目录：data/plugin_data/qbot_nikkeinformation/cookie.txt
    3) 兼容旧路径：项目根目录/.nikke_auth/cookie.txt、插件目录/.nikke_auth/cookie.txt、当前工作目录/.nikke_auth/cookie.txt
    """
    env1 = os.environ.get("NIKKE_COOKIE_FILE", "").strip()
    env2 = os.environ.get("NIKKE_COOKIE_PATH", "").strip()
    candidates = [
        env1 if env1 else "",
        env2 if env2 else "",
        os.path.join(STORAGE_DIR, "cookie.txt"),
        COOKIE_FILE_DEFAULT,
        os.path.join(ROOT_DIR, ".nikke_auth", "cookie.txt"),
        os.path.join(PLUGIN_DIR, ".nikke_auth", "cookie.txt"),
        os.path.join(".nikke_auth", "cookie.txt"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    # 默认返回插件数据目录 cookie.txt（即便不存在，以便提示明确路径）
    return os.path.join(STORAGE_DIR, "cookie.txt")

def _resolve_cookie_path() -> str:
    """
    解析 Cookie 文件路径优先级：
    1) 环境变量 NIKKE_COOKIE_PATH 指定路径（非空即采用）
    2) 插件数据目录 cookie.txt（data/plugin_data/qbot_nikkeinformation/cookie.txt）
    3) 兼容旧路径：项目根目录/.nikke_auth/cookie.txt → 插件目录/.nikke_auth/cookie.txt → 工作目录/.nikke_auth/cookie.txt
    """
    env_path = os.getenv("NIKKE_COOKIE_PATH", "").strip()
    if env_path:
        return env_path
    new_default = os.path.join(STORAGE_DIR, "cookie.txt")
    if os.path.isfile(new_default):
        return new_default
    legacy1 = os.path.join(ROOT_DIR, ".nikke_auth", "cookie.txt")
    if os.path.isfile(legacy1):
        return legacy1
    legacy2 = os.path.join(PLUGIN_DIR, ".nikke_auth", "cookie.txt")
    if os.path.isfile(legacy2):
        return legacy2
    return os.path.join(os.getcwd(), ".nikke_auth", "cookie.txt")


async def _run_runner(intl_open_id: str, openid_b64: str, cookie_file: str, type_: str = "combat", top_n: int = 10) -> Tuple[str, str, str, int, str]:
    """
    调用 scripts/nikke_runner.py，返回 (json_path, csv_path, latest_path, http_status, stdout)
    """
    page_url = _auto_page_url(openid_b64, type_)
    cmd = [
        os.sys.executable,
        RUNNER_SCRIPT,
        "--openid-base64", openid_b64,
        "--intl-open-id", str(intl_open_id),
        "--cookie-file", cookie_file,
        "--type", type_,
        "--top", str(top_n),
        "--quiet",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    out = (out_b or b"").decode("utf-8", errors="ignore")
    err = (err_b or b"").decode("utf-8", errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(f"后端执行失败(exit={proc.returncode}): {err or out}")

    # 解析 runner 摘要
    m_json = re.search(r"JSON：([^\r\n]+)", out)
    m_csv = re.search(r"CSV：([^\r\n]+)", out)
    m_latest = re.search(r"latest\.json：([^\r\n]+)", out)
    m_status = re.search(r"HTTP 状态码：(\d+)", out)

    json_path = m_json.group(1).strip() if m_json else ""
    csv_path = m_csv.group(1).strip() if m_csv else ""
    latest_path = m_latest.group(1).strip() if m_latest else ""
    http_status = int(m_status.group(1)) if m_status else 0
    return json_path, csv_path, latest_path, http_status, out


def _summarize_from_latest(latest_path: str) -> str:
    """
    从 latest.json 中提取前十，并为每个角色统计“装备属性”Top3：
    - 将同一角色四件装备的相同属性（以 function_type 识别）进行数值合并；
    - 梯度取 function_details.level（或从 id 的第 6、7 位提取），按梯度合计排序；
    - 输出该角色最大三项属性的合并数值（不是梯度），数值为 Percent 类型按两位小数的百分比展示。
    兼容字段名：
      - 角色列表：character_details / data.character_details / result.character_details
      - 状态/属性表：state_effects / data.state_effects / result.state_effects
    战力字段候选：power、combat、combat_power、score
    """
    if not latest_path or not os.path.isfile(latest_path):
        return "未找到最新详情文件。"
    try:
        with open(latest_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:
        return f"详情文件解析失败：{e}"

    # 载入名称映射（code → 中文名）
    names_map = _load_names_map()

    # 角色详情与状态效果
    details_candidates = [
        obj.get("character_details"),
        obj.get("data", {}).get("character_details") if isinstance(obj.get("data"), dict) else None,
        obj.get("result", {}).get("character_details") if isinstance(obj.get("result"), dict) else None,
    ]
    rows = None
    for lst in details_candidates:
        if isinstance(lst, list) and lst:
            rows = lst
            break
    if not rows:
        return "响应中未找到 character_details 列表。"

    eff_candidates = [
        obj.get("state_effects"),
        obj.get("data", {}).get("state_effects") if isinstance(obj.get("data"), dict) else None,
        obj.get("result", {}).get("state_effects") if isinstance(obj.get("result"), dict) else None,
    ]
    effects = None
    for lst in eff_candidates:
        if isinstance(lst, list):
            effects = lst
            break
    effects = effects or []

    # id -> function_details 列表
    effect_map: Dict[str, list] = {}
    for eff in effects:
        try:
            eff_id = str(eff.get("id"))
            fd = eff.get("function_details") or []
            if eff_id and isinstance(fd, list):
                effect_map[eff_id] = fd
        except Exception:
            continue

    TYPE_LABELS = {
        "StatAmmoLoad": "最大装弹数",
        "StatAtk": "攻击",
        "StatDef": "防御",
        "StatCritical": "暴击率",
        "StatCriticalDamage": "暴击伤害",
        "StatChargeTime": "蓄力速度",
        "StatChargeDamage": "蓄力伤害",
        "StatAccuracyCircle": "命中率",
        "IncElementDmg": "优越代码伤害",
    }
    # 部分词条在接口中 function_value_type 为 Integer，但实际应按百分比两位小数显示
    FORCE_PERCENT_TYPES = {"StatCriticalDamage", "StatCritical", "StatChargeDamage"}

    def pick_power(d: Dict[str, Any]) -> Optional[int]:
        for k in ["power", "combat", "combat_power", "score"]:
            v = d.get(k)
            if v is None:
                continue
            try:
                return int(float(str(v)))
            except Exception:
                continue
        return None

    def extract_grad(fd: Dict[str, Any]) -> int:
        """梯度：优先取 level，其次从 id 的第 6、7 位解析"""
        lvl = fd.get("level")
        if isinstance(lvl, int):
            return lvl
        _id = str(fd.get("id") or "")
        try:
            if len(_id) >= 7:
                return int(_id[5:7])
        except Exception:
            pass
        return 0

    def norm_value(fd: Dict[str, Any], force_percent: bool = False) -> float:
        """数值归一：
        - Percent：两位小数百分比 → 原值/100
        - 被强制为百分比的 Integer（如暴击伤害/暴击率/充能伤害）：按百分比两位小数 → 原值/100
        - 其他 Integer：直接返回数值
        """
        v = fd.get("function_value")
        t = fd.get("function_value_type")
        try:
            val = float(str(v))
        except Exception:
            return 0.0
        if force_percent or str(t).lower() == "percent":
            return val / 100.0
        return val

    def format_value(val: float, value_type: str) -> str:
        # 展示为正数：命中率/蓄力速度等负值按游戏显示为正处理
        val_abs = abs(val)
        if str(value_type).lower() == "percent":
            return f"{val_abs:.2f}%"
        # 整数/浮点按不带百分号展示
        if abs(val_abs - round(val_abs)) < 1e-6:
            return f"{int(round(val_abs))}"
        return f"{val_abs:.2f}"

    # 按战力从高到低排序
    rows_sorted = sorted(rows, key=lambda d: (pick_power(d) or 0), reverse=True)
    lines = []
    for idx, ch in enumerate(rows_sorted[:10], start=1):
        name = ch.get("name") or ch.get("nikke_name") or ch.get("display_name")
        code = ch.get("name_code") or ch.get("code") or ""
        # 若无名字，尝试通过映射用 code → 中文名
        if (not name) and code is not None:
            try:
                code_key = str(int(float(str(code))))
            except Exception:
                code_key = str(code)
            name = names_map.get(code_key, name)
        pw = pick_power(ch)
        show_name = name if name else (f"未知角色({code})" if code else "未知角色")
        title = f"{idx}. {show_name}  战力：{pw if pw is not None else '未知'}"
        lines.append(title)

        # 收集该角色四件装备的 option_id
        equip_ids: list[str] = []
        for part in ("head", "torso", "arm", "leg"):
            for i in (1, 2, 3):
                k = f"{part}_equip_option{i}_id"
                v = ch.get(k)
                try:
                    if v and int(v) != 0:
                        equip_ids.append(str(v))
                except Exception:
                    continue

        # 汇总属性：function_type -> {value_sum, grad_sum, last_value_type}
        agg: Dict[str, Dict[str, Any]] = {}
        for eid in equip_ids:
            fd_list = effect_map.get(eid) or []
            for fd in fd_list:
                ftype = fd.get("function_type") or "Unknown"
                vtype = fd.get("function_value_type") or ""
                force_pct = ftype in FORCE_PERCENT_TYPES
                val = norm_value(fd, force_percent=force_pct)
                if force_pct:
                    vtype = "Percent"
                grad = extract_grad(fd)
                cur = agg.setdefault(ftype, {"value_sum": 0.0, "grad_sum": 0, "value_type": vtype})
                cur["value_sum"] += val
                cur["grad_sum"] += grad
                # 以最后一次出现的类型为准（通常一致）
                cur["value_type"] = vtype or cur["value_type"]

        if not agg:
            lines.append("    词条：无")
            continue

        # 排序：梯度降序；若梯度相同，按数值降序
        sorted_items = sorted(
            agg.items(),
            key=lambda kv: (kv[1]["grad_sum"], kv[1]["value_sum"]),
            reverse=True,
        )
        top3 = sorted_items[:3]
        pretty = []
        for ftype, info in top3:
            label = TYPE_LABELS.get(ftype, ftype)
            pretty_val = format_value(float(info["value_sum"]), str(info["value_type"]))
            pretty.append(f"{label} :{pretty_val}")

        lines.append("    词条：" + ("； ".join(pretty) if pretty else "无"))

    return "\n".join(lines)


# 工会战进度抓取与渲染辅助

async def _run_union_raid(intl_open_id: str, openid_b64: str, cookie_file: str) -> Tuple[str, int, str]:
    """
    调用 scripts/nikke_union_raid.py，返回 (latest_path, http_status, stdout)
    """
    page_url = _auto_page_url(openid_b64 or "", "combat")
    cmd = [
        os.sys.executable,
        RUNNER_UNION_RAID_SCRIPT,
        "--intl-open-id", str(intl_open_id),
        "--page-url", page_url,
        "--cookie-file", cookie_file,
        "--language", "zh-TW",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out_b, err_b = await proc.communicate()
    out = (out_b or b"").decode("utf-8", errors="ignore")
    err = (err_b or b"").decode("utf-8", errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(f"工会战脚本执行失败(exit={proc.returncode}): {err or out}")

    m_latest = re.search(r"union_raid_latest\.json：([^\r\n]+)", out)
    m_status = re.search(r"HTTP 状态码：(\d+)", out)
    latest_path = m_latest.group(1).strip() if m_latest else ""
    http_status = int(m_status.group(1)) if m_status else 0
    return latest_path, http_status, out


def _fmt_int(v: int) -> str:
    try:
        return f"{int(v):,}"
    except Exception:
        try:
            return f"{int(float(str(v))):,}"
        except Exception:
            return str(v)


def _select_locale_name(d: Dict[str, Any], prefer: str = "zh-tw") -> Optional[str]:
    """
    从 name_localvalues/appearance_localvalues 等多语言字典选择展示名。
    """
    if not isinstance(d, dict):
        return None
    # 常见键标准化
    keys = {k.lower(): v for k, v in d.items()}
    for k in (prefer, "zh-cn", "zh", "en", "ja", "ko"):
        if k in keys and isinstance(keys[k], str) and keys[k].strip():
            return keys[k].strip()
    # 候选值中随便取一个非空字符串
    for v in d.values():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _extract_boss_entry(node: Dict[str, Any], prefer_lang: str = "zh-tw") -> Optional[Dict[str, Any]]:
    """
    从“Boss 条目”或“兼容的关卡条目”中提取 {name, current_hp, max_hp, percent}
    - 兼容两类结构：
      1) 直接是 Boss 项：包含 current_hp/max_hp/name_localvalues/appearance_localvalues
      2) 关卡项（不含 current_hp/max_hp），这种将在上层展开 boss_info 列表，不在此处理
    """
    if not isinstance(node, dict):
        return None

    # 直接在当前 node 上取血量（Boss 项）
    cur = node.get("current_hp")
    mx = node.get("max_hp")

    # 名称优先从自身的本地化字段取
    name_dict = None
    for cand in (
        node.get("name_localvalues"),
        node.get("appearance_localvalues"),
    ):
        if isinstance(cand, dict):
            name_dict = cand
            break
    name = _select_locale_name(name_dict or {}, prefer_lang) or node.get("boss_name") or node.get("name")

    # 兜底：尝试把字符串数字转为整数
    def to_int(x):
        try:
            return int(str(x))
        except Exception:
            try:
                return int(float(str(x)))
            except Exception:
                return 0

    cur_i = to_int(cur)
    mx_i = to_int(mx)
    if mx_i <= 0:
        return None
    pct = max(0, min(100, int(round(cur_i * 100.0 / mx_i))))  # 当前血量百分比

    return {
        "name": name or "未知Boss",
        "current": cur_i,
        "max": mx_i,
        "percent": pct,
    }


def _parse_union_raid(latest_path: str, prefer_lang: str = "zh-tw") -> Tuple[list, str]:
    """
    解析 union_raid_latest.json，返回 (items, err_msg)
    items: [{name, percent, current_fmt, max_fmt}]
    - 兼容结构：
      data.level_info[*].boss_info[list of boss]  ← 实测结构
      以及极少数情况下 level_info[*] 直接带 current_hp/max_hp 的结构
    """
    if not latest_path or not os.path.isfile(latest_path):
        return [], "未找到工会战最新文件。"
    try:
        with open(latest_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:
        return [], f"工会战详情解析失败：{e}"

    # 兼容多层 data/result
    candidates = [
        obj.get("level_info"),
        obj.get("data", {}).get("level_info") if isinstance(obj.get("data"), dict) else None,
        obj.get("result", {}).get("level_info") if isinstance(obj.get("result"), dict) else None,
    ]
    levels = None
    for lst in candidates:
        if isinstance(lst, list) and lst:
            levels = lst
            break
    if not levels:
        return [], "响应中未找到 level_info 列表。"

    items = []
    for lv in levels:
        # 优先处理 boss_info 列表（常见结构）
        boss_list = lv.get("boss_info")
        if isinstance(boss_list, list) and boss_list:
            for boss in boss_list:
                entry = _extract_boss_entry(boss, prefer_lang=prefer_lang)
                if not entry:
                    continue
                items.append({
                    "name": entry["name"],
                    "percent": entry["percent"],
                    "current_fmt": _fmt_int(entry["current"]),
                    "max_fmt": _fmt_int(entry["max"]),
                })
            continue

        # 兜底：关卡条目自身携带 hp 的非常规结构
        entry = _extract_boss_entry(lv, prefer_lang=prefer_lang)
        if entry:
            items.append({
                "name": entry["name"],
                "percent": entry["percent"],
                "current_fmt": _fmt_int(entry["current"]),
                "max_fmt": _fmt_int(entry["max"]),
            })

    if not items:
        return [], "未解析到任何 Boss 进度项。"

    # 保留原顺序；如需突出危急度可按 percent 升序
    return items, ""

def _build_union_raid_template() -> str:
    """
    构造用于渲染工会战进度的 HTML 模板（Jinja2）。
    """
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>NIKKE 工会战进度</title>
<style>
  html, body { margin: 0; padding: 0; background:#fff; width: 100%; height: 100vh; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC","Microsoft YaHei", sans-serif; display: flex; align-items: center; justify-content: flex-start; }
  #root { display: block; width: 100%; margin: 0; padding: 0; }
  .card { width: 100vw; box-sizing: border-box; padding: 16px 18px; border-radius: 14px; background: #ffffff; box-shadow: none; margin: 0; }
  .row { display: flex; flex-direction: column; gap: 10px; margin: 8px 0; }
  .row:last-child { margin-bottom: 0; }
  .name { font-size: 18px; font-weight: 600; color: #333; }
  .bar { position: relative; height: 16px; background: #eee; border-radius: 8px; overflow: hidden; width: 100%; }
  .bar-inner { position: absolute; left: 0; top: 0; height: 100%;
               background: linear-gradient(90deg, #F97316 0%, #FB923C 100%); }
  .meta { display: flex; justify-content: space-between; font-size: 12px; color: #666; }
  .meta .hp { font-size: 10px; white-space: nowrap; }
  .percent { color: #F97316; font-weight: 700; }
  .boss-icon { width: 18px; height: 18px; display:inline-block; border-radius:4px; margin-right:8px; background:#6B7280; vertical-align: -3px;}
  .title { font-size: 20px; font-weight:700; color:#111827; margin-bottom: 8px; }
</style>
</head>
<body>
  <div id="root">
    <div class="card">
      <div class="title">工会战 Boss 进度</div>
      {% for item in items %}
      <div class="row">
        <div class="name"><span class="boss-icon"></span>{{ item.name }}</div>
        <div class="bar">
          <div class="bar-inner" style="width: {{ item.percent }}%;"></div>
        </div>
        <div class="meta">
          <div class="percent">{{ item.percent }}%</div>
          <div class="hp">{{ item.current_fmt }} / {{ item.max_fmt }}</div>
        </div>
      </div>
      {% endfor %}
    </div>
  </div>
</body>
</html>
"""

# 工会突袭成员出刀数据抓取与聚合

async def _run_union_raid_members(intl_open_id: str, openid_b64: str, cookie_file: str) -> Tuple[str, int, str]:
    """
    调用 scripts/nikke_union_members.py，返回 (latest_path, http_status, stdout)
    """
    page_url = _auto_page_url(openid_b64 or "", "combat")
    cmd = [
        os.sys.executable,
        RUNNER_UNION_MEMBERS_SCRIPT,
        "--intl-open-id", str(intl_open_id),
        "--page-url", page_url,
        "--cookie-file", cookie_file,
        "--language", "zh-TW",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out_b, err_b = await proc.communicate()
    out = (out_b or b"").decode("utf-8", errors="ignore")
    err = (err_b or b"").decode("utf-8", errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(f"工会成员脚本执行失败(exit={proc.returncode}): {err or out}")

    m_latest = re.search(r"union_raid_members_latest\.json：([^\r\n]+)", out)
    m_status = re.search(r"HTTP 状态码：(\d+)", out)
    latest_path = m_latest.group(1).strip() if m_latest else ""
    http_status = int(m_status.group(1)) if m_status else 0
    return latest_path, http_status, out


def _read_union_members_map() -> List[Dict[str, str]]:
    """
    读取 storage/union_members_map.json
    文件结构（建议）：
    {
      "version": 1,
      "max_slots": 32,
      "items": [
        {"openid": "123...", "nickname": "某某", "member": "某某"},
        {"openid": "", "nickname": "", "member": ""}
      ]
    }
    若不存在则返回空列表。
    """
    try:
        with open(UNION_MEMBERS_MAP_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        items = obj.get("items")
        if isinstance(items, list):
            out = []
            for it in items:
                if isinstance(it, dict):
                    out.append({
                        "openid": str(it.get("openid") or ""),
                        "nickname": str(it.get("nickname") or ""),
                        "member": str(it.get("member") or ""),
                    })
            return out
    except Exception:
        pass
    return []


def _write_union_members_map(items: List[Dict[str, str]], max_slots: int = 32) -> None:
    """
    初始化写入映射；仅当文件不存在时调用。
    """
    _ensure_dir(STORAGE_DIR)
    data = {
        "version": 1,
        "max_slots": int(max_slots),
        "items": items[:max_slots] + [{"openid": "", "nickname": "", "member": ""} for _ in range(max(0, max_slots - len(items)))],
    }
    with open(UNION_MEMBERS_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _init_union_members_map_if_missing(observed: List[Dict[str, str]], max_slots: int = 32) -> List[Dict[str, str]]:
    """
    若映射文件不存在：以当前观测的成员初始化（member 默认等于 nickname），不足 32 以空位补齐。
    若已存在：只读取并返回，不覆盖。
    """
    if os.path.isfile(UNION_MEMBERS_MAP_PATH):
        return _read_union_members_map()

    uniq: Dict[str, Dict[str, str]] = {}
    for it in observed:
        openid = str(it.get("openid") or "")
        nickname = str(it.get("nickname") or "")
        key = openid or nickname
        if not key:
            continue
        if key in uniq:
            continue
        uniq[key] = {"openid": openid, "nickname": nickname, "member": nickname or ""}

    items = list(uniq.values())
    _write_union_members_map(items, max_slots=max_slots)
    return items


def _lookup_member_name(map_items: List[Dict[str, str]], openid: str, nickname: str) -> str:
    """
    优先按 openid 命中映射；其次按 nickname。
    若命中项的 member 为空，则回退为 nickname。
    """
    openid = str(openid or "")
    nickname = str(nickname or "")
    for it in map_items:
        if openid and it.get("openid") == openid:
            mem = it.get("member") or ""
            return mem if mem else (nickname if nickname else openid)
    for it in map_items:
        if nickname and it.get("nickname") == nickname:
            mem = it.get("member") or ""
            return mem if mem else nickname
    return nickname or openid or "未知成员"


def _int_or_zero(x: Any) -> int:
    try:
        s = str(x)
        if not s:
            return 0
        if "." in s:
            return int(float(s))
        return int(s)
    except Exception:
        return 0


def _format_damage_b(v: int) -> str:
    """
    以 B（十亿）单位显示，保留三位小数。例如：56757000000 → 56.757B
    """
    try:
        val = float(v)
    except Exception:
        return str(v)
    return f"{val / 1_000_000_000:.3f}B"


def _parse_union_raid_members(latest_path: str) -> Tuple[List[Dict[str, Any]], str]:
    """
    解析 union_raid_members_latest.json
    仅统计 day == 1 的出刀记录。
    返回 (rows, err)：
      rows: [{openid, nickname, attempts, total_damage}]
    聚合规则：
      - 优先解析 participate_data[*] 中 day == 1 的条目：
          * 若存在 squad(list)，累加每一刀 total_damage
          * 否则回退 total_damage/damage/score 字段
      - 若不存在 participate_data，则回退通用深度遍历，但仅在 day 上下文为 1 时计入
      - 同成员（按 openid，否则按 nickname）进行合并：attempts 为刀数，total_damage 为总和
    """
    if not latest_path or not os.path.isfile(latest_path):
        return [], "未找到成员出刀最新文件。"
    try:
        with open(latest_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception as e:
        return [], f"成员出刀详情解析失败：{e}"

    def to_int(x: Any, default: int = -1) -> int:
        try:
            s = str(x).strip()
            if s == "":
                return default
            if "." in s:
                return int(float(s))
            return int(s)
        except Exception:
            return default

    agg: Dict[str, Dict[str, Any]] = {}

    # 方式一：优先解析 participate_data[*] day==1
    pd_candidates = [
        obj.get("participate_data"),
        obj.get("data", {}).get("participate_data") if isinstance(obj.get("data"), dict) else None,
        obj.get("result", {}).get("participate_data") if isinstance(obj.get("result"), dict) else None,
    ]
    pd_list = None
    for lst in pd_candidates:
        if isinstance(lst, list) and lst:
            pd_list = lst
            break

    def add_damage(openid: str, nickname: str, damages: List[int]) -> None:
        if not damages:
            return
        key = str(openid or nickname)
        cur = agg.setdefault(key, {
            "openid": str(openid or ""),
            "nickname": str(nickname or ""),
            "attempts": 0,
            "total_damage": 0
        })
        cur["attempts"] += len(damages)
        cur["total_damage"] += sum(damages)

    if pd_list is not None:
        for ent in pd_list:
            if not isinstance(ent, dict):
                continue
            day_val = to_int(ent.get("day"), default=-1)
            if day_val != 1:
                continue  # 只要 day1
            nickname = ent.get("nickname") or ent.get("nick_name") or ""
            openid = ent.get("openid") or ent.get("open_id") or ent.get("intl_open_id") or ""

            damages: List[int] = []
            squad = ent.get("squad")
            if isinstance(squad, list):
                for it in squad:
                    if isinstance(it, dict):
                        d = _int_or_zero(it.get("total_damage") or it.get("damage") or it.get("total") or it.get("score"))
                        if d > 0:
                            damages.append(d)
            if not damages:
                d0 = _int_or_zero(ent.get("total_damage") or ent.get("damage") or ent.get("total") or ent.get("score"))
                if d0 > 0:
                    damages.append(d0)

            add_damage(openid, nickname, damages)

    # 方式二：回退深度遍历，传递 day 上下文，仅统计 day==1
    if not agg:
        def collect_node(node: Dict[str, Any], day_ctx: Optional[int]) -> None:
            if day_ctx != 1:
                return
            nickname = node.get("nickname") or node.get("nick_name") or ""
            openid = node.get("openid") or node.get("open_id") or node.get("intl_open_id") or ""
            if not (nickname or openid):
                return
            damages: List[int] = []
            squad = node.get("squad")
            if isinstance(squad, list):
                for it in squad:
                    if isinstance(it, dict):
                        d = _int_or_zero(it.get("total_damage") or it.get("damage") or it.get("total") or it.get("score"))
                        if d > 0:
                            damages.append(d)
            if not damages:
                d0 = _int_or_zero(node.get("total_damage") or node.get("damage") or node.get("total") or node.get("score"))
                if d0 > 0:
                    damages.append(d0)
            add_damage(openid, nickname, damages)

        def walk(o: Any, day_ctx: Optional[int] = None) -> None:
            if isinstance(o, dict):
                # 更新 day 上下文
                day_here = o.get("day")
                ctx = to_int(day_here, default=day_ctx if day_ctx is not None else -1)
                collect_node(o, ctx)
                for v in o.values():
                    walk(v, ctx)
            elif isinstance(o, list):
                for it in o:
                    walk(it, day_ctx)

        walk(obj.get("data") or obj.get("result") or obj)

    rows = list(agg.values())
    if not rows:
        return [], "未解析到任何成员出刀项（或 day1 数据为空）。"

    # 成功解析到 rows：按总伤害降序返回
    rows.sort(key=lambda x: int(x.get("total_damage") or 0), reverse=True)
    return rows, ""
def _build_union_members_remaining_template() -> str:
    """
    构造用于渲染“未出刀/未出满三刀”列表的 HTML 表格模板（Jinja2）。
    表头（简体）：No. | 成员 | 已出刀 | 剩余次数
    """
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>NIKKE 联盟突袭未出刀/未出满</title>
<style>
  html, body { margin:0; padding:0; background:#fff; width:100%; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC","Microsoft YaHei", sans-serif; display:block; }
  .card { width:100%; max-width:100%; box-sizing:border-box; padding:16px 18px; border-radius:14px; background:#ffffff; box-shadow:none; margin:0; }
  .title { font-size:20px; font-weight:700; color:#111827; margin-bottom: 8px; }
  .subtitle { font-size:13px; color:#6B7280; margin-bottom: 12px; }
  table { width:100%; border-collapse: collapse; font-size:14px; }
  th, td { border-bottom: 1px solid #eee; padding:8px 10px; text-align:left; }
  th { color:#374151; font-weight:600; background:#f9fafb; }
  .no { width:56px; }
  .member { min-width:160px; }
  .attempts { width:100px; }
  .remain { width:100px; font-weight:600; color:#B91C1C; }
</style>
</head>
<body>
  <div class="card">
    <div class="title">联盟突袭未出刀/未出满</div>
    <table>
      <thead>
        <tr>
          <th class="no">No.</th>
          <th class="member">成员</th>
          <th class="attempts">已出刀</th>
          <th class="remain">剩余次数</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td class="no">{{ loop.index }}</td>
          <td class="member">{{ r.member }}</td>
          <td class="attempts">{{ r.attempts }}</td>
          <td class="remain">{{ r.remaining }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</body>
</html>
"""


def _compute_unfilled(map_items: List[Dict[str, str]], rows: List[Dict[str, Any]], max_required: int = 3) -> List[Dict[str, Any]]:
    """
    基于“现有映射”与当天(day1)的出刀聚合结果 rows，计算
    - 未出刀（attempts == 0）
    - 未出满三刀（0 < attempts < max_required）
    返回展示列表：[{member, attempts, remaining}]
    规则：
      - 仅统计映射中非空项（至少 openid/nickname/member 有一个非空）
      - 匹配顺序：openid 优先；若无 openid 则按 nickname 匹配
      - 成员显示名：优先映射 member，其次 nickname，最后 openid
    """
    # 构建观测索引
    by_openid: Dict[str, Dict[str, Any]] = {}
    by_nick: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        oid = str(r.get("openid") or "").strip()
        nick = str(r.get("nickname") or "").strip()
        if oid:
            by_openid[oid] = r
        if nick:
            by_nick[nick] = r

    out: List[Dict[str, Any]] = []
    for it in map_items:
        if not isinstance(it, dict):
            continue
        openid = str(it.get("openid") or "").strip()
        nickname = str(it.get("nickname") or "").strip()
        member = str(it.get("member") or "").strip()
        # 忽略完全空位
        if not (openid or nickname or member):
            continue

        attempts = 0
        ref = None
        if openid and openid in by_openid:
            ref = by_openid[openid]
        elif nickname and nickname in by_nick:
            ref = by_nick[nickname]
        if ref:
            try:
                attempts = int(ref.get("attempts") or 0)
            except Exception:
                attempts = 0

        remaining = max(0, max_required - attempts)
        # 仅纳入 attempts < max_required 的成员
        if attempts < max_required:
            out.append({
                "member": member or nickname or openid or "未知成员",
                "attempts": attempts,
                "remaining": remaining
            })

    # 排序：已出刀次数升序；同次数按成员名拼音/字典序
    out.sort(key=lambda x: (int(x["attempts"]), str(x["member"])))
    return out


def _build_union_members_template() -> str:
    """
    构造用于渲染“成员出刀统计”的 HTML 表格模板（Jinja2）。
    第一行使用简体中文，列为：No. | 成员 | 参与次数 | 总伤害
    成员列使用映射中的“member”字段。
    """
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>NIKKE 联盟突袭出刀情况</title>
<style>
  html, body { margin:0; padding:0; background:#fff; width:100%; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC","Microsoft YaHei", sans-serif; display:block; }
  .card { width:100%; max-width:100%; box-sizing:border-box; padding:16px 18px; border-radius:14px; background:#ffffff; box-shadow:none; margin:0; }
  .title { font-size:20px; font-weight:700; color:#111827; margin-bottom: 8px; }
  table { width:100%; border-collapse: collapse; font-size:14px; }
  th, td { border-bottom: 1px solid #eee; padding:8px 10px; text-align:left; }
  th { color:#374151; font-weight:600; background:#f9fafb; }
  .no { width:56px; }
  .member { min-width:160px; }
  .attempts { width:100px; }
  .total { width:140px; font-weight:600; color:#111827; }
</style>
</head>
<body>
  <div class="card">
    <div class="title">联盟突袭出刀情况</div>
    <table>
      <thead>
        <tr>
          <th class="no">No.</th>
          <th class="member">成员</th>
          <th class="attempts">参与次数</th>
          <th class="total">总伤害</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td class="no">{{ loop.index }}</td>
          <td class="member">{{ r.member }}</td>
          <td class="attempts">{{ r.attempts }}</td>
          <td class="total">{{ r.total_fmt }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</body>
</html>
"""
@register("nikkeinformation", "Apex", "NIKKE 信息查询（绑定 openid 后一键查询前十详情）", "1.1.0", "https://example.com/repo")
class NikkePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 仅在本插件触发的 AI 请求上进行 system_prompt 注入的标记：session_id -> injection_text
        self._ai_pending: Dict[str, str] = {}

    @filter.command_group("nikke", alias={"妮姬", "nikkeinfo"})
    def nikke(self):
        """NIKKE 查询指令组。使用 /nikke bind <openid> 绑定，/nikke info 查询绑定账户的战力前十详情。"""
        pass

    @nikke.command("bind", alias={"绑定"})
    async def bind(self, event: AstrMessageEvent, openid: str):
        """
        绑定用户 openid（Base64 或 '29080-XXXXXXXX' 或纯数字 intl_open_id）。
        绑定后使用 /nikke info 即可查询。
        """
        # 解析 openid
        openid_b64 = None
        intl_open_id = None
        if "-" in openid or openid.isdigit():
            # 可能是 '29080-XXXXXXXX' 或纯数字
            if "-" in openid:
                intl_open_id = openid.split("-", 1)[1]
            else:
                intl_open_id = openid
        else:
            openid_b64 = openid.strip()
            intl_open_id = _decode_intl_open_id_from_b64(openid_b64)
            if not intl_open_id:
                yield event.plain_result("无法从 Base64 openid 解码 intl_open_id，请检查输入。")
                return

        # 保存绑定
        bindings = _load_bindings()
        key = f"{event.get_platform_name()}:{event.get_sender_id()}"
        bindings[key] = {
            "openid_base64": openid_b64,
            "intl_open_id": str(intl_open_id),
            "type": "combat",
        }
        _save_bindings(bindings)
        # 单一回复策略：启用 AI 时，不再发送公式回复，只发送一次 AI 回复；若 AI 失败则回退到公式回复
        ai_cfg = (self.config or {}).get("ai_settings", {})
        if ai_cfg.get("enable_ai_for_bind", False):
            try:
                inj = build_bind_system_prompt(self.config, str(intl_open_id), str(openid_b64 or ""))
                self._ai_pending[event.session_id] = inj

                # 获取/创建当前会话的 Conversation
                conv = None
                try:
                    curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                    if curr_cid:
                        conv = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                except Exception:
                    conv = None
                if conv is None:
                    try:
                        cid = await self.context.conversation_manager.new_conversation(
                            event.unified_msg_origin, event.get_platform_id()
                        )
                        conv = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, cid)
                    except Exception:
                        conv = None

                # 仅触发一次 LLM 请求（system_prompt 注入在 on_llm_request 钩子中执行，且在人格提示词之后）
                yield event.request_llm(
                    prompt=str(ai_cfg.get("bind_prompt", "用户已完成 NIKKE 账户绑定，请进行确认并提示下一步操作。")),
                    conversation=conv,
                    session_id=event.session_id,
                    func_tool_manager=self.context.get_llm_tool_manager(),
                )
            except Exception as e:
                logger.error(f"bind AI 回复失败：{e}")
                # 回退为原始公式回复
                yield event.plain_result(f"已绑定：intl_open_id={intl_open_id}；后续使用 /nikke info 即可查询。")
        else:
            # 未启用 AI：使用原有公式回复
            yield event.plain_result(f"已绑定：intl_open_id={intl_open_id}；后续使用 /nikke info 即可查询。")

    @nikke.command("info", alias={"查询"})
    async def info(self, event: AstrMessageEvent):
        """
        查询已绑定账户的战力前十详情。
        要求：插件数据目录 cookie.txt 存在且为有效登录态。
        """
        # 读取绑定
        bindings = _load_bindings()
        key = f"{event.get_platform_name()}:{event.get_sender_id()}"
        bind = bindings.get(key)
        if not bind:
            yield event.plain_result("尚未绑定。请先使用 /nikke bind <openid> 完成绑定。")
            return

        openid_b64 = bind.get("openid_base64")
        intl_open_id = bind.get("intl_open_id")
        type_ = bind.get("type") or "combat"

        # 若未保存 openid_base64，则根据 intl_open_id 构造一个 Base64("29080-<intl_open_id>")
        if not openid_b64 and intl_open_id:
            try:
                openid_b64 = base64.b64encode(f"29080-{intl_open_id}".encode("utf-8")).decode("ascii")
            except Exception:
                openid_b64 = ""

        # 校验 Cookie
        cookie_file = _resolve_cookie_path()
        if not os.path.isfile(cookie_file):
            yield event.plain_result(f"未找到 Cookie 文件：{cookie_file}。请将登录态写入该路径后再试。")
            return

        # 执行后端
        try:
            json_path, csv_path, latest_path, status, runner_out = await _run_runner(
                intl_open_id=str(intl_open_id),
                openid_b64=str(openid_b64),
                cookie_file=cookie_file,
                type_=type_,
                top_n=10,
            )
        except Exception as e:
            logger.error(f"运行失败：{e}")
            yield event.plain_result(f"查询失败：{e}")
            return

        summary = _summarize_from_latest(latest_path)
        page_url = _auto_page_url(openid_b64 or "", type_)
        text = (
            f"已为您整理战力前十详情\n"
            f"{summary}"
        )
        # 单一回复策略：启用 AI 时不再发送公式回复，只发送一次 AI 回复；若 AI 失败则回退到公式回复
        ai_cfg = (self.config or {}).get("ai_settings", {})
        if ai_cfg.get("enable_ai_for_info", False):
            try:
                inj = build_info_system_prompt(self.config, summary)
                self._ai_pending[event.session_id] = inj

                # 获取/创建当前会话的 Conversation
                conv = None
                try:
                    curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                    if curr_cid:
                        conv = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                except Exception:
                    conv = None
                if conv is None:
                    try:
                        cid = await self.context.conversation_manager.new_conversation(
                            event.unified_msg_origin, event.get_platform_id()
                        )
                        conv = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, cid)
                    except Exception:
                        conv = None

                # 仅触发一次 LLM 请求（提示词插入在人格设定提示词之后；摘要位于可编辑提示词之后）
                yield event.request_llm(
                    prompt=str(ai_cfg.get("info_prompt", "请基于系统中追加的战力前十详情摘要进行分析与建议回复。")),
                    conversation=conv,
                    session_id=event.session_id,
                    func_tool_manager=self.context.get_llm_tool_manager(),
                )
            except Exception as e:
                logger.error(f"info AI 回复失败：{e}")
                # 回退为原始公式回复
                yield event.plain_result(text)
        else:
            # 未启用 AI：使用原有公式回复
            yield event.plain_result(text)

    @nikke.command("unionraid", alias={"工会战", "raid", "会战"})
    async def unionraid(self, event: AstrMessageEvent):
        """
        工会战 Boss 进度图（当前血量、最大血量、百分比）。
        要求：Cookie 有效。需先 /nikke bind 绑定 openid。
        """
        # 读取绑定
        bindings = _load_bindings()
        key = f"{event.get_platform_name()}:{event.get_sender_id()}"
        bind = bindings.get(key)
        if not bind:
            yield event.plain_result("尚未绑定。请先使用 /nikke bind <openid> 完成绑定。")
            return

        openid_b64 = bind.get("openid_base64")
        intl_open_id = bind.get("intl_open_id")

        # 若未保存 openid_base64，则根据 intl_open_id 构造一个 Base64("29080-<intl_open_id>")
        if not openid_b64 and intl_open_id:
            try:
                openid_b64 = base64.b64encode(f"29080-{intl_open_id}".encode("utf-8")).decode("ascii")
            except Exception:
                openid_b64 = ""

        # 校验 Cookie
        cookie_file = _resolve_cookie_path()
        if not os.path.isfile(cookie_file):
            yield event.plain_result(f"未找到 Cookie 文件：{cookie_file}。请将登录态写入该路径后再试。")
            return

        # 执行工会战后端
        try:
            latest_path, status, runner_out = await _run_union_raid(
                intl_open_id=str(intl_open_id),
                openid_b64=str(openid_b64 or ""),
                cookie_file=cookie_file,
            )
        except Exception as e:
            logger.error(f"工会战运行失败：{e}")
            yield event.plain_result(f"工会战查询失败：{e}")
            return

        items, err = _parse_union_raid(latest_path, prefer_lang="zh-tw")
        if err:
            yield event.plain_result(err)
            return
        if not items:
            yield event.plain_result("未解析到任何 Boss 进度项。")
            return

        # 渲染进度图
        try:
            tmpl = _build_union_raid_template()
            img_url = await self.html_render(
                tmpl,
                {"items": items},
                return_url=True,
                options={"type": "jpeg", "full_page": False}
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"工会战进度图渲染失败：{e}")
            lines = []
            for it in items:
                lines.append(f"{it['name']}: {it['percent']}%  {it['current_fmt']} / {it['max_fmt']}")
            yield event.plain_result("工会战 Boss 进度：\n" + "\n".join(lines))

    @nikke.command("unionraid_members", alias={"出刀", "刀表", "出刀情况", "members"})
    async def unionraid_members(self, event: AstrMessageEvent):
        """
        联盟突袭成员出刀情况表（合并总伤害、按总伤害降序，首行简体中文）。
        流程：
        1) 直连接口获取包含 nickname、openid、单刀 total_damage 的原始数据；
        2) 聚合同一成员的总伤害与参与次数；
        3) 读取本地映射 storage/union_members_map.json，将“成员”列用映射的 member 字段展示。
           - 首次运行若映射不存在：以现有观测成员初始化（member 默认=nickname），补齐至 32 个占位；
           - 后续仅读取该文件，不会覆盖；你可自行编辑完善该文件。
        """
        # 读取绑定
        bindings = _load_bindings()
        key = f"{event.get_platform_name()}:{event.get_sender_id()}"
        bind = bindings.get(key)
        if not bind:
            yield event.plain_result("尚未绑定。请先使用 /nikke bind <openid> 完成绑定。")
            return

        openid_b64 = bind.get("openid_base64")
        intl_open_id = bind.get("intl_open_id")

        # 若未保存 openid_base64，则根据 intl_open_id 构造一个 Base64("29080-<intl_open_id>")
        if not openid_b64 and intl_open_id:
            try:
                openid_b64 = base64.b64encode(f"29080-{intl_open_id}".encode("utf-8")).decode("ascii")
            except Exception:
                openid_b64 = ""

        # 校验 Cookie
        cookie_file = _resolve_cookie_path()
        if not os.path.isfile(cookie_file):
            yield event.plain_result(f"未找到 Cookie 文件：{cookie_file}。请将登录态写入该路径后再试。")
            return

        # 执行后端：抓取成员出刀数据
        try:
            latest_path, status, runner_out = await _run_union_raid_members(
                intl_open_id=str(intl_open_id),
                openid_b64=str(openid_b64 or ""),
                cookie_file=cookie_file,
            )
        except Exception as e:
            logger.error(f"工会成员出刀运行失败：{e}")
            yield event.plain_result(f"出刀情况查询失败：{e}")
            return

        # 解析与聚合
        rows, err = _parse_union_raid_members(latest_path)
        if err:
            yield event.plain_result(err)
            return
        if not rows:
            yield event.plain_result("未解析到任何成员出刀项。")
            return

        # 首次运行：若映射文件不存在则按当前观测初始化；之后仅读取
        observed = [{"openid": r.get("openid", ""), "nickname": r.get("nickname", "")} for r in rows]
        map_items = _init_union_members_map_if_missing(observed, max_slots=32)

        # 组装展示行：成员名来自映射中的 member 字段
        disp = []
        for r in rows:
            member = _lookup_member_name(map_items, r.get("openid", ""), r.get("nickname", ""))
            attempts = int(r.get("attempts") or 0)
            total_damage = int(r.get("total_damage") or 0)
            disp.append({
                "member": member,
                "attempts": attempts,
                "total_damage": total_damage,
                "total_fmt": _format_damage_b(total_damage),
            })
        # 按总伤害降序
        disp.sort(key=lambda x: int(x["total_damage"]), reverse=True)

        # 渲染表格（首行简体）
        try:
            tmpl = _build_union_members_template()
            img_url = await self.html_render(
                tmpl,
                {"rows": disp},
                return_url=True,
                options={"type": "jpeg", "full_page": True}
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"成员出刀表渲染失败：{e}")
            # 纯文本回退
            lines = ["No. | 成员 | 参与次数 | 总伤害"]
            for i, d in enumerate(disp, start=1):
                lines.append(f"{i} | {d['member']} | {d['attempts']} | {d['total_fmt']}")
            yield event.plain_result("联盟突袭出刀情况：\n" + "\n".join(lines))

    @nikke.command("unionraid_missing", alias={"未出刀", "未满", "不满三刀", "缺刀", "missing", "remain"})
    async def unionraid_missing(self, event: AstrMessageEvent):
        """
        联盟突袭未出刀/未出满三刀清单（依据现有映射；仅统计 day1）。
        """
        # 读取绑定
        bindings = _load_bindings()
        key = f"{event.get_platform_name()}:{event.get_sender_id()}"
        bind = bindings.get(key)
        if not bind:
            yield event.plain_result("尚未绑定。请先使用 /nikke bind <openid> 完成绑定。")
            return

        openid_b64 = bind.get("openid_base64")
        intl_open_id = bind.get("intl_open_id")

        # 若未保存 openid_base64，则根据 intl_open_id 构造一个 Base64("29080-<intl_open_id>")
        if not openid_b64 and intl_open_id:
            try:
                openid_b64 = base64.b64encode(f"29080-{intl_open_id}".encode("utf-8")).decode("ascii")
            except Exception:
                openid_b64 = ""

        # 校验 Cookie
        cookie_file = _resolve_cookie_path()
        if not os.path.isfile(cookie_file):
            yield event.plain_result(f"未找到 Cookie 文件：{cookie_file}。请将登录态写入该路径后再试。")
            return

        # 拉取 day1 出刀聚合
        try:
            latest_path, status, runner_out = await _run_union_raid_members(
                intl_open_id=str(intl_open_id),
                openid_b64=str(openid_b64 or ""),
                cookie_file=cookie_file,
            )
        except Exception as e:
            logger.error(f"工会成员出刀运行失败：{e}")
            yield event.plain_result(f"未出刀清单查询失败：{e}")
            return

        rows, err = _parse_union_raid_members(latest_path)
        if err:
            yield event.plain_result(err)
            return

        # 读取映射；若不存在则用观测构建一次
        map_items = _read_union_members_map()
        if not map_items:
            observed = [{"openid": r.get("openid", ""), "nickname": r.get("nickname", "")} for r in rows]
            map_items = _init_union_members_map_if_missing(observed, max_slots=32)

        unfilled = _compute_unfilled(map_items, rows, max_required=3)
        if not unfilled:
            yield event.plain_result("全员已出满三刀。")
            return

        # 渲染列表
        try:
            tmpl = _build_union_members_remaining_template()
            img_url = await self.html_render(
                tmpl,
                {"rows": unfilled},
                return_url=True,
                options={"type": "jpeg", "full_page": True}
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"未出刀清单渲染失败：{e}")
            lines = ["No. | 成员 | 已出刀 | 剩余次数"]
            for i, d in enumerate(unfilled, start=1):
                lines.append(f"{i} | {d['member']} | {d['attempts']} | {d['remaining']}")
            yield event.plain_result("未出刀/未出满清单：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @nikke.command("cookie", alias={"设置cookie"})
    async def set_cookie_hint(self, event: AstrMessageEvent):
        """查看或设置 Cookie 路径提示（管理员专用；优先环境变量 NIKKE_COOKIE_PATH）。"""
        resolved = _resolve_cookie_path()
        exists = os.path.isfile(resolved)
        msg = (
            f"Cookie 文件首选：{resolved}\n"
            f"状态：{'已存在' if exists else '未找到'}\n"
            f"可通过环境变量 NIKKE_COOKIE_PATH 指定绝对路径；"
            f"默认使用插件数据目录 {os.path.join(STORAGE_DIR, 'cookie.txt')}（首次运行会从旧路径自动迁移）。"
        )
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @nikke.command("update_namelist", alias={"更新namelist", "更新映射", "更新名字", "更新名称"})
    async def update_namelist(self, event: AstrMessageEvent):
        """
        一键更新 NIKKE 名称映射（code→中文名）（管理员专用）。
        无需参数，使用插件配置 names_updater.sources，若未配置则使用内置默认源。
        结果写入 storage/nikke_names_zh.json。
        """
        try:
            ns_cfg = {}
            try:
                ns_cfg = (self.config or {}).get("names_updater", {}) or {}
            except Exception:
                ns_cfg = {}

            sources = []
            if isinstance(ns_cfg, dict):
                v = ns_cfg.get("sources", [])
                if isinstance(v, list):
                    sources = [str(x).strip() for x in v if str(x).strip()]
            if not sources:
                sources = [
                    "https://sg-tools-cdn.blablalink.com/vm-36/bj-70/6223a9fbfd3be53b48587c934a91f686.json"
                ]

            language = "zh-CN"
            if isinstance(ns_cfg, dict):
                language = str(ns_cfg.get("language", "zh-CN") or "zh-CN")

            _ensure_dir(STORAGE_DIR)

            cmd = [
                os.sys.executable,
                NAMES_FETCHER_SCRIPT,
                "--language", language,
                "--out", NAMES_MAP_PATH,
            ]
            for u in sources:
                cmd.extend(["--url", u])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await proc.communicate()
            out = (out_b or b"").decode("utf-8", errors="ignore")
            err = (err_b or b"").decode("utf-8", errors="ignore")

            if proc.returncode != 0:
                raise RuntimeError(err or out or "抓取脚本执行失败")

            m_stats = re.search(r"来源文件数：(\d+)，新增映射数：(\d+)，当前总映射键数：(\d+)", out)
            stats_text = ""
            if m_stats:
                stats_text = f"来源：{m_stats.group(1)}；新增：{m_stats.group(2)}；总键数：{m_stats.group(3)}"
            else:
                stats_text = "抓取完成。"

            yield event.plain_result(
                f"已更新名称映射（写入：{NAMES_MAP_PATH}）\n语言：{language}\n{stats_text}"
            )
        except Exception as e:
            logger.error(f"update_namelist 失败：{e}")
            yield event.plain_result(f"更新映射失败：{e}")

    @filter.on_llm_request(priority=-10)
    async def ai_on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在请求 LLM 前注入 AI 提示词，插入位置：人格设定提示词之后。
        仅处理本插件标记过的请求（通过 self._ai_pending 标记）。
        """
        try:
            inj = self._ai_pending.pop(event.session_id, None)
            if inj:
                base = req.system_prompt or ""
                req.system_prompt = f"{base}\n{inj}"
        except Exception as e:
            logger.error(f"Nikke 插件 on_llm_request 失败：{e}")

    async def terminate(self):
        """插件卸载时的清理（当前无需处理）。"""

