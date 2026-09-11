from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import ChallengeKind


class ConnectorError(Exception):
    """Base class for connector failures above the transport details."""


class ConnectorTransportError(ConnectorError):
    """The remote service could not be reached or did not answer in time."""


class ConnectorCredentialsError(ConnectorError):
    """The connector could not authenticate with the provided credentials."""


class ConnectorChallengeRequired(ConnectorError):  # noqa: N818 -- spec name
    """The connector requires a human challenge response before continuing."""

    __slots__ = ("kind",)

    def __init__(self, kind: ChallengeKind) -> None:
        super().__init__(f"connector challenge required: {kind}")
        self.kind = kind


class ConnectorUndecodableError(ConnectorError):
    """The remote payload could not be decoded into the declared contract."""


class ConnectorChildMissingError(ConnectorError):
    """The requested child is unknown to this connector."""


class ConnectorUnsupportedError(ConnectorError):
    """The caller asked the connector for something outside its capabilities."""
