import base64
import time
from typing import Tuple, List, Optional
from pathlib import Path

from requests_html import HTMLSession

# Reuse dataclass and parsers from scraper.py
try:
    # Prefer package-relative import when running via astrbot plugin
    from .scraper import NikkeItem, parse_top10, format_items_text, build_url
except ImportError:
    # Fallback to local absolute import when running as a standalone script
    from scraper import NikkeItem, parse_top10, format_items_text, build_url


CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def get_nikke_top10_js(openid_b64: str) -> Tuple[List[NikkeItem], Optional[Path], Optional[str]]:
    """
    使用 requests_html 渲染 SPA 页面后解析前十。
    - openid_b64: Base64 编码的 openid（示例中提供的形式）
    返回 (items, cache_path_if_any, error_message_if_any)
    """
    try:
        openid = base64.b64decode(openid_b64).decode()
    except Exception as e:
        return ([], None, f"OpenID 解码失败: {e}")

    url = build_url(openid, "combat")

    session = HTMLSession()
    try:
        resp = session.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        }, timeout=30)
    except Exception as e:
        return ([], None, f"页面请求失败: {e}")

    # 渲染 JS（requests_html 内部将调用 pyppeteer）
    try:
        # sleep 让前端数据加载完成；超时可适当增大
        resp.html.render(timeout=40, sleep=2)
    except Exception as e:
        return ([], None, f"页面渲染失败: {e}")

    # 渲染后的 HTML
    html = resp.html.html or ""
    cache_path = CACHE_DIR / f"nikke_rendered_{openid_b64.replace('=', '_')}.html"
    try:
        cache_path.write_text(html, encoding="utf-8")
    except Exception:
        cache_path = None

    try:
        items = parse_top10(html)
        return (items, cache_path, None)
    except Exception as e:
        return ([], cache_path, f"页面解析失败: {e}")


__all__ = [
    "get_nikke_top10_js",
    "format_items_text",
]