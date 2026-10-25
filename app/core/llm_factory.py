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
from pydantic import model_validator

from app.config import config
from app.core.data_safety import ModelInputRejected, model_input_bytes, redact_sensitive
from app.core.model_protection import ModelObserver


class SafeChatOpenAI(ChatOpenAI):
    """Keep the boundary in the transport path, including tool/structured calls."""

    @model_validator(mode="before")
    @classmethod
    def disable_hidden_retries(cls, values):
        # One quota reservation represents one actual provider attempt.
        if isinstance(values, dict):
            values = {**values, "max_retries": 0}
        return values

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # Never rewrite tool JSON Schemas: only actual conversational data.
        for field in ("messages", "input"):
            if field in payload:
                payload[field] = redact_sensitive(payload[field])
        if model_input_bytes(payload) > config.model_max_input_bytes:
            raise ModelInputRejected("模型输入超过安全上限，请缩小问题或证据范围")
        return payload

    async def _agenerate_with_cache(self, messages, stop=None, run_manager=None, **kwargs):
        if not config.shared_model_quota_enabled:
            return await super()._agenerate_with_cache(messages, stop=stop, run_manager=run_manager, **kwargs)
        from app.services.shared_model_quota import shared_transport

        size = model_input_bytes(self._get_request_payload(messages, stop=stop, **kwargs))
        async with shared_transport(self, size):
            return await super()._agenerate_with_cache(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def astream(self, input, config=None, *, stop=None, **kwargs):
        from app.config import config as settings

        if not settings.shared_model_quota_enabled:
            async for chunk in super().astream(input, config=config, stop=stop, **kwargs):
                yield chunk
            return
        from app.services.shared_model_quota import shared_transport

        messages = self._convert_input(input).to_messages()
        size = model_input_bytes(self._get_request_payload(messages, stop=stop, **{**kwargs, "stream": True}))
        async with shared_transport(self, size):
            iterator = super().astream(input, config=config, stop=stop, **kwargs)
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                await iterator.aclose()

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if not config.shared_model_quota_enabled:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        from app.services.shared_model_quota import shared_transport

        size = model_input_bytes(self._get_request_payload(messages, stop=stop, **kwargs))
        async with shared_transport(self, size):
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        if not config.shared_model_quota_enabled:
            async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
                yield chunk
            return
        from app.services.shared_model_quota import shared_transport, track_provider_stream

        size = model_input_bytes(self._get_request_payload(messages, stop=stop, **{**kwargs, "stream": True}))
        async with shared_transport(self, size):
            iterator = super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs)
            track_provider_stream(iterator)
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                await iterator.aclose()

    def _generate(self, *args, **kwargs):
        if config.shared_model_quota_enabled:
            raise RuntimeError("共享模型额度已启用，请使用异步 async 模型调用")
        return super()._generate(*args, **kwargs)

    def _stream(self, *args, **kwargs):
        if config.shared_model_quota_enabled:
            raise RuntimeError("共享模型额度已启用，请使用异步 async 模型调用")
        yield from super()._stream(*args, **kwargs)


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
