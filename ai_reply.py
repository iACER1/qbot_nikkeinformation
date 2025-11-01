# -*- coding: utf-8 -*-
"""
AI 回复辅助模块

职责：
- 从插件配置中读取可编辑提示词
- 生成将被插入到 LLM system_prompt 中的片段
- 按要求确保 info 的摘要在可编辑提示词之后

插入位置说明（由主插件在 on_llm_request 钩子中保证顺序）：
- 人格设定 system_prompt
- 本模块返回的可编辑提示词
- （仅 info）战力前十详情的摘要文本
"""

from __future__ import annotations

from typing import Dict


def _get_ai_settings(config: Dict | None) -> Dict:
    if not isinstance(config, dict):
        return {}
    return config.get("ai_settings", {}) or {}


def _normalize_text(s: str) -> str:
    s = (s or "").strip()
    # 简单清理：避免出现多余空行
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    return s


def build_bind_system_prompt(config: Dict | None, intl_open_id: str, openid_b64: str) -> str:
    """
    生成 /nikke bind 指令的 system_prompt 注入片段。
    - 使用配置中的 bind_prompt；若缺省，使用内置默认值
    - 不直接泄露 openid 或 cookie 等敏感信息
    """
    ai_cfg = _get_ai_settings(config)
    editable_prompt = ai_cfg.get("bind_prompt") or (
        "你是 NIKKE 游戏的情报助理。用户刚刚完成账户绑定，请以简洁友好的语气进行确认，并提示下一步操作：例如告知他可使用 /nikke info 获取战力前十详情；如需Cookie或公开设置，也请简要提醒。禁止输出路径或URL。"
    )

    editable_prompt = _normalize_text(editable_prompt)

    # 可添加少量上下文供 AI 使用（非敏感）
    ctx_lines = [
        "当前事件：用户完成 NIKKE 账户绑定。",
        "请只输出与确认和下一步引导有关的内容，避免无关信息。",
    ]

    result = editable_prompt
    result += "\n\n" + "\n".join(ctx_lines)
    return result


def build_info_system_prompt(config: Dict | None, summary: str) -> str:
    """
    生成 /nikke info 指令的 system_prompt 注入片段。
    - 先插入可编辑提示词（info_prompt）
    - 再紧随其后插入“战力前十详情摘要”文本
    """
    ai_cfg = _get_ai_settings(config)
    editable_prompt = ai_cfg.get("info_prompt") or (
        "你是 NIKKE 装备与练度分析助手。基于后续提供的“战力前十详情摘要”，请用中文给出清晰的结构化点评与建议："
        "1) 账号整体强弱与风格；2) 针对每个角色的Top3词条给出一句优化建议；3) 指出明显问题的优先改进方向。"
        "输出简洁，避免无关信息。"
    )
    editable_prompt = _normalize_text(editable_prompt)

    summary = _normalize_text(summary or "暂无摘要。")

    # 组织为“提示词 + 摘要”顺序，确保摘要在可编辑提示词之后
    result = editable_prompt
    result += "\n\n【战力前十详情摘要】\n"
    result += summary
    return result