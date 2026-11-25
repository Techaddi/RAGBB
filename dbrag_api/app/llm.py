"""Async LLM client (Ollama native API or any OpenAI-compatible server such as vLLM)."""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from .config import Settings


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.I | re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError("Model did not return JSON.")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"Model returned invalid JSON: {e}") from e


class LLMClient:
    def __init__(self, s: Settings):
        self.s = s
        self.http = httpx.AsyncClient(
            base_url=s.llm_base_url.rstrip("/"),
            timeout=httpx.Timeout(s.llm_timeout_sec, connect=5),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"Authorization": f"Bearer {s.llm_api_key}"} if s.llm_api_key else {},
        )
        self.sem = asyncio.Semaphore(s.llm_concurrency)   # protects a single GPU / Ollama instance

    async def aclose(self):
        await self.http.aclose()

    async def chat(self, system: str, messages: list[dict], json_mode: bool = True, max_tokens: int | None = None) -> str:
        msgs = [{"role": "system", "content": system}, *messages]
        mt = max_tokens or self.s.llm_max_tokens
        async with self.sem:
            try:
                if self.s.llm_provider == "ollama":
                    body = {
                        "model": self.s.llm_model, "messages": msgs, "stream": False,
                        "keep_alive": "30m",                       # keep model resident in RAM/VRAM
                        "options": {"temperature": 0, "num_ctx": self.s.llm_num_ctx, "num_predict": mt},
                    }
                    if json_mode:
                        body["format"] = "json"
                    r = await self.http.post("/api/chat", json=body)
                    r.raise_for_status()
                    return r.json()["message"]["content"]
                body = {"model": self.s.llm_model, "messages": msgs, "temperature": 0, "max_tokens": mt}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                r = await self.http.post("/v1/chat/completions", json=body)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except httpx.HTTPError as e:
                raise LLMError(f"LLM request failed: {e}") from e

    async def ping(self) -> bool:
        try:
            r = await self.http.get("/api/tags" if self.s.llm_provider == "ollama" else "/v1/models", timeout=3)
            return r.status_code < 500
        except httpx.HTTPError:
            return False

    async def warmup(self):
        try:
            await self.chat("Reply with {}", [{"role": "user", "content": "ok"}], max_tokens=4)
        except LLMError:
            pass
