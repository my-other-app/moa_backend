"""A stand-in for every AI provider: nothing in the suite reaches Google or
Anthropic, and no real key exists here.

`install(monkeypatch)` puts a Hub behind app.core.ai.providers.make. Every
call is recorded (provider, key, model, the whole request, the timeout).
What a model answers is scripted per model id in `hub.script`: a list
taken from the front, its last item repeating -- a dict or a string is the
answer's text, a ProviderError is raised. A model with no script answers
from `hub.answers`, by the schema asked for. `hub.check_errors` maps a key
to the ProviderError its test call raises.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.core.ai import providers
from app.core.ai.base import AIProvider, AIRequest, AIResult, ProviderError


class FakeProvider(AIProvider):
    def __init__(self, hub: "Hub", name: str, api_key: str):
        super().__init__(api_key)
        self.hub = hub
        self.name = name

    async def generate(self, request: AIRequest, *, model: str, timeout: float) -> AIResult:
        self.hub.calls.append(SimpleNamespace(provider=self.name, api_key=self._api_key, model=model, request=request, timeout=timeout))
        queue = self.hub.script.get(model)
        if queue:
            item = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            item = self.hub.default_for(request)
        if isinstance(item, ProviderError):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return AIResult(text=text, model=model, input_tokens=120, output_tokens=40)

    async def check_key(self, *, model: str, timeout: float) -> None:
        self.hub.checks.append(SimpleNamespace(provider=self.name, api_key=self._api_key, model=model))
        error = self.hub.check_errors.get(self._api_key)
        if error is not None:
            raise error

    async def aclose(self) -> None:
        self.hub.closed += 1


class Hub:
    def __init__(self):
        self.calls: list = []
        self.checks: list = []
        self.script: dict = {}
        self.answers: dict = {}
        self.check_errors: dict = {}
        self.closed = 0

    def make(self, name: str, api_key: str) -> FakeProvider:
        return FakeProvider(self, name, api_key)

    def default_for(self, request: AIRequest):
        from app.api.ai import draft, feedback, write

        if request.schema is draft.SCHEMA:
            return self.answers.get("draft", DRAFT)
        if request.schema is write.SCHEMA:
            return self.answers.get("write", {"text": "Join us for a great evening.", "subject": "See you there"})
        if request.schema is feedback.SCHEMA:
            return self.answers.get("feedback", SUMMARY)
        return self.answers.get("text", "OK")

    def prompts(self) -> str:
        """Everything every call sent, as one string: for asserting what never leaves."""
        return "\n".join(f"{c.request.system}\n{c.request.text}" for c in self.calls)


def install(monkeypatch) -> Hub:
    hub = Hub()
    monkeypatch.setattr(providers, "_factory", hub.make)
    return hub


DRAFT = {
    "name": "Hack Night",
    "date": "",
    "start_time": "",
    "end_time": "",
    "duration_hours": None,
    "format": "",
    "venue_name": "",
    "venue_address": "",
    "online_link": "",
    "fee": None,
    "capacity": None,
    "about": "A night of building things.",
    "guidelines": "",
    "category": "",
    "topics": [],
    "interests": [],
    "agenda": [],
    "speakers": [],
    "questions": [],
    "notes": [],
}

SUMMARY = {
    "loved": ["The hands-on session"],
    "complaints": ["The room was too warm"],
    "fix_next": ["Book a bigger room"],
    "sentiment": "positive",
    "quotes": [],
}
