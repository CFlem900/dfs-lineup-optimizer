"""Central AI service — unified LLM provider abstraction.

Supports both **OpenAI** and **Anthropic** with:
  * Automatic provider selection via ``AI_PROVIDER`` env var
  * Two model tiers: ``default`` (cheap/fast) and ``reasoning`` (powerful)
  * Response caching in Redis (15-min TTL)
  * Retry with exponential back-off
  * Graceful degradation — returns ``None`` so callers fall back to
    existing rule-based logic

Every AI agent in the system depends on this single service.
"""

import hashlib
import json
import logging
import re
import threading
import time
from typing import AsyncGenerator, Optional

from app.config import get_settings
from app.models.ai import AIRequest, AIResponse

logger = logging.getLogger(__name__)


class AIService:
    """Unified LLM provider abstraction.

    Instantiated once at module level in ``routes.py`` and passed to
    every agent.  The ``cache`` attribute is attached after the Redis
    connection is ready (see ``main.py`` lifespan).
    """

    def __init__(self, cache_service=None):
        self._settings = get_settings()
        self._cache = cache_service
        self._provider = self._settings.ai_provider.lower()
        self._enabled = self._settings.ai_enabled

        # Lazy-initialised client handles
        self._openai_client = None
        self._anthropic_client = None

        # Concurrency control — limits parallel LLM calls to prevent
        # overwhelming the provider when multiple agents run concurrently
        self._concurrency_sem = threading.Semaphore(4)

    # ── Public API ──────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """True when AI is enabled, the provider SDK is installed, and an API key is set."""
        if not self._enabled:
            return False
        if self._provider == "openai":
            if not self._settings.openai_api_key:
                return False
            try:
                import openai  # noqa: F401
                return True
            except ImportError:
                logger.warning("[AI] openai package not installed — run: pip install openai")
                return False
        if self._provider == "anthropic":
            if not self._settings.anthropic_api_key:
                return False
            try:
                import anthropic  # noqa: F401
                return True
            except ImportError:
                logger.warning("[AI] anthropic package not installed — run: pip install anthropic")
                return False
        return False

    def set_cache(self, cache_service):
        """Attach cache after Redis is connected (called from lifespan)."""
        self._cache = cache_service

    # ── Completion (sync-safe) ─────────────────────────────────

    def complete(self, request: AIRequest) -> Optional[AIResponse]:
        """Single LLM completion with retry + optional caching.

        Returns ``None`` if AI is unavailable or the call fails after
        retries, letting callers transparently fall back.
        """
        if not self.is_available:
            return None

        # Cache check
        cache_key = self._build_cache_key(request)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        model = self._select_model(request.model_tier)
        last_error = None

        with self._concurrency_sem:
            for attempt in range(self._settings.ai_max_retries):
                try:
                    start = time.time()
                    if self._provider == "openai":
                        result = self._call_openai(request, model)
                    else:
                        result = self._call_anthropic(request, model)
                    result.latency_ms = int((time.time() - start) * 1000)

                    # Cache the response
                    self._cache_set(cache_key, result)

                    # Inline usage logging (sync pool — no event loop needed)
                    if hasattr(request, 'agent_name') and request.agent_name:
                        try:
                            from app.services.ai_usage_service import log_usage_sync
                            log_usage_sync(
                                agent_name=request.agent_name,
                                model_used=result.model_used,
                                provider=result.provider,
                                input_tokens=result.input_tokens,
                                output_tokens=result.output_tokens,
                                latency_ms=result.latency_ms,
                                cached=result.cached,
                            )
                        except Exception:
                            pass  # Never let usage logging break the API call

                    return result

                except Exception as exc:
                    last_error = exc

                    # Fail fast on non-retryable errors (model not found,
                    # auth failure, bad request).  Retrying these wastes
                    # time and pollutes logs.
                    exc_str = str(exc)
                    _status = getattr(exc, "status_code", None)
                    _is_not_found = (
                        _status == 404
                        or "not_found" in exc_str.lower()
                        or "model:" in exc_str.lower()
                    )
                    _is_auth = _status == 401 or "authentication" in exc_str.lower()
                    _is_bad_request = _status == 400

                    if _is_not_found:
                        logger.error(
                            f"[AI] Model not found (non-retryable): {exc}. "
                            f"Check ANTHROPIC_MODEL_DEFAULT in config."
                        )
                        break
                    if _is_auth:
                        logger.error(f"[AI] Authentication failed (non-retryable): {exc}")
                        break
                    if _is_bad_request:
                        logger.error(f"[AI] Bad request (non-retryable): {exc}")
                        break

                    wait = 2 ** attempt
                    logger.warning(
                        f"[AI] Attempt {attempt + 1}/{self._settings.ai_max_retries} "
                        f"failed ({exc}), retrying in {wait}s"
                    )
                    time.sleep(wait)

        logger.error(f"[AI] All attempts exhausted or non-retryable: {last_error}")
        return None

    def complete_json(self, request: AIRequest) -> Optional[dict]:
        """Completion that parses the response as JSON.

        Automatically sets ``response_format='json'`` and wraps the
        prompt with instructions to return valid JSON.  For Anthropic,
        uses assistant-message prefill to force pure JSON output.

        If the first parse fails, attempts lightweight repair:
          1. Strip markdown code fences
          2. Remove trailing commas before } or ]
          3. Close unterminated arrays/objects
          4. Truncate trailing non-JSON text
          5. If repair fails, return None (caller uses fallback)
        """
        # Harden system prompt to suppress conversational postscript
        json_suffix = (
            "\n\nCRITICAL: You are a machine. Output ONLY raw, valid JSON. "
            "DO NOT include the word 'Reasoning'. DO NOT output any markdown "
            "formatting, preamble, or postscript. If you output anything "
            "other than JSON, the system will crash."
        )
        request = request.model_copy(update={
            "response_format": "json",
            "system_prompt": request.system_prompt + json_suffix,
        })
        response = self.complete(request)
        if response is None:
            return None

        content = response.content.strip()

        # ── Pass 1: clean markdown fences ───────────────────────────
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            content = "\n".join(lines).strip()

        # ── Pass 2: try parsing as-is ──────────────────────────────
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning(
                f"[AI] JSON parse failed (attempting repair): {exc}"
            )

        # ── Pass 3: lightweight repair ─────────────────────────────
        try:
            repaired = content

            # Truncate trailing non-JSON text (e.g. "**Reasoning:** ...")
            # after the last top-level closing bracket/brace
            for _end_char, _start_char in [("]", "["), ("}", "{")]:
                if _start_char in repaired:
                    _last = repaired.rfind(_end_char)
                    if _last != -1 and _last < len(repaired) - 1:
                        repaired = repaired[: _last + 1]
                        break

            # Remove trailing commas before closing brackets: ,] or ,}
            repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

            # Close unterminated strings: if odd number of unescaped
            # quotes, append a closing quote
            _unescaped_quotes = re.findall(r'(?<!\\)"', repaired)
            if len(_unescaped_quotes) % 2 != 0:
                repaired += '"'

            # Balance brackets: count opens vs closes, append missing
            _open_braces = repaired.count("{") - repaired.count("}")
            _open_brackets = repaired.count("[") - repaired.count("]")
            repaired += "}" * max(0, _open_braces)
            repaired += "]" * max(0, _open_brackets)

            result = json.loads(repaired)
            logger.info("[AI] JSON repair succeeded")
            return result
        except (json.JSONDecodeError, Exception) as repair_exc:
            logger.warning(
                f"[AI] JSON repair also failed: {repair_exc} | "
                f"Raw content (first 500 chars): {content[:500]}"
            )
            return None

    def complete_tool_use(
        self,
        request: AIRequest,
        tool_name: str,
        tool_description: str,
        input_schema: dict,
    ) -> Optional[dict]:
        """Force structured output via Anthropic tool use.

        Defines a single tool with the given JSON Schema and forces the
        model to call it via ``tool_choice={"type": "tool", "name": ...}``.
        The returned dict is the validated ``input`` block from the tool
        call — guaranteed to conform to ``input_schema``.

        Falls back to ``complete_json()`` when provider is OpenAI (which
        doesn't support Anthropic-style tool_choice).
        """
        if not self.is_available:
            return None

        if self._provider == "openai":
            # OpenAI doesn't support Anthropic tool_choice — fall back
            return self.complete_json(request)

        model = self._select_model(request.model_tier)
        client = self._get_anthropic_client()

        tool_def = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": input_schema,
        }

        try:
            resp = client.messages.create(
                model=model,
                system=request.system_prompt,
                messages=[{"role": "user", "content": request.user_prompt}],
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                tools=[tool_def],
                tool_choice={"type": "tool", "name": tool_name},
            )

            # Extract the tool_use block
            for block in resp.content:
                if block.type == "tool_use" and block.name == tool_name:
                    # Inline usage logging (sync pool — no event loop needed)
                    if hasattr(request, 'agent_name') and request.agent_name:
                        try:
                            from app.services.ai_usage_service import log_usage_sync
                            log_usage_sync(
                                agent_name=request.agent_name,
                                model_used=model,
                                provider="anthropic",
                                input_tokens=resp.usage.input_tokens if resp.usage else 0,
                                output_tokens=resp.usage.output_tokens if resp.usage else 0,
                            )
                        except Exception:
                            pass
                    return block.input

            logger.warning(
                f"[AI] Tool use response had no '{tool_name}' block — "
                f"falling back to complete_json"
            )
            return self.complete_json(request)

        except Exception as exc:
            logger.warning(f"[AI] Tool use call failed: {exc} — falling back")
            return self.complete_json(request)

    def stream(self, request: AIRequest):
        """Generator yielding text chunks for SSE streaming.

        Note: this is a *synchronous* generator suitable for use with
        FastAPI's ``StreamingResponse`` + background thread pattern
        that the app already uses for ``/player-pool/stream``.
        """
        if not self.is_available:
            return

        model = self._select_model(request.model_tier)
        try:
            if self._provider == "openai":
                yield from self._stream_openai(request, model)
            else:
                yield from self._stream_anthropic(request, model)
        except Exception as exc:
            logger.error(f"[AI] Stream failed: {exc}")
            return

    # ── OpenAI ─────────────────────────────────────────────────

    def _get_openai_client(self):
        if self._openai_client is None:
            from openai import OpenAI
            self._openai_client = OpenAI(
                api_key=self._settings.openai_api_key,
                timeout=self._settings.ai_timeout,
            )
        return self._openai_client

    def _call_openai(self, request: AIRequest, model: str) -> AIResponse:
        client = self._get_openai_client()
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        resp = client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        return AIResponse(
            content=choice.message.content or "",
            model_used=model,
            provider="openai",
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )

    def _stream_openai(self, request: AIRequest, model: str):
        client = self._get_openai_client()
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    # ── Anthropic ──────────────────────────────────────────────

    def _get_anthropic_client(self):
        if self._anthropic_client is None:
            from anthropic import Anthropic
            self._anthropic_client = Anthropic(
                api_key=self._settings.anthropic_api_key,
                timeout=self._settings.ai_timeout,
            )
        return self._anthropic_client

    def _call_anthropic(self, request: AIRequest, model: str) -> AIResponse:
        client = self._get_anthropic_client()

        messages = [{"role": "user", "content": request.user_prompt}]

        # ── Prefill technique for JSON responses ──────────────────
        # Seed the assistant turn with the opening bracket so the LLM
        # continues with pure JSON — no preamble, no postscript.
        prefill = ""
        if request.response_format == "json":
            # Detect whether the expected shape is an array or object
            prompt_lower = request.user_prompt.lower()
            if "array" in prompt_lower or "list" in prompt_lower:
                prefill = "["
            else:
                prefill = "{"
            messages.append({"role": "assistant", "content": prefill})

        resp = client.messages.create(
            model=model,
            system=request.system_prompt,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )
        content = ""
        for block in resp.content:
            if hasattr(block, "text"):
                content += block.text

        # Prepend the prefill token the model didn't echo back
        if prefill:
            content = prefill + content

        return AIResponse(
            content=content,
            model_used=model,
            provider="anthropic",
            input_tokens=resp.usage.input_tokens if resp.usage else 0,
            output_tokens=resp.usage.output_tokens if resp.usage else 0,
        )

    def _stream_anthropic(self, request: AIRequest, model: str):
        client = self._get_anthropic_client()
        with client.messages.stream(
            model=model,
            system=request.system_prompt,
            messages=[{"role": "user", "content": request.user_prompt}],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        ) as stream:
            for text in stream.text_stream:
                yield text

    # ── Model Selection ────────────────────────────────────────

    def _select_model(self, tier: str) -> str:
        if self._provider == "openai":
            return (
                self._settings.openai_model_reasoning
                if tier == "reasoning"
                else self._settings.openai_model_default
            )
        return (
            self._settings.anthropic_model_reasoning
            if tier == "reasoning"
            else self._settings.anthropic_model_default
        )

    # ── Cache ──────────────────────────────────────────────────

    def _build_cache_key(self, request: AIRequest) -> str:
        raw = f"{request.system_prompt}:{request.user_prompt}:{request.model_tier}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"ai:resp:{digest}"

    def _cache_get(self, key: str) -> Optional[AIResponse]:
        if not self._cache:
            return None
        try:
            data = self._cache.get_sync(key)
            if data:
                parsed = json.loads(data) if isinstance(data, str) else data
                resp = AIResponse(**parsed)
                resp.cached = True
                return resp
        except Exception:
            pass
        return None

    def _cache_set(self, key: str, response: AIResponse):
        if not self._cache:
            return
        try:
            self._cache.set_sync(
                key,
                response.model_dump_json(),
                ttl=self._settings.ai_cache_ttl,
            )
        except Exception:
            pass
