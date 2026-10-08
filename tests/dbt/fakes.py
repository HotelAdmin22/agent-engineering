"""A scripted stand-in for the OpenAI client's Responses API.

Each script step is either a list of function calls [(name, args), ...] or a
final text. The fake records every request so tests can assert on what the
loop sent (model, tools, store, include, text.format, input items).
"""

import json
from types import SimpleNamespace


class _Item(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


class _Usage(SimpleNamespace):
    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    def model_dump(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "input_tokens_details": {"cached_tokens": self.cached},
                "output_tokens_details": {"reasoning_tokens": self.reasoning}}


def response(step, model: str, n: int):
    if isinstance(step, str):
        output = [_Item(type="message", role="assistant",
                        content=[{"type": "output_text", "text": step}])]
        text = step
    else:
        output = [_Item(type="function_call", name=name, arguments=json.dumps(args),
                        call_id=f"call_{n}_{i}", id=f"fc_{n}_{i}")
                  for i, (name, args) in enumerate(step)]
        text = ""
    return SimpleNamespace(
        id=f"resp_{n}", model=f"{model}-echo", status="completed", output=output,
        output_text=text, incomplete_details=None,
        usage=_Usage(input_tokens=1000, output_tokens=100, cached=200, reasoning=10),
    )


class FakeClient:
    """scripts: {model: [step, step, ...]} consumed in order per model."""

    def __init__(self, scripts: dict, raise_for: dict | None = None):
        self.scripts = {m: list(s) for m, s in scripts.items()}
        self.raise_for = raise_for or {}
        self.requests: list[dict] = []
        self.responses = self
        self._n = 0

    def create(self, **kwargs):
        kwargs.pop("timeout", None)
        # A copy: the loop keeps appending to the same input list afterwards.
        self.requests.append({**kwargs, "input": list(kwargs.get("input") or [])})
        model = kwargs["model"]
        if model in self.raise_for:
            raise self.raise_for[model]
        self._n += 1
        script = self.scripts.get(model)
        if not script:
            return response("done", model, self._n)
        return response(script.pop(0), model, self._n)


def api_error(message: str = "boom"):
    """An openai.APIError without constructing transport objects."""
    import openai

    class FakeAPIError(openai.APIError):
        def __init__(self):
            Exception.__init__(self, message)
            self.message = message

    return FakeAPIError()
