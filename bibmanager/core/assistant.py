"""Ask questions about a paper to a large language model (the "Ask AI" panel). Pure Python (httpx), no Qt.

Providers are reached through their HTTP APIs with the user's own API key; an API key is billed per use and is not
part of a ChatGPT / Claude / Gemini subscription. Local models served by Ollama need no key and no network.

* ``PROVIDERS``: Claude (Anthropic), ChatGPT models (OpenAI), Gemini (Google), Ollama (local), and any server with an
  OpenAI-compatible API (Mistral, OpenRouter, LM Studio, a university endpoint, ...).
* ``build_context`` turns the paper (metadata + text of the main PDF, page by page, and optionally of the
  supplementary PDFs) into what is sent with every question; ``AssistantClient.ask`` streams the answer.
* The PDF file itself can be sent instead of its text to providers that read PDFs (figures, tables, equations).

Nothing here stores anything: conversations live in the GUI, API keys in ``core/secrets.KeyStore``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import APP_NAME, __version__
from .models import Paper

log = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_CHARS = 300_000  # ~75k tokens: a long paper; supplementary text beyond it is cut
DEFAULT_MAX_TOKENS = 4096  # length of an answer
CHARS_PER_TOKEN = 4.0  # rough average for English scientific prose (an estimate shown to the user, not a count)
# Conservative size limits for sending the PDF file itself (beyond them the text is sent instead).
PDF_LIMIT_BYTES = {"anthropic": 30_000_000, "openai": 30_000_000, "gemini": 18_000_000}


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    label: str
    api: str  # wire protocol: "anthropic" | "openai" | "gemini" | "ollama"
    base_url: str
    needs_key: bool = True
    key_url: str = ""  # where the user creates a key
    pdf_input: bool = False  # can receive the PDF file itself
    local: bool = False  # runs on this computer: the paper never leaves it
    hint: str = ""


PROVIDERS: dict[str, Provider] = {
    p.id: p
    for p in (
        Provider("anthropic", "Claude (Anthropic)", "anthropic", "https://api.anthropic.com",
                 key_url="https://platform.claude.com/settings/keys", pdf_input=True,
                 hint="API key from the Claude Console (billed per use, separate from a Claude subscription)."),
        Provider("openai", "ChatGPT models (OpenAI)", "openai", "https://api.openai.com/v1",
                 key_url="https://platform.openai.com/api-keys", pdf_input=True,
                 hint="API key from the OpenAI platform (billed per use, separate from ChatGPT Plus/Pro)."),
        Provider("gemini", "Gemini (Google)", "gemini", "https://generativelanguage.googleapis.com/v1beta",
                 key_url="https://aistudio.google.com/apikey", pdf_input=True,
                 hint="API key from Google AI Studio. On the free tier Google may use what you send to improve "
                      "its products and human reviewers may read it: do not send confidential papers."),
        Provider("ollama", "Local model (Ollama)", "ollama", "http://localhost:11434", needs_key=False, local=True,
                 key_url="https://ollama.com/download",
                 hint="Runs on this computer: nothing leaves it. Install Ollama and pull a model first."),
        Provider("compatible", "Other (OpenAI-compatible server)", "openai", "", needs_key=False,
                 hint="Any server with an OpenAI-compatible /chat/completions endpoint (key optional)."),
    )
}  # fmt: skip

# The chat web sites, for asking with a subscription instead of an API key (the GUI copies the prompt and shows the
# PDF so that the user can attach it there).
WEB_APPS: tuple[tuple[str, str], ...] = (
    ("ChatGPT", "https://chatgpt.com/"),
    ("Claude", "https://claude.ai/new"),
    ("Gemini", "https://gemini.google.com/app"),
    ("Microsoft Copilot", "https://copilot.microsoft.com/"),
)

QUICK_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Summary", "Summarise the paper: research question, method, main results and conclusions."),
    ("Assumptions", "List the main modelling assumptions and hypotheses, with the page where each is stated."),
    ("Key equations", "Which are the key equations of the paper? Write them in LaTeX and explain each symbol."),
    ("Method", "Describe the method or numerical/experimental procedure step by step."),
    ("Limitations", "What are the limitations of this work, those stated by the authors and others you see?"),
    ("Cite it for", "In one or two sentences each: for which claims could I cite this paper in my own work?"),
)


class AssistantError(RuntimeError):
    pass


@dataclass(slots=True)
class ChatMessage:
    role: str  # "user" | "assistant"
    text: str


@dataclass(slots=True)
class PaperContext:
    """What is sent with every question about one paper."""

    paper_id: int
    header: str  # bibliographic description
    text: str  # paper text with page markers ("" when only the PDF file is sent)
    pages: int = 0
    truncated: bool = False
    pdf_bytes: bytes | None = None
    pdf_name: str = ""
    sources: list[str] = field(default_factory=list)

    @property
    def estimated_tokens(self) -> int:
        """Rough size of the paper text (a PDF sent as a file is counted by the provider, roughly 1-3k tokens per
        page including page images)."""
        return int((len(self.header) + len(self.text)) / CHARS_PER_TOKEN)


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0  # read from the provider's prompt cache (cheaper)

    def describe(self) -> str:
        if not (self.input_tokens or self.output_tokens):
            return ""
        cached = f", {self.cached_tokens:,} from cache" if self.cached_tokens else ""
        return f"{self.input_tokens:,} tokens in{cached}, {self.output_tokens:,} out"


# --------------------------------------------------------------------------- context
def describe_paper(paper: Paper) -> str:
    m = paper.meta
    lines = [f"Title: {m.title or '(unknown)'}", f"Authors: {m.authors_text() or '(unknown)'}", f"Year: {m.year_label}"]
    for label, value in (("Published in", m.journal), ("Volume", m.volume), ("Pages", m.pages), ("DOI", m.doi),
                         ("arXiv", m.arxiv_id), ("Citation key", paper.citation_key)):  # fmt: skip
        if value:
            lines.append(f"{label}: {value}")
    if m.abstract.strip():
        lines.append(f"Abstract (from the library record): {m.abstract.strip()}")
    return "\n".join(lines)


def build_context(
    paper: Paper,
    documents: Sequence[tuple[str, Sequence[str]]],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    pdf_bytes: bytes | None = None,
    pdf_name: str = "",
) -> PaperContext:
    """``documents``: ``(label, page_texts)`` in order -- the main PDF first, then supplementary PDFs.

    Pages are marked ``--- page N ---`` so that answers can point to them; text beyond ``max_chars`` is left out
    (and the model is told so). With ``pdf_bytes`` the main PDF is sent as a file and only the supplementary text
    goes in ``text``.
    """
    parts: list[str] = []
    used = 0
    truncated = False
    pages = 0
    sources = []
    for index, (label, page_texts) in enumerate(documents):
        if index == 0 and pdf_bytes is not None:
            pages += len(page_texts)
            sources.append(f"{label} (PDF file)")
            continue
        header = f"===== {label} =====" if index else f"===== {label} (main article) ====="
        block = [header]
        for number, text in enumerate(page_texts, start=1):
            text = re.sub(r"[ \t]+\n", "\n", text or "").strip()
            chunk = f"--- page {number} ---\n{text}"
            if used + len(chunk) > max_chars:
                truncated = True
                remaining = max_chars - used
                if remaining > 2000:  # keep a meaningful part of the page
                    block.append(chunk[:remaining])
                block.append(f"[... text truncated here: pages {number}-{len(page_texts)} of this file not included ...]")
                break
            block.append(chunk)
            used += len(chunk)
            pages += 1
        parts.append("\n".join(block))
        sources.append(label)
        if truncated:
            break
    return PaperContext(
        paper_id=paper.id,
        header=describe_paper(paper),
        text="\n\n".join(parts),
        pages=pages,
        truncated=truncated,
        pdf_bytes=pdf_bytes,
        pdf_name=pdf_name,
        sources=sources,
    )


SYSTEM_PROMPT = """You are a research assistant helping a scientist read one paper from their reference library.
Answer from the paper (and its supplementary material, when given). When a statement comes from the paper, give the
page as (p. N). If the paper does not answer the question, say so first; then you may add general knowledge, clearly
labelled as not from the paper. Do not invent results, equations, numbers or references. Write mathematics in LaTeX
($...$ inline, $$...$$ displayed). Be precise and concise; answer in the language of the question."""


def system_prompt(context: PaperContext) -> str:
    note = (
        "\nThe text below is incomplete (it was cut at the configured length); say so if the answer may be in "
        "the missing part."
        if context.truncated
        else ""
    )
    return f"{SYSTEM_PROMPT}{note}\n\nLibrary record of the paper:\n{context.header}"


def paper_message(context: PaperContext) -> str:
    """The paper as the first part of the first user message (kept identical so that providers can cache it)."""
    if context.pdf_bytes is not None and not context.text:
        return "The paper is attached as a PDF file."
    intro = "The paper is attached as a PDF file. " if context.pdf_bytes is not None else ""
    return f"{intro}Text of the paper extracted from the PDF, page by page:\n\n{context.text}"


def web_prompt(paper: Paper, question: str) -> str:
    """What the GUI copies for a chat web site (the user attaches the PDF there)."""
    return (
        f"I attach the PDF of a paper. {SYSTEM_PROMPT}\n\nLibrary record of the paper:\n{describe_paper(paper)}\n\n"
        f"Question: {question.strip() or 'Summarise the paper.'}"
    )


# --------------------------------------------------------------------------- HTTP client
@dataclass(slots=True)
class AssistantSettings:
    provider: str = "anthropic"
    model: str = ""
    base_url: str = ""  # "" -> the provider's default
    max_tokens: int = DEFAULT_MAX_TOKENS
    workspace_id: str = ""  # Claude: keys that are not scoped to a workspace need it (anthropic-workspace-id)
    timeout_s: float = 300.0


def _clean(base: str) -> str:
    return base.strip().rstrip("/")


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300] or response.reason_phrase
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)
        if isinstance(err, str):
            return err
        if isinstance(data.get("message"), str):
            return data["message"]
    if isinstance(data, list) and data and isinstance(data[0], dict):  # Gemini wraps errors in a list
        return _error_text_from(data[0])
    return str(data)[:300]


def _error_text_from(data: dict[str, Any]) -> str:
    err = data.get("error")
    return str(err.get("message")) if isinstance(err, dict) else str(data)[:300]


def _explain(status: int, message: str, provider: Provider) -> str:
    if status in (401, 403):
        return f"{provider.label} refused the API key ({status}): {message}"
    if status == 404:
        return f"Not found ({status}) -- is the model name right? {message}"
    if status == 429:
        return f"Rate limit or quota reached at {provider.label} ({status}): {message}"
    if status == 413:
        return f"The request is too large for {provider.label} ({status}): send the text instead of the PDF, or less text."
    return f"{provider.label} answered {status}: {message}"


class AssistantClient:
    """One provider, one key. ``list_models`` and ``ask`` block: call them from a worker thread."""

    def __init__(self, settings: AssistantSettings, api_key: str = "", *, client: httpx.Client | None = None) -> None:
        if settings.provider not in PROVIDERS:
            raise AssistantError(f"Unknown provider {settings.provider!r}")
        self.settings = settings
        self.provider = PROVIDERS[settings.provider]
        self.api_key = api_key.strip()
        self.base_url = _clean(settings.base_url or self.provider.base_url)
        if not self.base_url:
            raise AssistantError("Set the address of the server (Settings of the Ask AI panel)")
        if self.provider.needs_key and not self.api_key:
            raise AssistantError(f"No API key for {self.provider.label}: add it in the settings of the Ask AI panel")
        self._client = client or httpx.Client(timeout=httpx.Timeout(settings.timeout_s, connect=20.0))
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------ requests
    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": f"{APP_NAME}/{__version__}", "Content-Type": "application/json"}
        api = self.provider.api
        if api == "anthropic":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = ANTHROPIC_VERSION
            if self.settings.workspace_id.strip():
                headers["anthropic-workspace-id"] = self.settings.workspace_id.strip()
        elif api == "gemini":
            headers["x-goog-api-key"] = self.api_key
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path: str, **params: Any) -> Any:
        try:
            response = self._client.get(f"{self.base_url}{path}", headers=self._headers(), params=params or None)
        except httpx.HTTPError as exc:
            raise AssistantError(self._network_error(exc)) from exc
        if response.status_code >= 400:
            raise AssistantError(_explain(response.status_code, _error_text(response), self.provider))
        return response.json()

    def _network_error(self, exc: Exception) -> str:
        if self.provider.local:
            return f"Cannot reach Ollama at {self.base_url} -- is it running? ({exc})"
        return f"Cannot reach {self.provider.label}: {exc}"

    # ------------------------------------------------------------------ models
    def list_models(self) -> list[str]:
        api = self.provider.api
        if api == "anthropic":
            data = self._get("/v1/models", limit=100)
            return [m["id"] for m in data.get("data", []) if m.get("id")]
        if api == "gemini":
            models = []
            data = self._get("/models", pageSize=1000)
            for m in data.get("models", []):
                name = str(m.get("name", "")).removeprefix("models/")
                methods = m.get("supportedGenerationMethods") or []
                if "generateContent" in methods and "gemini" in name and not any(
                    x in name for x in ("embedding", "image", "tts", "audio", "live")
                ):
                    models.append(name)
            return sorted(models, reverse=True)
        if api == "ollama":
            data = self._get("/api/tags")
            return sorted(m["name"] for m in data.get("models", []) if m.get("name"))
        data = self._get("/models")
        ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
        if self.provider.id == "openai":  # the list also has embedding, audio, image, moderation models
            chat = re.compile(r"^(gpt-|o\d|chatgpt-)")
            skip = ("audio", "realtime", "transcribe", "tts", "image", "embedding", "search", "moderation",
                    "instruct", "dall-e", "whisper")  # fmt: skip
            ids = [i for i in ids if chat.match(i) and not any(s in i for s in skip)]
        return sorted(ids)

    # ------------------------------------------------------------------ asking
    def ask(
        self,
        context: PaperContext,
        history: Sequence[ChatMessage],
        question: str,
        on_text: Callable[[str], None],
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Usage:
        """Stream the answer to ``question`` (after ``history``) into ``on_text``; returns the token usage."""
        model = self.settings.model.strip()
        if not model:
            raise AssistantError("Choose a model first (the list is filled from the provider)")
        messages = [*history, ChatMessage("user", question)]
        api = self.provider.api
        if api == "anthropic":
            url, body = f"{self.base_url}/v1/messages", self._anthropic_body(context, messages, model)
        elif api == "gemini":
            url = f"{self.base_url}/models/{model}:streamGenerateContent?alt=sse"
            body = self._gemini_body(context, messages)
        elif api == "ollama":
            url, body = f"{self.base_url}/api/chat", self._ollama_body(context, messages, model)
        else:
            url, body = f"{self.base_url}/chat/completions", self._openai_body(context, messages, model)
        usage = Usage()
        try:
            with self._client.stream("POST", url, headers=self._headers(), json=body) as response:
                if response.status_code >= 400:
                    response.read()
                    raise AssistantError(_explain(response.status_code, _error_text(response), self.provider))
                parse = {"anthropic": self._parse_anthropic, "gemini": self._parse_gemini,
                         "ollama": self._parse_ollama}.get(api, self._parse_openai)  # fmt: skip
                for text in parse(response, usage):
                    if cancelled():
                        break
                    if text:
                        on_text(text)
        except httpx.HTTPError as exc:
            raise AssistantError(self._network_error(exc)) from exc
        return usage

    # -- request bodies
    def _anthropic_body(self, context: PaperContext, messages: list[ChatMessage], model: str) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index == 0:
                parts: list[dict[str, Any]] = []
                if context.pdf_bytes is not None:
                    parts.append({
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf",
                                   "data": base64.b64encode(context.pdf_bytes).decode("ascii")},
                        **({"cache_control": {"type": "ephemeral"}} if not context.text else {}),
                    })  # fmt: skip
                if context.text or context.pdf_bytes is None:
                    parts.append({"type": "text", "text": paper_message(context), "cache_control": {"type": "ephemeral"}})
                parts.append({"type": "text", "text": message.text})
                out.append({"role": "user", "content": parts})
            else:
                out.append({"role": message.role, "content": message.text})
        return {
            "model": model,
            "max_tokens": self.settings.max_tokens,
            "system": system_prompt(context),
            "messages": out,
            "stream": True,
        }

    def _openai_body(self, context: PaperContext, messages: list[ChatMessage], model: str) -> dict[str, Any]:
        system = system_prompt(context)
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for index, message in enumerate(messages):
            if index == 0:
                parts: list[dict[str, Any]] = []
                if context.pdf_bytes is not None:
                    data = base64.b64encode(context.pdf_bytes).decode("ascii")
                    parts.append({"type": "file", "file": {"filename": context.pdf_name or "paper.pdf",
                                                            "file_data": f"data:application/pdf;base64,{data}"}})  # fmt: skip
                if context.text or context.pdf_bytes is None:
                    parts.append({"type": "text", "text": paper_message(context)})
                parts.append({"type": "text", "text": message.text})
                out.append({"role": "user", "content": parts})
            else:
                out.append({"role": message.role, "content": message.text})
        body: dict[str, Any] = {"model": model, "messages": out, "stream": True}
        if self.provider.id == "openai":
            body["stream_options"] = {"include_usage": True}
        return body

    def _gemini_body(self, context: PaperContext, messages: list[ChatMessage]) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            role = "model" if message.role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            if index == 0:
                if context.pdf_bytes is not None:
                    parts.append({"inlineData": {"mimeType": "application/pdf",
                                                 "data": base64.b64encode(context.pdf_bytes).decode("ascii")}})  # fmt: skip
                if context.text or context.pdf_bytes is None:
                    parts.append({"text": paper_message(context)})
            parts.append({"text": message.text})
            contents.append({"role": role, "parts": parts})
        return {
            "systemInstruction": {"parts": [{"text": system_prompt(context)}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self.settings.max_tokens},
        }

    def _ollama_body(self, context: PaperContext, messages: list[ChatMessage], model: str) -> dict[str, Any]:
        system = f"{system_prompt(context)}\n\n{paper_message(context)}"
        out = [{"role": "system", "content": system}] + [{"role": m.role, "content": m.text} for m in messages]
        # Ollama's default context window is small: without num_ctx most of the paper would be dropped silently.
        needed = int(len(system) / 3.5) + sum(len(m.text) for m in messages) // 3 + self.settings.max_tokens + 1024
        num_ctx = 1 << max(13, (needed - 1).bit_length())  # power of two >= 8192
        return {"model": model, "messages": out, "stream": True, "options": {"num_ctx": min(num_ctx, 131072)}}

    # -- stream parsers (yield text pieces, fill usage)
    @staticmethod
    def _sse(response: httpx.Response) -> Iterator[tuple[str, str]]:
        event = ""
        for line in response.iter_lines():
            if not line:
                event = ""
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                yield event, line[5:].strip()

    def _parse_anthropic(self, response: httpx.Response, usage: Usage) -> Iterator[str]:
        for event, data in self._sse(response):
            if not data:
                continue
            payload = json.loads(data)
            kind = payload.get("type", event)
            if kind == "content_block_delta":
                delta = payload.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield delta.get("text", "")
            elif kind == "message_start":
                u = payload.get("message", {}).get("usage", {})
                usage.input_tokens = int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0) or 0)
                usage.cached_tokens = int(u.get("cache_read_input_tokens", 0) or 0)
                usage.input_tokens += usage.cached_tokens
            elif kind == "message_delta":
                usage.output_tokens = int(payload.get("usage", {}).get("output_tokens", usage.output_tokens))
                if payload.get("delta", {}).get("stop_reason") == "max_tokens":
                    yield "\n\n*[The answer reached the length limit set in the settings.]*"
            elif kind == "error":
                raise AssistantError(f"{self.provider.label}: {_error_text_from(payload)}")

    def _parse_openai(self, response: httpx.Response, usage: Usage) -> Iterator[str]:
        for _event, data in self._sse(response):
            if not data or data == "[DONE]":
                continue
            payload = json.loads(data)
            if "error" in payload:
                raise AssistantError(f"{self.provider.label}: {_error_text_from(payload)}")
            for choice in payload.get("choices") or []:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    yield text
                if choice.get("finish_reason") == "length":
                    yield "\n\n*[The answer reached the length limit.]*"
            u = payload.get("usage")
            if u:
                usage.input_tokens = int(u.get("prompt_tokens", 0))
                usage.output_tokens = int(u.get("completion_tokens", 0))
                usage.cached_tokens = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)

    def _parse_gemini(self, response: httpx.Response, usage: Usage) -> Iterator[str]:
        for _event, data in self._sse(response):
            if not data:
                continue
            payload = json.loads(data)
            if "error" in payload:
                raise AssistantError(f"{self.provider.label}: {_error_text_from(payload)}")
            for candidate in payload.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    if part.get("text") and not part.get("thought"):
                        yield part["text"]
                if candidate.get("finishReason") == "MAX_TOKENS":
                    yield "\n\n*[The answer reached the length limit set in the settings.]*"
                elif candidate.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
                    yield f"\n\n*[Gemini stopped the answer: {candidate['finishReason']}.]*"
            u = payload.get("usageMetadata")
            if u:
                usage.input_tokens = int(u.get("promptTokenCount", 0))
                usage.output_tokens = int(u.get("candidatesTokenCount", 0)) + int(u.get("thoughtsTokenCount", 0) or 0)
                usage.cached_tokens = int(u.get("cachedContentTokenCount", 0) or 0)

    def _parse_ollama(self, response: httpx.Response, usage: Usage) -> Iterator[str]:
        for line in response.iter_lines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("error"):
                raise AssistantError(f"Ollama: {payload['error']}")
            text = (payload.get("message") or {}).get("content")
            if text:
                yield text
            if payload.get("done"):
                usage.input_tokens = int(payload.get("prompt_eval_count", 0) or 0)
                usage.output_tokens = int(payload.get("eval_count", 0) or 0)
