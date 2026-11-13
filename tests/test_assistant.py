"""v0.7: Ask AI -- request formats and streamed answers of each provider (mocked HTTP), context, key storage."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from bibmanager.core import Author, Library, Paper, PaperMetadata
from bibmanager.core.assistant import (
    PROVIDERS,
    AssistantClient,
    AssistantError,
    AssistantSettings,
    ChatMessage,
    build_context,
    web_prompt,
)
from bibmanager.core.config import AppConfig
from bibmanager.core.secrets import KeyStore

from .conftest import CONFIG_PATH


def paper() -> Paper:
    meta = PaperMetadata(title="Phase-field fracture of hydrogels", authors=[Author("Ciambella", "J.")], year=2025,
                         journal="J. Mech. Phys. Solids", doi="10.5555/x")  # fmt: skip
    return Paper(id=7, sha256=None, citation_key="Ciambella_2025", relative_pdf_path=None, meta=meta)


PAGES = ["Introduction. Hydrogels swell.", "We assume incompressibility, $J = 1$.", "Results: the crack arrests."]


def sse(*events: tuple[str, dict[str, Any] | str]) -> bytes:
    out = []
    for name, data in events:
        text = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {name}\n" if name else "") + f"data: {text}\n\n")
    return "".join(out).encode()


class Recorder:
    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def client_for(provider: str, recorder: Recorder, **settings: Any) -> AssistantClient:
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    options = {"model": "test-model"} | settings
    return AssistantClient(AssistantSettings(provider=provider, **options), "secret-key", client=http)


def collect(client: AssistantClient, context: Any, history: list[ChatMessage] | None = None) -> tuple[str, Any]:
    pieces: list[str] = []
    usage = client.ask(context, history or [], "Which assumptions?", pieces.append)
    return "".join(pieces), usage


# --------------------------------------------------------------------------- context
def test_context_marks_pages_includes_supplements_and_truncates() -> None:
    context = build_context(paper(), [("main.pdf", PAGES), ("SI: mmc1", ["Derivation of the Jacobian."])])
    assert "--- page 2 ---\nWe assume incompressibility" in context.text
    assert "===== SI: mmc1 =====" in context.text and context.pages == 4 and not context.truncated
    assert "Title: Phase-field fracture of hydrogels" in context.header and "DOI: 10.5555/x" in context.header
    short = build_context(paper(), [("main.pdf", ["x" * 30_000, "y" * 30_000])], max_chars=40_000)
    assert short.truncated and "text truncated here: pages 2-2" in short.text and len(short.text) < 45_000
    as_file = build_context(paper(), [("main.pdf", PAGES), ("SI", ["si text"])], pdf_bytes=b"%PDF-1.7", pdf_name="m.pdf")
    assert "main article" not in as_file.text and "si text" in as_file.text and as_file.sources[0] == "main.pdf (PDF file)"
    assert "I attach the PDF" in web_prompt(paper(), "What is J?") and "Question: What is J?" in web_prompt(paper(), "What is J?")


# --------------------------------------------------------------------------- providers
def test_claude_request_caches_the_paper_and_streams_the_answer() -> None:
    recorder = Recorder(lambda r: httpx.Response(200, content=sse(
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 50, "cache_read_input_tokens": 9000}}}),
        ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Incompressibility "}}),
        ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "(p. 2)."}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 6}}),
        ("message_stop", {"type": "message_stop"}),
    )))  # fmt: skip
    client = client_for("anthropic", recorder, workspace_id="wrkspc_1")
    context = build_context(paper(), [("main.pdf", PAGES)])
    history = [ChatMessage("user", "Summarise."), ChatMessage("assistant", "It is about gels.")]
    text, usage = collect(client, context, history)
    assert text == "Incompressibility (p. 2)." and (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (9050, 9000, 6)
    request = recorder.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "secret-key" and request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["anthropic-workspace-id"] == "wrkspc_1"
    body = recorder.body
    assert body["model"] == "test-model" and body["stream"] is True and "(p. N)" in body["system"]
    first = body["messages"][0]["content"]
    assert first[0]["cache_control"] == {"type": "ephemeral"} and "--- page 1 ---" in first[0]["text"]
    assert first[1]["text"] == "Summarise." and body["messages"][1] == {"role": "assistant", "content": "It is about gels."}
    assert body["messages"][2] == {"role": "user", "content": "Which assumptions?"}

    as_pdf = build_context(paper(), [("main.pdf", PAGES)], pdf_bytes=b"%PDF-1.7 bytes", pdf_name="main.pdf")
    collect(client, as_pdf)
    document = recorder.body["messages"][0]["content"][0]
    assert document["type"] == "document" and document["source"]["media_type"] == "application/pdf"
    assert document["cache_control"] == {"type": "ephemeral"}


def test_openai_request_with_pdf_file_and_usage() -> None:
    recorder = Recorder(lambda r: httpx.Response(200, content=sse(
        ("", {"choices": [{"delta": {"role": "assistant", "content": ""}}]}),
        ("", {"choices": [{"delta": {"content": "Three "}}]}),
        ("", {"choices": [{"delta": {"content": "assumptions."}, "finish_reason": "stop"}]}),
        ("", {"choices": [], "usage": {"prompt_tokens": 1200, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 1024}}}),
        ("", "[DONE]"),
    )))  # fmt: skip
    client = client_for("openai", recorder)
    context = build_context(paper(), [("main.pdf", PAGES)], pdf_bytes=b"%PDF-1.7", pdf_name="Ciambella_2025.pdf")
    text, usage = collect(client, context)
    assert text == "Three assumptions." and (usage.input_tokens, usage.cached_tokens) == (1200, 1024)
    request = recorder.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-key"
    body = recorder.body
    assert body["messages"][0]["role"] == "system" and body["stream_options"] == {"include_usage": True}
    file_part = body["messages"][1]["content"][0]
    assert file_part["type"] == "file" and file_part["file"]["filename"] == "Ciambella_2025.pdf"
    assert file_part["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_gemini_request_and_thoughts_are_not_shown() -> None:
    recorder = Recorder(lambda r: httpx.Response(200, content=sse(
        ("", {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}], "role": "model"}}]}),
        ("", {"candidates": [{"content": {"parts": [{"text": "The crack arrests (p. 3)."}], "role": "model"},
                              "finishReason": "STOP"}],
              "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 9, "thoughtsTokenCount": 20}}),
    )))  # fmt: skip
    client = client_for("gemini", recorder, model="gemini-test")
    text, usage = collect(client, build_context(paper(), [("main.pdf", PAGES)]), [ChatMessage("user", "Hi"), ChatMessage("assistant", "Hello")])
    assert text == "The crack arrests (p. 3)." and (usage.input_tokens, usage.output_tokens) == (800, 29)
    request = recorder.requests[0]
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:streamGenerateContent?alt=sse")
    assert request.headers["x-goog-api-key"] == "secret-key"
    body = recorder.body
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert "--- page 1 ---" in body["contents"][0]["parts"][0]["text"] and body["contents"][0]["parts"][1]["text"] == "Hi"
    assert "systemInstruction" in body and body["generationConfig"]["maxOutputTokens"] == 4096


def test_ollama_runs_without_a_key_and_sets_a_large_enough_context_window() -> None:
    lines = [{"message": {"content": "Local "}, "done": False}, {"message": {"content": "answer."}, "done": False},
             {"done": True, "prompt_eval_count": 7000, "eval_count": 2}]  # fmt: skip
    recorder = Recorder(lambda r: httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode()))
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    client = AssistantClient(AssistantSettings(provider="ollama", model="llama-test"), "", client=http)
    context = build_context(paper(), [("main.pdf", ["word " * 20_000])])
    text, usage = collect(client, context)
    assert text == "Local answer." and usage.input_tokens == 7000
    assert str(recorder.requests[0].url) == "http://localhost:11434/api/chat"
    assert "authorization" not in recorder.requests[0].headers
    assert recorder.body["options"]["num_ctx"] >= 32768 and recorder.body["messages"][0]["role"] == "system"


def test_model_lists_are_filtered_to_chat_models() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.openai.com"):
            ids = ["gpt-x", "gpt-x-mini", "o9", "text-embedding-3-large", "gpt-x-audio-preview", "dall-e-3", "whisper-1"]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if url.startswith("https://api.anthropic.com"):
            return httpx.Response(200, json={"data": [{"id": "claude-a"}, {"id": "claude-b"}]})
        if "generativelanguage" in url:
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-pro-x", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                {"name": "models/gemini-embedding-x", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-x-image", "supportedGenerationMethods": ["generateContent"]},
            ]})  # fmt: skip
        return httpx.Response(200, json={"models": [{"name": "qwen-test:14b"}, {"name": "llama-test"}]})

    recorder = Recorder(respond)
    assert client_for("openai", recorder).list_models() == ["gpt-x", "gpt-x-mini", "o9"]
    assert client_for("anthropic", recorder).list_models() == ["claude-a", "claude-b"]
    assert client_for("gemini", recorder).list_models() == ["gemini-pro-x"]
    assert client_for("ollama", recorder).list_models() == ["llama-test", "qwen-test:14b"]


def test_errors_are_explained() -> None:
    refused = Recorder(lambda r: httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                                         "message": "invalid x-api-key"}}))  # fmt: skip
    with pytest.raises(AssistantError, match="refused the API key .401.: invalid x-api-key"):
        collect(client_for("anthropic", refused), build_context(paper(), [("m", PAGES)]))
    quota = Recorder(lambda r: httpx.Response(429, json=[{"error": {"code": 429, "message": "Quota exceeded"}}]))
    with pytest.raises(AssistantError, match="Rate limit or quota .* Quota exceeded"):
        collect(client_for("gemini", quota), build_context(paper(), [("m", PAGES)]))
    midway = Recorder(lambda r: httpx.Response(200, content=sse(
        ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))))  # fmt: skip
    with pytest.raises(AssistantError, match="Overloaded"):
        collect(client_for("anthropic", midway), build_context(paper(), [("m", PAGES)]))

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http = httpx.Client(transport=httpx.MockTransport(offline))
    local = AssistantClient(AssistantSettings(provider="ollama", model="m"), client=http)
    with pytest.raises(AssistantError, match="is it running"):
        local.list_models()
    with pytest.raises(AssistantError, match="No API key"):
        AssistantClient(AssistantSettings(provider="openai", model="m"), "")
    with pytest.raises(AssistantError, match="address of the server"):
        AssistantClient(AssistantSettings(provider="compatible", model="m"), "")
    with pytest.raises(AssistantError, match="Choose a model"):
        client_for("openai", refused, model="").ask(build_context(paper(), [("m", PAGES)]), [], "q", print)


def test_a_cancelled_answer_stops_streaming() -> None:
    pieces = [("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": f"w{i} "}})
              for i in range(50)]  # fmt: skip
    recorder = Recorder(lambda r: httpx.Response(200, content=sse(*pieces)))
    received: list[str] = []
    client_for("anthropic", recorder).ask(
        build_context(paper(), [("m", PAGES)]), [], "q", received.append, cancelled=lambda: len(received) >= 3
    )
    assert received == ["w0 ", "w1 ", "w2 "]


def test_compatible_server_uses_its_own_address(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(lambda r: httpx.Response(200, content=sse(("", {"choices": [{"delta": {"content": "ok"}}]}), ("", "[DONE]"))))
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    client = AssistantClient(AssistantSettings(provider="compatible", model="mistral-x", base_url="https://llm.example.edu/v1/"),
                             "", client=http)  # fmt: skip
    assert collect(client, build_context(paper(), [("m", PAGES)]))[0] == "ok"
    assert str(recorder.requests[0].url) == "https://llm.example.edu/v1/chat/completions"
    assert "stream_options" not in recorder.body  # not every compatible server knows it
    assert set(PROVIDERS) == {"anthropic", "openai", "gemini", "ollama", "compatible"}


# --------------------------------------------------------------------------- keys and settings
def test_keys_are_stored_outside_the_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "from-environment")
    store = KeyStore(tmp_path, use_keyring=False)
    assert store.get("gemini") == "from-environment" and not store.is_stored("gemini")
    store.set("gemini", "  stored-key ")
    assert store.get("gemini") == "stored-key" and store.is_stored("gemini")
    raw = (tmp_path / "secrets.json").read_text()
    assert "stored-key" not in raw  # encoded (DPAPI-encrypted on Windows)
    if not __import__("sys").platform.startswith("win"):
        assert (tmp_path / "secrets.json").stat().st_mode & 0o077 == 0
    store.set("gemini", "")
    assert store.get("gemini") == "from-environment"
    assert KeyStore(tmp_path, use_keyring=False, use_environment=False).get("gemini") == ""


def test_assistant_settings_round_trip_through_the_user_file(tmp_path: Path) -> None:
    config = AppConfig.load(CONFIG_PATH, library_root=tmp_path / "lib")
    assert config.assistant_provider == "anthropic" and not config.assistant_send_pdf
    config.assistant_provider = "gemini"
    config.assistant_models = {"gemini": "gemini-x", "anthropic": "claude-y"}
    config.assistant_send_pdf = True
    config.assistant_acknowledged = ("gemini",)
    path = config.save_user_settings(tmp_path / "user.json")
    assert "secret" not in path.read_text()
    loaded = AppConfig.load(path)
    assert (loaded.assistant_provider, loaded.assistant_models["gemini"], loaded.assistant_send_pdf) == ("gemini", "gemini-x", True)
    assert loaded.assistant_acknowledged == ("gemini",)


def test_library_provides_the_texts_of_main_and_supplementary_pdfs(
    library: Library, make_pdf: Callable[..., Path]
) -> None:
    meta = PaperMetadata(title="Gels", authors=[Author("Anand", "L.")], year=2012)
    pid = library.ingest_pdf(make_pdf("m.pdf", lines=["main text here"]), metadata=meta).paper.id
    library.add_supplement(pid, library.prepare_attachment(make_pdf("s.pdf", lines=["supplement text"])), mode="copy")
    sources = library.fulltext_sources(pid)
    assert [p.name for p in sources] == ["Anand_2012.pdf", "Anand_2012_S1.pdf"]
