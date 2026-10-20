"""OpenAI-compatible DeepSeek transport and document-analysis adapter.

Credentials are read only from the process environment. The transport is
injectable so tests never make a network request.
"""
from __future__ import annotations

import json
import os
import re
import hashlib
import threading
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


class DeepSeekError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


Transport = Callable[[str, str, dict[str, Any], float], Mapping[str, Any]]


#: 额度用尽时对用户说的**同一句话**。三个出口（研究车道的缺口、问答接口、需求访谈
#: 接口）与前端 ``frontend/src/services/researchCopy.js`` 逐字相同——三处各写一句的
#: 后果实测过：同一件事，三种说法，其中一种读起来像产品坏了。
#: 三件事缺一不可：①这是用量上限、不是故障；②什么时候能再试；③已取得的结果不会丢。
BUDGET_EXCEEDED_MESSAGE = (
    '本小时的模型调用额度已用完——这是用量上限，不是故障。'
    '额度按调用时间滚动恢复（最早的那次调用满一小时后自动让出名额），届时直接重试即可；'
    '已经取得的结果都保留着，不会丢。'
)

#: 快到上限时的提醒（``model_call_budget`` 在 ``remaining`` 不多时带上它）。
#: 用户过去是"跑到一半突然被挡"，事先毫无预兆。
BUDGET_LOW_MESSAGE = (
    '本小时的模型调用额度快用完了（已用 {used}/{limit} 次）——这是用量上限，不是故障。'
    '现在开始新的研究可能中途停下，建议稍后再试；已经取得的结果不会受影响。'
)

#: 低于这个剩余次数就开始提醒。一次普通文本 PDF 实测约 11–18 次调用，所以"剩 5 次"
#: 已经不够跑完一份资料。
BUDGET_LOW_WATERMARK = 5


def model_call_limit(value=None):
    """本小时共享额度上限。**只读配置**，默认值由环境变量 ``LAB_MODEL_CALLS_PER_HOUR`` 覆盖。

    默认值是成本决策，不是技术决策：先保功能、后收紧成本，
    按此把 20 提到 200。原来的 20 会把真实用户挡在门外——一份普通文本
    PDF 实测要 11–18 次、一份需要逐页视觉分析的扫描件约 30 次，而这份额度是**整台
    部署全局共享**（持久账本、跨进程、滚动一小时），扫一份扫描件就用光。改这个数字
    要连 ``README.md`` 与 ``tests/test_budget_messaging.py`` 的默认值断言一起改。
    """
    if value is not None:
        return int(value)
    return int(os.getenv('LAB_MODEL_CALLS_PER_HOUR', '200'))


def model_call_budget(limit=None):
    """本小时共享额度用了多少、还剩多少——给界面看得见，只读。

    两个计数取**大者**：进程内 ``_attempts``（测试注入 transport 时只有它）与持久账本
    （跨进程；真调用才写）。口径与 ``_call`` 里那道闸门一致，所以界面上说"还剩 0"
    的时候，下一次调用确实会被拦。
    """
    limit = model_call_limit(limit)
    now = time.monotonic()
    local = max((len([s for s in stamps if now - s < 3600]) for stamps in _attempts.values()),
                default=0)
    try:
        from backend.infrastructure.usage_ledger import usage
        ledger = usage('https://api.deepseek.com/chat/completions', limit)
    except Exception:  # 账本读不出来不能让这条只读接口变成故障
        ledger = {'used': 0, 'reset_in_seconds': 0}
    used = max(local, int(ledger.get('used') or 0))
    remaining = max(0, limit - used)
    return {
        'used': used,
        'limit': limit,
        'remaining': remaining,
        'window_seconds': 3600,
        'reset_in_seconds': int(ledger.get('reset_in_seconds') or 0),
        'exhausted_message': BUDGET_EXCEEDED_MESSAGE,
        'notice': BUDGET_LOW_MESSAGE.format(used=used, limit=limit)
                  if remaining <= BUDGET_LOW_WATERMARK else '',
    }


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """The provider's own explanation, which is the only thing that says *why*.

    A bare "HTTP 400" once sent a whole round looking for a request-format bug
    with nothing to read. The response body names the offending field -- for
    instance DeepSeek answers a reasoner-only deployment with
    "deepseek-reasoner does not support tool_choice" -- and that sentence is
    the entire diagnosis. Collapsed and bounded so one bad response cannot
    flood a run's gap list.
    """
    try:
        body = exc.read()
    except Exception:                                                               # noqa: BLE001
        return ''
    if not body:
        return ''
    text = re.sub(r'\s+', ' ', body.decode('utf-8', errors='replace')).strip()
    return f'：{text[:400]}' if text else ''


def read_event_stream(response):
    """Require both a final answer and stream completion; reasoning is separate."""
    answer, reasoning, usage, finish, done = [], [], {}, None, False
    size = 0
    for raw in response:
        size += len(raw)
        if size > 8 * 1024 * 1024:
            raise DeepSeekError('invalid_provider_response', '模型响应超出大小限制')
        line = raw.decode('utf-8').strip()
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if data == '[DONE]':
            done = True
            break
        try:
            event = json.loads(data)
        except ValueError as exc:
            raise DeepSeekError('provider_stream_interrupted', '回答传输不完整，请重试此步骤') from exc
        if event.get('usage'):
            usage = event['usage']
        for choice in event.get('choices') or []:
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            if isinstance(delta.get('content'), str):
                answer.append(delta['content'])
            if isinstance(delta.get('reasoning_content'), str):
                reasoning.append(delta['reasoning_content'])
            finish = choice.get('finish_reason') or finish
    if not done or not finish:
        raise DeepSeekError('provider_stream_interrupted', '回答传输中断，已有结果保留，请重试此步骤')
    return {'choices': [{'message': {'content': ''.join(answer)}, 'finish_reason': finish}], 'usage': usage}

# Process-local exact request cache and single-flight guard. No credentials are
# persisted. Failed calls count against the limit because they may be billed.
_call_lock = threading.Lock()
_responses: dict[str, tuple[float, Mapping[str, Any]]] = {}
_attempts: dict[str, list[float]] = {}


def _default_transport(api_key: str, endpoint: str, payload: dict[str, Any], timeout: float) -> Mapping[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}
    if '/anthropic/' in endpoint:
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    request = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if payload.get('stream'):
                return read_event_stream(response)
            raw = response.read()
    except TimeoutError as exc:
        raise DeepSeekError("provider_timeout", "DeepSeek 请求超时") from exc
    except urllib.error.HTTPError as exc:
        if exc.code == 402:
            raise DeepSeekError(
                "provider_payment_required",
                "DeepSeek 账户余额不足，请充值或恢复计费后重试",
            ) from exc
        raise DeepSeekError("provider_http_error",
                            f"DeepSeek 服务返回 HTTP {exc.code}{_http_error_detail(exc)}") from exc
    except urllib.error.URLError as exc:
        raise DeepSeekError("provider_unavailable", "DeepSeek 服务不可用") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeepSeekError("invalid_provider_response", "DeepSeek 返回不是有效 JSON") from exc


@dataclass
class DeepSeekClient:
    api_key: str
    endpoint: str = "https://api.deepseek.com/chat/completions"
    model: str = "deepseek-flash"
    timeout: float = 30.0
    max_tokens: int = 8192
    json_retries: int = 0
    web_tool_type: str = "web_search"
    transport: Transport = _default_transport
    thinking_enabled: bool = False
    capability: str = 'auto'

    def _call(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        identity = hashlib.sha256(f"{self.endpoint}|{self.api_key}|{id(self.transport)}".encode()).hexdigest()
        key = hashlib.sha256((identity + json.dumps(payload, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        # Tests inject transports; the same production guard is exercised by
        # dedicated contract tests without making network requests.
        with _call_lock:
            now = time.monotonic()
            for expired in [k for k, (stamp, _) in _responses.items() if now - stamp >= 300]:
                del _responses[expired]
            if key in _responses:
                self.last_usage = _responses[key][1].get('usage', {})
                return _responses[key][1]
            from backend.infrastructure.call_budget import reserve
            if not reserve():
                raise DeepSeekError('document_budget_exceeded',
                                    '本轮资料识别最多调用十二次——这是单次请求的用量上限，不是故障。'
                                    '已完成的识别结果都保留着；还有没识别的页时，只选关键几页再识别一次即可。')
            attempts = [stamp for stamp in _attempts.get(identity, []) if now - stamp < 3600]
            if len(attempts) >= model_call_limit():
                raise DeepSeekError("provider_budget_exceeded", BUDGET_EXCEEDED_MESSAGE)
            _attempts[identity] = [*attempts, now]
            if self.transport is _default_transport:
                from backend.infrastructure.usage_ledger import reserve_call
                ledger_endpoint = 'https://api.deepseek.com/chat/completions' if urlsplit(self.endpoint).hostname == 'api.deepseek.com' else self.endpoint
                if not reserve_call(ledger_endpoint, model_call_limit()):
                    raise DeepSeekError('provider_budget_exceeded', BUDGET_EXCEEDED_MESSAGE)
            ticket = None
            if self.transport is _default_transport:
                from backend.infrastructure.monetary_budget import reserve as reserve_money, settle
                try:
                    ticket = reserve_money(payload)
                except ValueError as exc:
                    # 配置层错误自带 code（例如"闸门开了却没给初始额度"），原样转述；
                    # 不许粗化成 monetary_budget_exceeded——额度没超，是没配好。
                    raise DeepSeekError(getattr(exc, 'code', None) or 'monetary_budget_exceeded',
                                        str(exc)) from exc
            response = self.transport(self.api_key, self.endpoint, payload, self.timeout)
            if self.transport is _default_transport:
                settle(ticket, response.get('usage', {}))
            self.last_usage = response.get('usage', {})
            if len(_responses) >= 128:
                _responses.pop(next(iter(_responses)))
            try:
                choice = response["choices"][0]
                content = choice["message"]["content"]
                valid = choice.get("finish_reason") not in {"length", "max_tokens"} and isinstance(self._parse_json_object(content), Mapping)
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                valid = False
            if valid:
                _responses[key] = (now, response)
            return response

    @classmethod
    def from_env(cls, *, transport: Transport | None = None, env: Mapping[str, str] | None = None, **kwargs: Any) -> "DeepSeekClient":
        values = os.environ if env is None else env
        key = values.get("DEEPSEEK_API_KEY")
        if not key:
            raise DeepSeekError("provider_not_configured", "未配置 DeepSeek 运行时凭据")
        kwargs.setdefault("endpoint", values.get("DEEPSEEK_ENDPOINT", "https://api.deepseek.com/chat/completions"))
        kwargs.setdefault("model", values.get("DEEPSEEK_MODEL", "deepseek-flash"))
        kwargs.setdefault("web_tool_type", values.get("DEEPSEEK_WEB_TOOL_TYPE", "web_search"))
        kwargs.setdefault('thinking_enabled', values.get('LAB_MODEL_THINKING', 'off') == 'on')
        kwargs.setdefault('capability', values.get('LAB_MODEL_CAPABILITY', 'auto'))
        kwargs.setdefault('timeout', float(values.get('LAB_MODEL_TIMEOUT', '120' if kwargs['thinking_enabled'] else '45')))
        return cls(api_key=key, transport=transport or _default_transport, **kwargs)

    def model_options(self):
        compatible = self.capability == 'qianwen' or (self.capability == 'auto' and urlsplit(self.endpoint).hostname == 'maas.qianwenaiapi.com')
        if not compatible:
            return {'thinking': {'type': 'enabled' if self.thinking_enabled else 'disabled'}}
        options = {'enable_thinking': self.thinking_enabled, 'stream': True, 'stream_options': {'include_usage': True}}
        if self.thinking_enabled:
            if self.model.lower().startswith('qwen'):
                options['thinking_budget'] = min(2048, self.max_tokens // 2)
            elif self.model.lower().startswith('deepseek'):
                options['reasoning_effort'] = 'high'
        return options

    def analyze(self, *, system_prompt: str, user_prompt: str) -> Mapping[str, Any]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_error: DeepSeekError | None = None
        for attempt in range(self.json_retries + 1):
            request_messages = list(messages)
            if attempt:
                request_messages.append({
                    "role": "user",
                    "content": (
                        "上一次输出为空、被截断或不是完整 JSON。请重新生成一个更精简的 json 对象，"
                        "严格遵守给定示例；不要 Markdown，不要解释，不要省略闭合括号。"
                    ),
                })
            payload = {
                "model": self.model,
                "temperature": 0,
                "max_tokens": self.max_tokens,
                **self.model_options(),
                "response_format": {"type": "json_object"},
                "messages": request_messages,
            }
            try:
                response = self._call(payload)
            except DeepSeekError as exc:
                if exc.code in {'provider_stream_interrupted', 'provider_timeout'} and attempt < self.json_retries:
                    last_error = exc
                    continue
                raise
            except TimeoutError as exc:
                raise DeepSeekError("provider_timeout", "DeepSeek 请求超时") from exc
            except (urllib.error.URLError, OSError) as exc:
                raise DeepSeekError("provider_unavailable", "DeepSeek 服务不可用") from exc
            try:
                choice = response["choices"][0]
                content = choice["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise DeepSeekError("invalid_provider_response", "DeepSeek 响应缺少模型内容") from exc
            finish_reason = str(choice.get("finish_reason") or "") if isinstance(choice, Mapping) else ""
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if not isinstance(content, str) or not content.strip():
                last_error = DeepSeekError("empty_model_output", "模型返回空内容")
                continue
            if finish_reason.casefold() in {"length", "max_tokens"}:
                last_error = DeepSeekError("model_output_truncated", "模型输出因长度限制被截断")
                continue
            try:
                value = self._parse_json_object(content)
            except json.JSONDecodeError:
                last_error = DeepSeekError("invalid_model_json", "模型输出不是有效 JSON")
                continue
            if not isinstance(value, Mapping):
                last_error = DeepSeekError("invalid_model_json", "模型输出必须是 JSON 对象")
                continue
            return value
        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse_json_object(content: str) -> Any:
        """Accept JSON mode output and the common fenced-JSON deviation only.

        Some OpenAI-compatible gateways wrap an otherwise valid JSON object in
        a Markdown fence even when ``response_format`` requests JSON.  Strip a
        single complete fence, but keep rejecting prose, truncated output and
        multiple payloads so downstream review gates never consume ambiguous
        model text.
        """
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            opening = lines[0].strip().casefold()
            if opening not in {"```", "```json"} or len(lines) < 3 or lines[-1].strip() != "```":
                raise json.JSONDecodeError("invalid JSON fence", text, 0)
            text = "\n".join(lines[1:-1]).strip()
        return json.loads(text)

    def search_web(self, *, query: str, purpose: str = "general", limit: int = 10) -> list[dict[str, str]]:
        """Run a model-native web search and keep only explicit URL citations.

        DeepSeek-compatible deployments differ in how they expose web tools.
        The request intentionally uses a small, configurable tool declaration and
        the response parser accepts the citation shapes used by both
        Chat-Completions-compatible gateways and Responses-style gateways. If a
        deployment does not expose the tool, the provider raises a normal
        ``DeepSeekError`` so the search orchestrator can move to its configured
        fallback without inventing sources.
        """
        if not query.strip():
            raise DeepSeekError("invalid_search_query", "联网检索词不能为空")
        if urlsplit(self.endpoint).hostname == "api.deepseek.com":
            return self._search_native_anthropic(query=query, purpose=purpose, limit=limit)
        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是带来源的采购检索助手。使用可用的联网工具检索公开网页。"
                        "只返回工具实际提供的 URL 引用，不要凭常识补写网址。"
                        "每条结果保留 title、url、snippet；没有工具引用时返回空结果。"
                    ),
                },
                {"role": "user", "content": f"检索目的：{purpose}\n检索词：{query}"},
            ],
            "tools": [{"type": self.web_tool_type}],
            "tool_choice": "auto",
        }
        try:
            response = self._call(payload)
        except DeepSeekError:
            raise
        except TimeoutError as exc:
            raise DeepSeekError("provider_timeout", "联网检索请求超时") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise DeepSeekError("provider_unavailable", "联网检索服务不可用") from exc
        return self._parse_web_citations(response, limit=limit)

    def _search_native_anthropic(self, *, query, purpose, limit):
        """Official native search uses Anthropic server tools, not chat tools."""
        from dataclasses import replace
        client = replace(self, endpoint="https://api.deepseek.com/anthropic/v1/messages", timeout=max(self.timeout, 60))
        response = client._call({
            "model": self.model, "max_tokens": 1800,
            "system": "必须调用web_search检索给定问题，优先高校、医院、疾控、政府采购和招标代理机构官网。仅依据实际搜索结果回答，不编造网址或价格。",
            "messages": [{"role": "user", "content": f"检索目的：{purpose}\n检索词：{query}"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            "tool_choice": {"type": "auto"},
        })
        self.last_usage = response.get('usage', {})
        results = []
        executed = False
        for block in response.get('content', []):
            if block.get('type') != 'web_search_tool_result':
                continue
            executed = True
            content = block.get('content', [])
            if isinstance(content, dict):
                raise DeepSeekError('web_search_failed', '原生联网工具未成功返回搜索结果')
            for item in content:
                if item.get('type') == 'web_search_result' and str(item.get('url', '')).startswith(('http://', 'https://')):
                    results.append({'url': item['url'], 'title': item.get('title', ''), 'snippet': ''})
        if not executed:
            raise DeepSeekError('web_search_not_executed', '模型未返回服务端联网工具结果，不能当作联网成功')
        return list({r['url']: r for r in results}.values())[:limit]

    @classmethod
    def _parse_web_citations(cls, response: Mapping[str, Any], *, limit: int) -> list[dict[str, str]]:
        """Extract only explicit URL citations from common provider responses."""
        candidates: list[Mapping[str, Any]] = []

        def add(value: Any) -> None:
            if isinstance(value, Mapping):
                candidates.append(value)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for item in value:
                    add(item)

        add(response.get("citations"))
        add(response.get("sources"))
        add(response.get("web_results"))
        add(response.get("output"))
        choices = response.get("choices")
        if isinstance(choices, Sequence) and choices:
            choice = choices[0]
            if isinstance(choice, Mapping):
                message = choice.get("message")
                if isinstance(message, Mapping):
                    add(message.get("citations"))
                    add(message.get("annotations"))
                    add(message.get("sources"))
                    add(message.get("web_results"))
                    add(message.get("content"))
                    add(message.get("tool_calls"))
                add(choice.get("annotations"))
                add(choice.get("output"))

        results: list[dict[str, str]] = []
        seen: set[str] = set()

        def visit(item: Mapping[str, Any]) -> None:
            if len(results) >= limit:
                return
            nested = item.get("annotations") or item.get("citations") or item.get("sources") or item.get("results")
            if nested is not None and nested is not item:
                add(nested)
            url = item.get("url") or item.get("source_url") or item.get("href")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                return
            if url in seen:
                return
            seen.add(url)
            title = str(item.get("title") or item.get("name") or "").strip()
            snippet = str(item.get("snippet") or item.get("text") or item.get("excerpt") or "").strip()
            results.append({"url": url, "title": title, "snippet": snippet[:2000]})

        # The recursive walk also handles Responses-style output blocks where
        # URL citations are nested under ``content[*].annotations``.
        pending = list(candidates)
        visited: set[int] = set()
        while pending and len(results) < limit:
            item = pending.pop(0)
            marker = id(item)
            if marker in visited:
                continue
            visited.add(marker)
            visit(item)
            for value in item.values():
                if isinstance(value, Mapping):
                    pending.append(value)
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    pending.extend(value for value in value if isinstance(value, Mapping))
            arguments = item.get("arguments")
            function = item.get("function")
            if arguments is None and isinstance(function, Mapping):
                arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, Mapping):
                    pending.append(parsed)

        return results


__all__ = ["DeepSeekClient", "DeepSeekError"]
