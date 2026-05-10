"""
LangFuse observability — tracing, cost tracking, and prompt management.

Exposes three things used across the codebase:

  observe(name)                  — decorator that wraps a function in a
                                   LangFuse span so latency, inputs, and
                                   outputs are visible in the dashboard.

  update_current_observation()   — call inside an @observe function to
                                   attach extra metadata (token counts,
                                   search latency, etc.) to the active span.

  fetch_prompt(name, fallback)   — fetch a prompt template from LangFuse
                                   Prompt Management; use the local fallback
                                   string when LangFuse is unreachable or the
                                   prompt hasn't been created yet.

LangFuse is optional — if LANGFUSE_SECRET_KEY is missing, all calls are
no-ops so the agent still works in offline / test environments.
"""

from __future__ import annotations

import os
from functools import wraps
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ---------------------------------------------------------------------------
# LangFuse client — initialised once, None if keys are missing
# ---------------------------------------------------------------------------

_langfuse = None

def _get_langfuse():
    global _langfuse
    if _langfuse is not None:
        return _langfuse

    secret_key  = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    public_key  = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    base_url    = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").strip()

    if not secret_key or not public_key:
        logger.warning(
            "LANGFUSE_SECRET_KEY or LANGFUSE_PUBLIC_KEY not set — "
            "observability disabled, local fallbacks will be used."
        )
        return None

    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            secret_key=secret_key,
            public_key=public_key,
            host=base_url,
        )
        logger.info("LangFuse client initialised ({})", base_url)
    except Exception as exc:
        logger.warning("LangFuse init failed: {} — running without tracing.", exc)
        _langfuse = None

    return _langfuse


# ---------------------------------------------------------------------------
# observe — tracing decorator
# ---------------------------------------------------------------------------

def observe(name: str) -> Callable:
    """
    Decorator that wraps a function in a LangFuse observation span.

    If LangFuse is not configured the function runs normally with no
    overhead — the decorator becomes a transparent pass-through.

    Usage
    -----
        @observe(name="rag_search")
        def search(query: str) -> list:
            ...
    """
    def decorator(func: Callable) -> Callable:
        try:
            from langfuse.decorators import observe as lf_observe
            return lf_observe(name=name)(func)
        except Exception:
            # LangFuse not available or not configured — no-op wrapper
            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return func(*args, **kwargs)
            return wrapper
    return decorator


# ---------------------------------------------------------------------------
# update_current_observation — attach metadata to the active span
# ---------------------------------------------------------------------------

def update_current_observation(**kwargs: Any) -> None:
    """
    Attach extra metadata to the currently active LangFuse span.

    Call this inside any function decorated with ``@observe``.
    Safe to call even when LangFuse is not configured (no-op).

    Common kwargs
    -------------
    input       : the input value sent to the LLM / tool
    output      : the output value returned
    metadata    : dict of arbitrary key/value pairs
    usage       : {"input": n_tokens, "output": n_tokens}
    model       : model name string

    Usage
    -----
        @observe(name="embed_query")
        def embed(text: str) -> list:
            update_current_observation(input=text, metadata={"chars": len(text)})
            ...
    """
    try:
        from langfuse.decorators import langfuse_context
        langfuse_context.update_current_observation(**kwargs)
    except Exception:
        pass  # LangFuse not available — silently ignore


# ---------------------------------------------------------------------------
# fetch_prompt — LangFuse Prompt Management with local fallback
# ---------------------------------------------------------------------------

def fetch_prompt(
    name: str,
    fallback: str,
    **variables: Any,
) -> str:
    """
    Fetch a compiled prompt from LangFuse Prompt Management.

    If the prompt exists in LangFuse the latest published version is
    returned with ``variables`` interpolated (Mustache ``{{var}}`` syntax).
    Falls back to the local ``fallback`` string when:
      - LangFuse is not configured
      - The prompt name doesn't exist in LangFuse yet
      - Any network / API error occurs

    Parameters
    ----------
    name:
        The prompt name as created in LangFuse → Prompts dashboard.
    fallback:
        Local template string used when LangFuse is unavailable.
        May contain ``{variable}`` Python format placeholders.
    **variables:
        Template variables to interpolate into whichever template is used.

    Usage
    -----
        system = fetch_prompt(
            "gift-distill-system",
            fallback=_DISTILL_SYSTEM_FALLBACK,
        )
        user = fetch_prompt(
            "gift-distill-user",
            fallback=_DISTILL_USER_FALLBACK,
            conversation="User: I need a gift...",
        )
    """
    lf = _get_langfuse()
    if lf is not None:
        try:
            prompt_obj = lf.get_prompt(name)
            return prompt_obj.compile(**variables)
        except Exception as exc:
            logger.debug(
                "fetch_prompt('{}') fell back to local: {}", name, exc
            )

    # Local fallback — interpolate Python {variable} placeholders
    if variables:
        try:
            return fallback.format(**variables)
        except KeyError:
            pass  # Missing variable — return raw fallback

    return fallback
