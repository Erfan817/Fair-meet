"""FairMeet 配置读取。

原则：密钥仅从环境变量 / .env 读取，**绝不写死、绝不回显/打印**。
- 高德：AMAP_WEB_KEY（服务端 REST）；可选 AMAP_JS_KEY / AMAP_JS_SECURITY_CODE（前端 JS）。
- DeepSeek（真实 LLM）：DEEPSEEK_API_KEY，可选 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL。
- 其它行为开关：FAIRMEET_GEO（auto|fake|amap）、FAIRMEET_DB_PATH。
"""

import os

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports, reportUnknownVariableType]

load_dotenv()  # 加载 .env 文件


def get_amap_web_key() -> str:
    key = os.getenv("AMAP_WEB_KEY")
    if key is None or not key.strip():
        raise ValueError("AMAP_WEB_KEY 未找到，请查看 .env 文件")
    return key.strip()


def has_amap_web_key() -> bool:
    v = os.getenv("AMAP_WEB_KEY")
    return bool(v and v.strip())


def get_deepseek_api_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY")
    if key is None or not key.strip():
        raise ValueError("DEEPSEEK_API_KEY 未找到，请查看 .env 文件")
    return key.strip()


def has_deepseek_api_key() -> bool:
    v = os.getenv("DEEPSEEK_API_KEY")
    return bool(v and v.strip())


def get_deepseek_base_url() -> str:
    return os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")


def get_deepseek_model() -> str:
    return os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def get_geo_mode() -> str:
    """地理适配器选择：auto（有 AMAP_WEB_KEY 用真实高德，否则 Fake）/ fake / amap。"""
    return os.getenv("FAIRMEET_GEO", "auto").strip().lower()
