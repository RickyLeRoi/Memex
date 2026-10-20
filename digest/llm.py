from __future__ import annotations

import base64
import json
import logging
import re

import httpx

from .config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> dict:
    """Extract the first JSON object from a response (handles <think>, ```json and extra text)."""
    t = _THINK_RE.sub("", text).strip()
    t = _FENCE_RE.sub("", t).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"Nessun JSON nella risposta: {text[:200]!r}")
    try:
        obj = json.loads(t[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON non valido: {e}; risposta: {text[:200]!r}") from e
    if not isinstance(obj, dict):
        raise LLMError("La risposta JSON non è un oggetto")
    return obj


class LLM:
    def __init__(self, cfg: LLMConfig, http: httpx.Client | None = None):
        self.cfg = cfg
        self.http = http or httpx.Client(timeout=cfg.timeout)
        self._json_supported = cfg.json_mode == "json_object"

    def _post(self, payload: dict) -> str:
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        try:
            r = self.http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError(f"Endpoint LLM non raggiungibile ({url}): {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"LLM {r.status_code}: {r.text[:300]}", status=r.status_code)
        try:
            msg = r.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"Risposta inattesa dall'endpoint: {r.text[:300]}") from e
        return msg.get("content") or ""

    def chat(self, messages: list[dict], json_out: bool = False, model: str | None = None,
             max_tokens: int | None = None) -> str:
        payload = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            **self.cfg.extra_body,
        }
        if json_out and self._json_supported:
            payload["response_format"] = {"type": "json_object"}
            try:
                return self._post(payload)
            except LLMError as e:
                # some servers (e.g. old LM Studio versions) reject json_object
                if e.status in (400, 422):
                    log.warning("L'endpoint rifiuta response_format=json_object, proseguo senza")
                    self._json_supported = False
                    payload.pop("response_format")
                else:
                    raise
        return self._post(payload)

    def chat_json(self, system: str, user: str) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raw = self.chat(messages, json_out=True)
        try:
            return parse_json(raw)
        except LLMError:
            log.info("JSON non valido, chiedo al modello di correggerlo")
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "La risposta non è JSON valido. Restituisci SOLO l'oggetto JSON corretto."},
            ]
            return parse_json(self.chat(messages, json_out=True))

    def describe_image(self, image: bytes, mime: str, prompt: str, max_tokens: int = 1024) -> str:
        if not self.cfg.vision_model:
            return ""
        data_uri = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }]
        return _THINK_RE.sub("", self.chat(messages, model=self.cfg.vision_model, max_tokens=max_tokens)).strip()

    def list_models(self) -> list[str]:
        r = self.http.get(f"{self.cfg.base_url.rstrip('/')}/models",
                          headers={"Authorization": f"Bearer {self.cfg.api_key}"})
        r.raise_for_status()
        return [m.get("id", "?") for m in r.json().get("data", [])]
