"""House HTTP client contract for EcoleDirecte."""

# ruff: noqa: E501 -- Task 7 requires the three plan tests verbatim.

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs

from aiohttp import ClientPayloadError
import pytest

from custom_components.pronote_ng.connectors.ecoledirecte.ed_client import (
    ECOLEDIRECTE_API_VERSION,
    EcoleDirecteClient,
)
from custom_components.pronote_ng.connectors.errors import (
    ConnectorChallengeRequired,
    ConnectorCredentialsError,
    ConnectorTransportError,
)
from custom_components.pronote_ng.connectors.protocol import ChallengeKind

FIXTURES = Path(__file__).parent / "fixtures" / "ecoledirecte"


def load_fixture(name: str) -> dict[str, Any]:
    """Load one hand-written EcoleDirecte protocol response."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass(slots=True)
class FakeResponse:
    """Minimal response shape consumed by the house client."""

    payload: dict[str, Any]
    headers: dict[str, str]
    cookies: dict[str, str]
    json_delay: float = 0
    json_error: Exception | None = None

    async def json(self, *, content_type: None = None) -> dict[str, Any]:
        """Return the scripted JSON response."""
        await asyncio.sleep(self.json_delay)
        if self.json_error is not None:
            raise self.json_error
        return self.payload


@dataclass(slots=True)
class StaticTransport:
    """Return one response while keeping the HTTP call itself immediate."""

    response: FakeResponse

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> FakeResponse:
        """Return the response whose body behavior is under test."""
        return self.response


class RecordingTransport:
    """Strict scripted transport that records the emitted HTTP contract."""

    def __init__(
        self,
        script: Sequence[tuple[str, str, dict[str, Any]]],
    ) -> None:
        self._script = list(script)
        self.urls: list[str] = []
        self.headers_on: list[dict[str, str]] = []
        self.data_on: list[object] = []

    @classmethod
    def scripted(
        cls,
        script: Sequence[tuple[str, str, dict[str, Any]]],
    ) -> Self:
        """Build a transport from ordered method/path/payload triples."""
        return cls(script)

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> FakeResponse:
        """Record one call and return its exact scripted response."""
        expected_method, expected_path, payload = self._script.pop(0)
        assert method == expected_method
        assert expected_path in url
        headers = kwargs.get("headers", {})
        assert isinstance(headers, Mapping)
        self.urls.append(url)
        self.headers_on.append(dict(headers))
        self.data_on.append(kwargs.get("data"))
        response_headers = (
            {"x-token": "not-a-real-token"} if payload.get("code") in {200, 250} else {}
        )
        cookies = {"GTK": payload["gtk"]} if "gtk" in payload else {}
        return FakeResponse(payload, response_headers, cookies)


@pytest.mark.asyncio
async def test_a_login_fetches_gtk_before_posting_credentials() -> None:
    """Aplim treats a missing GTK as a wrong password, which would trip the IP-like hold."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    await client.login("demo.example.invalid", "not-a-real-password")
    assert transport.urls[0].endswith("gtk=1")
    assert transport.headers_on[1].get("x-gtk") == "gtk-demo"
    assert client.calls == 2
    assert ECOLEDIRECTE_API_VERSION  # named constant used in both URLs


@pytest.mark.asyncio
async def test_a_505_is_credentials_and_not_transport() -> None:
    """The HTTP status is 200; classifying on it would never increment the hold."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_505.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    with pytest.raises(ConnectorCredentialsError):
        await client.login("demo.example.invalid", "wrong")


@pytest.mark.asyncio
async def test_a_250_keeps_the_token_for_the_qcm() -> None:
    """The unofficial docs require the login token to answer the quiz."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_250.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    with pytest.raises(ConnectorChallengeRequired) as caught:
        await client.login("demo.example.invalid", "not-a-real-password")
    assert caught.value.kind is ChallengeKind.QCM
    assert client.token == "not-a-real-token"


@pytest.mark.asyncio
async def test_login_form_encodes_the_complete_json_object() -> None:
    """Quotes and literal percent escapes must survive form decoding as valid JSON."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
        ]
    )
    client = EcoleDirecteClient(transport)

    await client.login('demo"a%2Fb', "not-a-real-password")

    encoded_form = transport.data_on[1]
    assert isinstance(encoded_form, str)
    login_data = json.loads(parse_qs(encoded_form)["data"][0])
    assert login_data == {
        "identifiant": 'demo"a%2Fb',
        "motdepasse": "not-a-real-password",
        "isReLogin": False,
    }


@pytest.mark.asyncio
async def test_a_json_read_timeout_is_a_transport_error() -> None:
    """The read deadline must cover the body, not only response headers."""
    response = FakeResponse(
        {"code": 200},
        {},
        {},
        json_delay=0.02,
    )
    client = EcoleDirecteClient(StaticTransport(response), read_timeout=0.001)

    with pytest.raises(ConnectorTransportError):
        await client.request("/eleves/1/notes.awp", verbe="get", data={})


@pytest.mark.asyncio
async def test_a_payload_read_failure_is_a_transport_error() -> None:
    """An interrupted aiohttp body is a transport failure, not a raw library error."""
    response = FakeResponse(
        {"code": 200},
        {},
        {},
        json_error=ClientPayloadError("scripted body failure"),
    )
    client = EcoleDirecteClient(StaticTransport(response))

    with pytest.raises(ConnectorTransportError):
        await client.request("/eleves/1/notes.awp", verbe="get", data={})
