# ============================================================
# llm_client.py —— LLM 调用，配置来自 config_store
# ============================================================

import time, hashlib, logging, threading
from openai import OpenAI, APIError, APITimeoutError, RateLimitError
import config_store as cfg
from storage import cache_get, cache_set

logger = logging.getLogger("llm")

# ── 客户端复用 ─────────────────────────────────────────────

_client_cache: dict = {}   # {"base_url|api_key": OpenAI}
_client_lock = threading.Lock()
_last_call_time = 0.0
_rate_lock = threading.Lock()


def _get_client(base_url: str, api_key: str, timeout: int) -> OpenAI:
    """复用相同配置的 OpenAI client 实例，避免重复建连"""
    cache_key = f"{base_url}|{api_key}"
    with _client_lock:
        if cache_key not in _client_cache:
            _client_cache[cache_key] = OpenAI(
                api_key=api_key, base_url=base_url, timeout=timeout
            )
        client = _client_cache[cache_key]
        # 更新 timeout
        client.timeout = timeout
        return client


def _rate_limit(min_interval: float = 0.5):
    """简单速率限制，避免过快调用 API"""
    global _last_call_time
    with _rate_lock:
        elapsed = time.time() - _last_call_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _last_call_time = time.time()


def _hash(sys_p: str, user_p: str, model: str) -> str:
    return hashlib.md5(f"{sys_p}|||{user_p}|||{model}".encode()).hexdigest()


def chat(system_prompt: str, user_content: str,
         temperature: float = 0.3,
         use_cache: bool = True) -> str:
    """
    调用 LLM。先查缓存，再发请求，自动重试。
    配置全部从 config_store 实时读取，Web 页面修改后立即生效。
    """
    lc         = cfg.get_llm_config()
    model      = lc["model"]
    max_retries = lc["max_retries"]
    retry_delay = lc["retry_delay"]

    if not lc["api_key"]:
        raise ValueError("LLM API Key 未配置，请在设置页面填写")
    if not lc["base_url"]:
        raise ValueError("LLM Base URL 未配置")

    # 查缓存
    if use_cache:
        key = _hash(system_prompt, user_content, model)
        hit = cache_get(key)
        if hit is not None:
            return hit

    client = _get_client(lc["base_url"], lc["api_key"], lc["timeout"])
    _rate_limit()

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_content},
                ],
            )
            result = resp.choices[0].message.content.strip()
            if use_cache:
                key = _hash(system_prompt, user_content, model)
                cache_set(key, result, model)
            return result

        except (APITimeoutError, RateLimitError) as e:
            last_err = e
            wait = retry_delay * (2 ** attempt)
            logger.warning(f"LLM 重试 [{attempt+1}/{max_retries}]，等待 {wait}s：{e}")
            time.sleep(wait)

        except APIError as e:
            if 500 <= getattr(e, "status_code", 0) < 600:
                last_err = e
                time.sleep(retry_delay * (2 ** attempt))
            else:
                raise

        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                raise

    raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）：{last_err}")
