"""Injectable multimodal/OCR adapter contract used by M02.

The ingestion layer deliberately knows nothing about provider SDKs or API
credentials.  Applications may inject an adapter at runtime (for example a
DeepSeek vision transport, Tesseract wrapper, or deterministic test double).
Its result is always a draft and is never a confirmed product fact.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import struct
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


@dataclass
class VisionResult:
    """Verbatim OCR text plus structured extraction draft.

    ``fields`` may contain arbitrary provider output, but M02 consumers should
    treat every value as ``pending_review``.  ``warnings`` are plain strings so
    adapters do not need to depend on the domain warning dataclass.
    """

    text: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provider: str | None = None
    source_refs: list[dict[str, Any]] = field(default_factory=list)


class VisionAdapter(Protocol):
    """Minimal runtime-injectable multimodal adapter interface."""

    provider: str

    def analyze(self, image_bytes: bytes, *, media_type: str,
                source_location: str) -> VisionResult | Mapping[str, Any]:
        ...


class VisionAdapterError(RuntimeError):
    """Stable, credential-safe failure returned by a production adapter."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # 配置类错误点名是哪个设置项写错了（如 ``LAB_VISION_JSON_RETRIES``）。
        # 与 ``HTTPException(detail={"code", "message", "field"})`` 同一个出口形状，
        # 外部不用为配置错误单独解析一套字段。
        self.field = field


VisionTransport = Callable[[str, Mapping[str, Any], Mapping[str, str], float], Mapping[str, Any]]


VISION_TIMEOUT_SETTING = "LAB_VISION_TIMEOUT"
VISION_RETRY_SETTING = "LAB_VISION_JSON_RETRIES"


def _shown_value(value: object, limit: int = 32) -> str:
    """把写错的那个值原样、单行、有限长度地写给人看。

    这两个设置都是行为开关，从来不是凭据，所以值本身可以回显；限长是防止一段
    误粘贴的长文本灌满响应。
    """

    text = " ".join(str(value).split())
    if not text:
        # 设成空串（例如在 Windows 上 ``set X=``）也算写错，得让人看出"值是空的"。
        return "（空）"
    return text if len(text) <= limit else f"{text[:limit]}…"


def _config_error(setting: str, raw: object, problem: str) -> VisionAdapterError:
    """构造一个点名设置项、原值和合法范围的配置错误。

    对外只留一条路：说清"哪一项、写的什么、允许什么"，不退回默认值——退回会把
    "装错了"变成"照默认跑"，见 ``docs/209`` §3。
    """

    return VisionAdapterError(
        "vision_provider_not_configured",
        f"配置项 {setting} 的值 {_shown_value(raw)} {problem}",
        field=setting,
    )


def vision_json_retries(values: Mapping[str, str] | None = None) -> int:
    """Resolve ``LAB_VISION_JSON_RETRIES`` (default ``1``, allowed ``0`` to ``3``).

    The adapter the API builds per request from the browser-configured model
    reads its retry budget through this parser.  An unusable value is a
    configuration error: it raises instead of quietly falling back, because a
    silent fallback would report "retried nothing" as if that had been the
    operator's choice.
    """

    source = os.environ if values is None else values
    raw = source.get(VISION_RETRY_SETTING, "1")
    try:
        retries = int(raw)
    except ValueError as exc:
        raise _config_error(
            VISION_RETRY_SETTING, raw, "不是整数，允许范围 0～3（视觉识别失败后的重试次数）"
        ) from exc
    if retries < 0 or retries > 3:
        raise _config_error(
            VISION_RETRY_SETTING, raw, "超出允许范围 0～3（视觉识别失败后的重试次数）"
        )
    return retries


def vision_timeout(values: Mapping[str, str] | None = None) -> float:
    """Resolve ``LAB_VISION_TIMEOUT`` (default ``45``, allowed above ``0`` to ``180``).

    Only the timeout is configurable here: the endpoint, the credential and the
    model always come from the reconfigured thinking model client, which is the
    single multimodal API this product deploys.  As with the retry budget, an
    unusable value raises instead of falling back, so a typo cannot silently
    become a different timeout than the operator asked for.
    """

    source = os.environ if values is None else values
    raw = source.get(VISION_TIMEOUT_SETTING, "45")
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise _config_error(
            VISION_TIMEOUT_SETTING, raw, "不是数字，允许范围是大于 0 且不超过 180（单次视觉请求超时秒数）"
        ) from exc
    if timeout <= 0 or timeout > 180:
        raise _config_error(
            VISION_TIMEOUT_SETTING, raw, "超出允许范围（单次视觉请求超时秒数，要求大于 0 且不超过 180）"
        )
    return timeout


def _vision_probe_png(sequence: str) -> bytes:
    """Build a dependency-free high-contrast numeric image challenge."""

    glyphs = {
        "2": ("11111", "00001", "00001", "11111", "10000", "10000", "11111"),
        "3": ("11111", "00001", "00001", "11111", "00001", "00001", "11111"),
        "4": ("10001", "10001", "10001", "11111", "00001", "00001", "00001"),
        "5": ("11111", "10000", "10000", "11111", "00001", "00001", "11111"),
        "6": ("11111", "10000", "10000", "11111", "10001", "10001", "11111"),
        "8": ("11111", "10001", "10001", "11111", "10001", "10001", "11111"),
        "9": ("11111", "10001", "10001", "11111", "00001", "00001", "11111"),
    }
    scale, gap, margin = 10, 16, 16
    glyph_width, glyph_height = 5 * scale, 7 * scale
    width = margin * 2 + len(sequence) * glyph_width + max(0, len(sequence) - 1) * gap
    height = margin * 2 + glyph_height
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            color = (255, 255, 255)
            relative_x, relative_y = x - margin, y - margin
            stride = glyph_width + gap
            if 0 <= relative_y < glyph_height and relative_x >= 0:
                index, within = divmod(relative_x, stride)
                if index < len(sequence) and within < glyph_width:
                    row, column = relative_y // scale, within // scale
                    if glyphs[sequence[index]][row][column] == "1":
                        color = (0, 0, 0)
            raw.extend(color)

    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
        + chunk(b"IEND", b"")
    )


@dataclass
class OpenAICompatibleVisionAdapter:
    """OpenAI-compatible chat-completions adapter with an image input.

    Callers hand in the endpoint, the credential and the model explicitly.  The
    product's runtime path builds it from the same thinking-model client the
    user configured in the browser; a text-only OpenAI-compatible endpoint must
    not be presented as image capable merely because its request format is
    similar, so the connection check is a real image probe.
    """

    api_key: str
    endpoint: str
    model: str
    timeout: float = 45.0
    max_tokens: int = 4096
    # Programmatic callers (and tests) keep the single-attempt default; the
    # runtime path opts in explicitly through ``vision_json_retries``.
    json_retries: int = 0
    transport: VisionTransport | None = None
    provider: str = "openai-compatible-vision"

    def _call(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        if self.transport is not None:
            return self.transport(self.endpoint, payload, {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, self.timeout)
        # Text and image calls to the same real provider share the hourly cap
        # and exact-request cache. Injected test adapters stay network-free.
        from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
        try:
            return DeepSeekClient(api_key=self.api_key, endpoint=self.endpoint, model=self.model, timeout=self.timeout)._call(payload)
        except DeepSeekError as exc:
            raise VisionAdapterError(exc.code, exc.message) from exc

    @staticmethod
    def _default_transport(endpoint: str, payload: Mapping[str, Any],
                           headers: Mapping[str, str], timeout: float) -> Mapping[str, Any]:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise VisionAdapterError("vision_auth_failed", "视觉服务鉴权失败") from exc
            if exc.code in {400, 404, 415, 422}:
                raise VisionAdapterError("vision_input_unsupported", "视觉服务不接受当前模型或图像输入") from exc
            raise VisionAdapterError("vision_provider_unavailable", f"视觉服务返回 HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise VisionAdapterError("vision_provider_timeout", "视觉服务请求超时") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise VisionAdapterError("vision_provider_timeout", "视觉服务请求超时") from exc
            raise VisionAdapterError("vision_provider_unavailable", "视觉服务暂不可用") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise VisionAdapterError("vision_invalid_response", "视觉服务返回了无效 JSON") from exc

    @staticmethod
    def _content(payload: Mapping[str, Any]) -> str:
        try:
            content = payload["choices"][0]["message"]["content"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionAdapterError("vision_invalid_response", "视觉服务响应缺少 message.content") from exc
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", "")) for item in content
                if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}
            )
        if not isinstance(content, str) or not content.strip():
            raise VisionAdapterError("vision_empty_result", "视觉服务返回空结果")
        value = content.strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[-1]
            value = value.rsplit("```", 1)[0].strip()
        return value

    def analyze(self, image_bytes: bytes, *, media_type: str,
                source_location: str) -> VisionResult:
        if not image_bytes:
            raise VisionAdapterError("vision_input_invalid", "视觉输入为空")
        if not media_type.startswith("image/"):
            raise VisionAdapterError("vision_input_unsupported", "视觉适配器只接受图片输入")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        prompt = (
            "你正在读取一页实验室仪器采购资料的渲染图。此任务必须适用于任何仪器品类，"
            "不得套用某个品类的预设参数。先忠实识别页面，再抽取页面明确出现的证据。"
            "只返回一个完整 JSON 对象，不要 Markdown 或解释。结构："
            '{"text":"与采购判断有关的可读原文，最多2500字","fields":{"candidate_mentions":['
            '{"product_role":"instrument|instrument_configuration|software|accessory|consumable|assay_or_setting|cross_promotion",'
            '"brand":null,"series":null,"model":null,"configuration":null,"evidence_quote":"原文"}],'
            '"evidence_units":[{"topic":"identity|function|mode|capacity|compatibility|performance|accuracy|safety|software|accessories|physical|commercial|other",'
            '"label":"原文字段名","value":"原文值","unit":null,"evidence_quote":"原文"}]},'
            '"source_refs":[{"field":"字段或候选","quote":"原文短摘录"}]}。'
            "candidate_mentions 只记录本页明确出现的完整型号或配置，不从系列名猜型号。"
            "必须按本页语境填写 product_role：可独立采购运行的主仪器才是 instrument；"
            "主仪器完整配置是 instrument_configuration；软件、配件、耗材、检测方法或界面预设、"
            "以及仅用于引流到其他产品页面的交叉推广分别标为 software、accessory、consumable、"
            "assay_or_setting、cross_promotion，不能伪装成主仪器。"
            "evidence_units 每页最多 20 条，只保留影响采购判断的事实；每条摘录最多 120 字。"
            "text 只保留型号、配置、规格、功能、价格、服务等采购相关原文，不复制页眉页脚和营销段落。"
            "图中没有的信息不要补写，无法识别的内容留空。"
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}},
                ],
            }],
        }
        transport = self.transport or self._default_transport
        result: VisionResult | None = None
        last_output_error: VisionAdapterError | None = None
        for attempt in range(self.json_retries + 1):
            if attempt:
                payload["messages"][0]["content"][0]["text"] = (
                    prompt + " 上一次输出为空、截断或不是完整 JSON；请压缩 text 和 evidence_units 后重新输出。"
                )
            try:
                raw = self._call(payload)
            except VisionAdapterError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                raise VisionAdapterError("vision_provider_timeout", "视觉服务请求超时") from exc
            except Exception as exc:
                raise VisionAdapterError("vision_provider_unavailable", "视觉服务调用失败") from exc
            try:
                choice = raw["choices"][0]  # type: ignore[index]
                finish_reason = str(choice.get("finish_reason") or "") if isinstance(choice, Mapping) else ""
                if finish_reason.casefold() in {"length", "max_tokens"}:
                    raise VisionAdapterError("vision_output_truncated", "视觉服务输出因长度限制被截断")
                parsed = json.loads(self._content(raw))
                if not isinstance(parsed, Mapping):
                    raise VisionAdapterError("vision_invalid_response", "视觉服务内容必须是 JSON 对象")
                # An empty page and an unparseable page are the same kind of
                # output-quality problem, and the retry prompt above already says
                # "上一次输出为空".  Judging emptiness here (rather than after the
                # loop) is what lets the empty case use its retry.
                candidate = normalize_vision_result(parsed)
                if not candidate.text.strip() and not candidate.fields:
                    raise VisionAdapterError("vision_empty_result", "视觉服务返回空文本和空字段")
                result = candidate
                break
            except json.JSONDecodeError:
                last_output_error = VisionAdapterError("vision_invalid_response", "视觉服务内容不是有效 JSON")
            except VisionAdapterError as exc:
                last_output_error = exc
        if result is None:
            # The outward vocabulary is unchanged: an exhausted retry budget
            # still reports the last output-quality error, never a new code.
            raise last_output_error or VisionAdapterError("vision_invalid_response", "视觉服务内容不是有效 JSON")
        result.provider = self.provider
        result.source_refs = [
            {**ref, "provider_location": ref.get("location"), "location": source_location}
            for ref in result.source_refs
        ] or [{"location": source_location, "quote": result.text}]
        return result

    def probe(self) -> None:
        """Verify that the endpoint actually consumes image content.

        A text-only endpoint can follow a fixed ``{"ok": true}`` instruction,
        so the probe uses an unpredictable numeric code that is present only
        in the image. No credential or provider response is returned to callers.
        """

        sequence = "".join(secrets.choice("2345689") for _ in range(6))
        encoded = base64.b64encode(_vision_probe_png(sequence)).decode("ascii")
        prompt = (
            "这是视觉连接探针。请从左到右读取图像中的六位黑色数字验证码。"
            "只返回一个完整 JSON 对象：{\"sequence\":\"六位数字\"}。"
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 128,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ],
            }],
        }
        transport = self.transport or self._default_transport
        try:
            raw = self._call(payload)
            parsed = json.loads(self._content(raw))
        except VisionAdapterError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VisionAdapterError("vision_probe_failed", "视觉服务未返回可验证的图像读取结果") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise VisionAdapterError("vision_provider_timeout", "视觉服务请求超时") from exc
        except Exception as exc:
            raise VisionAdapterError("vision_provider_unavailable", "视觉服务调用失败") from exc
        observed = re.sub(r"\D", "", str(parsed.get("sequence", ""))) if isinstance(parsed, Mapping) else ""
        if observed != sequence:
            raise VisionAdapterError("vision_probe_failed", "模型未能正确读取图像内容")


def normalize_vision_result(raw: VisionResult | Mapping[str, Any] | Any) -> VisionResult:
    """Accept common test/provider return shapes without importing an SDK."""

    if isinstance(raw, VisionResult):
        return raw
    if isinstance(raw, Mapping):
        text = raw.get("text", raw.get("ocr_text", ""))
        fields = raw.get("fields", raw.get("structured_fields", {}))
        warnings = raw.get("warnings", [])
        provider = raw.get("provider")
        source_refs = raw.get("source_refs", raw.get("sources", []))
        return VisionResult(
            text="" if text is None else str(text),
            fields=dict(fields) if isinstance(fields, Mapping) else {},
            warnings=[str(x) for x in warnings] if isinstance(warnings, (list, tuple)) else ([str(warnings)] if warnings else []),
            provider=str(provider) if provider else None,
            source_refs=[dict(x) for x in source_refs if isinstance(x, Mapping)] if isinstance(source_refs, (list, tuple)) else [],
        )
    return VisionResult(
        text=str(getattr(raw, "text", getattr(raw, "ocr_text", "")) or ""),
        fields=dict(getattr(raw, "fields", getattr(raw, "structured_fields", {})) or {}),
        warnings=[str(x) for x in (getattr(raw, "warnings", []) or [])],
        provider=getattr(raw, "provider", None),
        source_refs=[dict(x) for x in (getattr(raw, "source_refs", []) or []) if isinstance(x, Mapping)],
    )


__all__ = [
    "VisionAdapter", "VisionResult", "VisionAdapterError",
    "OpenAICompatibleVisionAdapter", "normalize_vision_result",
    "vision_json_retries", "vision_timeout",
]
