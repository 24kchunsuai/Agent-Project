"""DeepSeek LLM 客户端（OpenAI 兼容协议）。

设计要点：
- key / base_url / model 全部来自 .env（app.core.config），禁止硬编码；
- chat() 同步整段返回，chat_stream() 逐 delta 产出（SSE）；
- 异常归一化为 LLMError，由上层转成 3000 段业务码，绝不把堆栈抛给 Java/前端；
- 网络类错误做有限重试（默认 1 次），429/5xx 同样适用；
- P2 failover（容错规约 §6.2，G8）：主 provider 持续故障 → 冷却（默认 300s）
  并切备用 provider；冷却期内跳过、到期自然半开探测。未配置备用时行为与
  单 provider 时代完全一致。chat_stream 只在首个 token 前 failover（半截回复
  无法重放），首 token 后的断流由上层 error 事件兜底。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 调用失败（配置缺失 / 网络 / 上游错误），message 可直接展示给用户。"""


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: str  # JSON 字符串，由调用方解析


@dataclass
class ChatResult:
    content: str
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)  # 非空表示模型要求调工具


@dataclass(frozen=True)
class Provider:
    """链上的一个 LLM provider（OpenAI 兼容端点）。"""

    name: str
    base_url: str
    api_key: str
    model: str


class LLMClient:
    """薄封装：只负责"发消息、拿回复"，provider 链与冷却归本类管。"""

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        # provider 名 → 冷却截止（epoch 秒）；冷却期内跳过，到期自然半开
        self._cooldown_until: dict[str, float] = {}

    # ---- provider 链 ----

    def _chain(self) -> list[Provider]:
        primary = Provider(
            name="primary",
            base_url=settings.llm_base_url or "https://api.deepseek.com",
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
        chain = [primary]
        if (
            settings.llm_backup_base_url
            and settings.llm_backup_api_key
            and settings.llm_backup_model
        ):
            chain.append(Provider(
                name="backup",
                base_url=settings.llm_backup_base_url,
                api_key=settings.llm_backup_api_key,
                model=settings.llm_backup_model,
            ))
        return chain

    def _pick(self, chain: list[Provider]) -> list[Provider]:
        """冷却期内跳过（规约 §6.2）；全部冷却中取最快到期的一个半开探测。"""
        now = time.time()
        available = [p for p in chain if self._cooldown_until.get(p.name, 0) <= now]
        if not available:
            available = [min(chain, key=lambda p: self._cooldown_until.get(p.name, 0))]
        return available

    def _cooldown(self, provider: Provider, reason: str) -> None:
        until = time.time() + settings.llm_cooldown_seconds
        self._cooldown_until[provider.name] = until
        logger.error(
            "LLM provider「%s」进入冷却 %ds（原因：%s）", provider.name,
            settings.llm_cooldown_seconds, reason,
        )

    async def _http_for(self, provider: Provider) -> httpx.AsyncClient:
        client = self._clients.get(provider.name)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                base_url=provider.base_url,
                timeout=httpx.Timeout(settings.llm_timeout, connect=10),
            )
            self._clients[provider.name] = client
        return client

    async def close(self) -> None:
        for client in self._clients.values():
            if not client.is_closed:
                await client.aclose()
        self._clients.clear()

    # ---- 对话补全 ----

    async def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        """对话补全。messages 为 [{"role": ..., "content": ...}, ...]。

        传入 tools 时启用 function calling：模型要求调工具时，
        ChatResult.tool_calls 非空（content 可能为空字符串），
        调用方执行工具后以 role=tool 消息回填再发起下一轮。

        failover：按链序逐个 provider 尝试（每个内部保留原有 429/5xx/网络重试），
        一个 provider 重试耗尽即冷却并切下一个；全部失败抛 LLMError。
        """
        if not settings.llm_api_key:
            raise LLMError("大模型未配置，请在 .env 中设置 LLM_API_KEY")

        chain = self._pick(self._chain())
        last_error: Exception | None = None
        for idx, provider in enumerate(chain):
            try:
                return await self._chat_one(provider, messages, temperature, max_tokens, tools)
            except LLMError as exc:
                last_error = exc
                self._cooldown(provider, str(exc))
                if idx < len(chain) - 1:
                    # 切备：留痕一条 failover 事件（降级必须计数，规约 §8）
                    try:
                        from app.services.telemetry import PATH_LLM_FAILOVER, telemetry
                        telemetry.record_fault(
                            PATH_LLM_FAILOVER,
                            detail=f"本应使用 {provider.model}，实际切换备用 provider"
                                   f"（原因：{exc}）",
                        )
                    except Exception:  # 埋点失败不影响主链路
                        pass
                    logger.warning("LLM provider「%s」失败，切换备用: %s", provider.name, exc)
        logger.error("LLM 调用最终失败（链上 %d 个 provider 全部耗尽）", len(chain))
        raise LLMError("大模型暂时不可用，请稍后重试") from last_error

    async def _chat_one(
        self,
        provider: Provider,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
    ) -> ChatResult:
        """单 provider 调用（含原有 429/5xx/网络类重试，语义不变）。"""
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {provider.api_key}"}

        last_error: Exception | None = None
        for attempt in range(3):  # 最多 2 次重试（429/5xx/网络类）
            try:
                http = await self._http_for(provider)
                resp = await http.post(
                    "/chat/completions", json=payload, headers=headers
                )
                if resp.status_code == 401:
                    raise LLMError("大模型鉴权失败，请检查 API_KEY 配置")
                if resp.status_code == 429:
                    # 限流可重试：退避后重发（客户端并发高时偶发 429）
                    last_error = LLMError("大模型限流中，请稍后重试")
                    logger.warning("LLM 限流(第 %d 次)，退避重试", attempt + 1)
                    if attempt < 2:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    break
                resp.raise_for_status()
                data = resp.json()
                message = data["choices"][0]["message"]
                content = message.get("content") or ""
                tool_calls = [
                    ToolCall(
                        id=tc["id"],
                        name=tc["function"]["name"],
                        arguments=tc["function"].get("arguments") or "{}",
                    )
                    for tc in (message.get("tool_calls") or [])
                ]
                if not content and not tool_calls:
                    raise LLMError("大模型返回了空回复")
                return ChatResult(
                    content=content,
                    model=data.get("model", provider.model),
                    usage=data.get("usage") or {},
                    tool_calls=tool_calls,
                )
            except LLMError:
                raise  # 业务语义明确的错误不重试
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("LLM 网络异常(第 %d 次): %s", attempt + 1, exc)
            except httpx.HTTPStatusError as exc:
                # 5xx 可重试，其余直接失败
                if 500 <= exc.response.status_code < 600 and attempt < 2:
                    last_error = exc
                    logger.warning("LLM 上游 %d，准备重试", exc.response.status_code)
                else:
                    last_error = exc
                    break
            if attempt < 2:
                await asyncio.sleep(1)

        logger.error("LLM 调用最终失败（provider=%s）: %s", provider.name, last_error)
        raise LLMError("大模型暂时不可用，请稍后重试") from last_error

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        usage_out: dict[str, int] | None = None,
    ) -> AsyncIterator[str]:
        """流式对话，逐 delta 产出文本片段。流式不做重试（半截回复无法重放）。

        仅在开始前跳过冷却中的 provider（选链与 chat 一致）；
        首 token 之后的断流不切换 provider，由上层 error 事件兜底。

        E-13 成本计量：`usage_out` 传 dict 时，尝试带 `stream_options.include_usage`
        请求真实用量——OpenAI 兼容协议下服务端会在 `[DONE]` 前补一个含 `usage` 的
        收尾 chunk（DeepSeek 实测支持）。两个易踩点：
        1. **usage chunk 的 `choices` 是空列表**——必须先取 usage 再做 choices 判断，
           否则收尾 chunk 会被当噪声跳过（这正是本方法要写在循环最前面的原因）；
        2. 个别兼容网关不认识该参数会回 400——此时**降级重试一次**（400 发生在
           首 delta 之前，重放安全），并把 `usage_out["estimated"]` 置 1 交由上层估算。
        """
        if not settings.llm_api_key:
            raise LLMError("大模型未配置，请在 .env 中设置 LLM_API_KEY")

        provider = self._pick(self._chain())[0]
        base_payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {provider.api_key}"}

        # E-13：include_usage 开关。400 → 关掉重试一次；第二次再 400 说明是别的问题，如实上抛。
        want_usage = usage_out is not None
        try:
            http = await self._http_for(provider)
            while True:
                payload = dict(base_payload)
                if want_usage:
                    payload["stream_options"] = {"include_usage": True}
                async with http.stream(
                    "POST", "/chat/completions", json=payload, headers=headers
                ) as resp:
                    if resp.status_code == 401:
                        raise LLMError("大模型鉴权失败，请检查 API_KEY 配置")
                    if resp.status_code == 429:
                        raise LLMError("大模型限流中，请稍后重试")
                    if resp.status_code == 400 and want_usage:
                        want_usage = False
                        if usage_out is not None:
                            usage_out["estimated"] = 1
                        logger.info("上游不支持 stream_options.include_usage，改由上层估算 token")
                        continue
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            logger.warning("流式 chunk 解析失败: %r", data[:80])
                            continue
                        # 先取 usage：收尾 chunk 的 choices 为空，先判 choices 会把它跳掉
                        if usage_out is not None and chunk.get("usage"):
                            u = chunk["usage"]
                            usage_out["prompt_tokens"] = int(u.get("prompt_tokens") or 0)
                            usage_out["completion_tokens"] = int(u.get("completion_tokens") or 0)
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta:
                            yield delta
                    break  # 正常读完一轮，退出 while（400 降级重试用）
        except LLMError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.error("LLM 流式网络异常: %s", exc)
            raise LLMError("大模型连接中断，请稍后重试") from exc
        except httpx.HTTPStatusError as exc:
            logger.error("LLM 流式上游错误: %s", exc.response.status_code)
            raise LLMError("大模型暂时不可用，请稍后重试") from exc


llm_client = LLMClient()
