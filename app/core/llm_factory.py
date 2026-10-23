"""LLM 工厂类

使用 LangChain ChatOpenAI 通过 OpenAI 兼容模式调用阿里云 DashScope
这种方式便于后续切换到其他支持 OpenAI API 的模型提供商

支持的模型提供商（只需修改 base_url 和 api_key）：
- 阿里云 DashScope: https://dashscope.aliyuncs.com/compatible-mode/v1
- OpenAI: https://api.openai.com/v1
- Azure OpenAI: https://{resource}.openai.azure.com
- 其他兼容 OpenAI API 的服务
"""

from langchain_openai import ChatOpenAI

from app.config import config
from app.core.data_safety import ModelInputRejected, model_input_bytes, redact_sensitive
from app.core.model_protection import ModelObserver


class SafeChatOpenAI(ChatOpenAI):
    """Keep the boundary in the transport path, including tool/structured calls."""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # Never rewrite tool JSON Schemas: only actual conversational data.
        for field in ("messages", "input"):
            if field in payload:
                payload[field] = redact_sensitive(payload[field])
        if model_input_bytes(payload) > config.model_max_input_bytes:
            raise ModelInputRejected("模型输入超过安全上限，请缩小问题或证据范围")
        return payload


class LLMFactory:
    """LLM 工厂类 - 使用 OpenAI 兼容模式"""

    # 阿里云 DashScope OpenAI 兼容模式 URL
    DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    @staticmethod
    def create_chat_model(
        model: str | None = None,
        temperature: float = 0.7,
        streaming: bool = True,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> ChatOpenAI:
        model = model or config.dashscope_model
        base_url = base_url or LLMFactory.DASHSCOPE_BASE_URL
        api_key = api_key or config.dashscope_api_key

        return SafeChatOpenAI(
            model=model,
            temperature=temperature,
            streaming=streaming,
            base_url=base_url,
            api_key=api_key,
            timeout=config.llm_request_timeout,
            max_retries=0,
            stream_usage=True,
            callbacks=[ModelObserver()],
        )


# 全局 LLM 工厂实例
llm_factory = LLMFactory()
