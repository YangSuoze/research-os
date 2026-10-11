import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from openai import (
    AsyncOpenAI,
    OpenAI,
)
from pydantic import BaseModel

from paper_import_mcp.config import settings

# --- 类型定义 ---
MessageList = list[dict[str, Any]]  # content 也可以是视觉输入的多模态 parts


def _json_mode_options(
    json_mode: bool | type[BaseModel], request_kwargs: dict | None
) -> tuple[type[BaseModel] | None, dict[str, Any]]:
    """True 使用 JSON 对象模式；Pydantic 类使用 SDK 的严格 Schema 模式。"""

    kwargs = dict(request_kwargs or {})
    if json_mode is False:
        return None, kwargs
    if json_mode is True:
        kwargs.setdefault("response_format", {"type": "json_object"})
        return None, kwargs
    if isinstance(json_mode, type) and issubclass(json_mode, BaseModel):
        if "response_format" in kwargs:
            raise ValueError(
                "request_kwargs.response_format cannot be combined with a "
                "Pydantic json_mode schema"
            )
        return json_mode, kwargs
    raise TypeError("json_mode must be bool or a Pydantic BaseModel subclass")


def _has_multimodal_content(messages: MessageList) -> bool:
    return any(isinstance(message.get("content"), list) for message in messages)


def _completion_content(response: Any) -> str:
    content = response.choices[0].message.content
    if not isinstance(content, str):
        raise ValueError("LLM did not return text content")
    return content


def _validated_json(content: str, schema: type[BaseModel]) -> str:
    """多模态降级为 json_object 时，仍在本地校验模型返回值。"""

    return schema.model_validate_json(content).model_dump_json()


def _validated_json_object(content: str) -> str:
    """防止兼容服务忽略 json_object 约束后返回非 JSON 文本。"""

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("LLM JSON response must be an object")
    return content


class LLMClient:
    """
    一个精简且高性能的大模型请求客户端，基于官方 openai 库重构。
    专为同步/异步查询和获取Embeddings设计，并针对FastAPI等异步框架进行了优化。
    现已支持 Qwen 和 vLLM 的 JSON Mode/Schema 功能，以及本地视觉模型。
    """

    def __init__(self):
        if not settings.llm_api_key:
            raise ValueError("Set BACKEND_LLM_API_KEY before running LLM extraction")
        self.logger = logging.getLogger("LLMClient")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

        # 重试策略配置
        self.max_retries = 3
        self.retry_delay = 1  # 秒
        self.sync_client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=120,
            max_retries=2,
        )
        self._async_client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=120,
            max_retries=2,
        )

    async def close(self) -> None:
        """关闭同步和异步 HTTP 连接。"""
        try:
            self.sync_client.close()
        finally:
            await self._async_client.close()

    def query(
        self,
        query: str | MessageList,
        model_name: str = "qwen3.8-flash",
        temperature: float = 1.0,
        json_mode: bool | type[BaseModel] = False,
        request_kwargs: dict | None = None,
    ) -> str:
        """返回文本；True 请求 JSON 对象，Pydantic 类请求严格结构化 JSON。"""

        try:
            if isinstance(query, str):
                messages = [{"role": "user", "content": query}]
            else:
                messages = query
            schema, kwargs = _json_mode_options(json_mode, request_kwargs)
            if schema is not None and not _has_multimodal_content(messages):
                response = self.sync_client.chat.completions.parse(
                    model=model_name,
                    messages=messages,
                    temperature=temperature,
                    response_format=schema,
                    **kwargs,
                )
                parsed = response.choices[0].message.parsed
                if parsed is None:
                    raise ValueError("LLM did not return a valid structured response")
                return parsed.model_dump_json()
            if schema is not None:
                kwargs["response_format"] = {"type": "json_object"}
            response = self.sync_client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                **kwargs,
            )
            content = _completion_content(response)
            if schema is not None:
                return _validated_json(content, schema)
            return _validated_json_object(content) if json_mode is True else content
        except Exception as e:
            self.logger.error(f"Non-retryable error in query: {e}")
            raise

    async def aquery(
        self,
        query: str | MessageList,
        model_name: str = settings.model_name,
        temperature: float = 1.0,
        json_mode: bool | type[BaseModel] = False,
        request_kwargs: dict | None = None,
    ) -> str:
        """异步版本；返回值保持为 JSON 字符串而非 Pydantic 实例。"""

        try:
            if isinstance(query, str):
                messages = [{"role": "user", "content": query}]
            else:
                messages = query
            schema, kwargs = _json_mode_options(json_mode, request_kwargs)
            if schema is not None and not _has_multimodal_content(messages):
                response = await self._async_client.chat.completions.parse(
                    model=model_name,
                    messages=messages,
                    temperature=temperature,
                    response_format=schema,
                    **kwargs,
                )
                parsed = response.choices[0].message.parsed
                if parsed is None:
                    raise ValueError("LLM did not return a valid structured response")
                return parsed.model_dump_json()
            if schema is not None:
                kwargs["response_format"] = {"type": "json_object"}
            response = await self._async_client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                **kwargs,
            )
            content = _completion_content(response)
            if schema is not None:
                return _validated_json(content, schema)
            return _validated_json_object(content) if json_mode is True else content
        except Exception as e:
            self.logger.error(f"Non-retryable error in aquery: {e}")
            raise

    async def aquery_stream(
        self,
        query: str | MessageList,
        model_name: str = "deepseek-v3",
        temperature: float = 1.0,
        deployment: str = "ali",
        is_preprocess_token: bool = False,
        system_message: str | None = None,
        enable_thinking: bool = False,
        json_mode: bool | type[BaseModel] = False,
        image_url: str | None = None,  # <-- 新增参数
        is_preprocess_image_token: bool = False,
    ) -> AsyncGenerator[str, None]:
        last_error = None
        for attempt in range(self.max_retries):
            try:
                api_config = self._get_api_config(deployment)
                client = self._get_async_client(
                    api_config["api_base"], api_config["api_key"]
                )

                # 准备消息，传入 image_url
                messages = self._prepare_messages(query, system_message, image_url)

                request_kwargs = self._build_request_kwargs(
                    deployment, model_name, messages, enable_thinking, json_mode
                )

                response = await client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    stream=True,
                    temperature=temperature,
                    **request_kwargs,
                )
                tmp_cache = {
                    "reference_flag": "",
                    "before_toekn_text": "",
                    "before_seg": "",
                    # --- 用于图表解析的状态 ---
                    "chart_buffer": "",
                    "in_chart": False,
                }
                reasoning_start_flag = True
                async for chunk in response:
                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        # self.logger.info(f"raw_token: {delta}")
                        if hasattr(delta, "reasoning_content"):
                            if reasoning_start_flag:
                                reasoning_start_flag = False
                                yield {"type": "reasoning", "token": "\n > "}
                            reasoning_token = delta.reasoning_content or ""

                            if "\n" in reasoning_token:
                                # 换行符添加 '> '
                                reasoning_token = reasoning_token.replace("\n", "\n> ")
                            if is_preprocess_token:
                                reasoning_token = self._preprocess_token(
                                    reasoning_token, tmp_cache
                                )
                            if reasoning_token:
                                yield {"token": reasoning_token, "type": "reasoning"}
                        if hasattr(delta, "content"):
                            token = delta.content or ""

                            if is_preprocess_token:
                                token = self._preprocess_token(token, tmp_cache)
                            # --- 处理图表 tzchart 解析 (New) ---
                            if is_preprocess_image_token:
                                token = self._preprocess_img_token(token, tmp_cache)
                                if "<start_pic>" in token:
                                    token = token.replace("<start_pic>", "")
                                    yield {"token": token, "type": "content"}
                                    yield {"token": "pic_start", "type": "pic_start"}
                                elif "<end_pic>" in token:
                                    # <pic_end>前面的内容作为pic_json，后面的内容作为普通token
                                    pic_json_str = token.split("<end_pic>")[0]
                                    token = token.split("<end_pic>")[1]
                                    pic_json = self._parse_tzchart_data(pic_json_str)
                                    yield {
                                        "token": pic_json,
                                        "type": "pic_json",
                                    }
                                    yield {
                                        "token": token,
                                        "type": "content",
                                    }
                                elif token:
                                    yield {"token": token, "type": "content"}
                            else:
                                if token:
                                    yield {"token": token, "type": "content"}
                return
            except self.retryable_errors as e:
                last_error = e
                wait_time = self.retry_delay * (2**attempt)
                self.logger.warning(
                    f"Async attempt {attempt + 1}/{self.max_retries} failed. Retrying in {wait_time}s. Error: {e}"
                )
                await asyncio.sleep(wait_time)
            except Exception as e:
                self.logger.error(f"Non-retryable error in aquery: {e}")
                raise
        raise (
            last_error
            if last_error
            else Exception("Unknown error in aquery after max retries")
        )
