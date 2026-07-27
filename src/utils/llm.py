"""LLM 请求扩展参数辅助 —— 按 provider 决定是否下发私有扩展字段。

DeepSeek v4 默认开启思考模式：① 要求历史 assistant 消息原样回传
reasoning_content（预处理注入的合成轨迹无该字段会 400）；② 每轮额外生成
reasoning token 拖慢响应。因此对 DeepSeek 统一禁用思考模式。

但 "thinking" 是 DeepSeek 私有扩展字段，通义/GLM 等其他 OpenAI 兼容服务
未必识别（严格校验的实现会直接 4xx）。按 OPENAI_BASE_URL 判断：仅 DeepSeek
下发该字段，其他 provider 保持纯 OpenAI 协议请求体。
"""
import os


def thinking_extra_body() -> dict:
    """返回适配当前 provider 的 extra_body（非 DeepSeek 返回空 dict）。"""
    base = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    if "deepseek" in base.lower():
        return {"thinking": {"type": "disabled"}}
    return {}
