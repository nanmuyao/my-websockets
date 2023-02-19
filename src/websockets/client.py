from __future__ import annotations

import warnings
from typing import Any, Generator, List, Optional, Sequence

from .datastructures import Headers, MultipleValuesError
from .exceptions import (
    InvalidHandshake,
    InvalidHeader,
    InvalidHeaderValue,
    InvalidStatus,
    InvalidUpgrade,
    NegotiationError,
)
from .extensions import ClientExtensionFactory, Extension
from .headers import (
    build_authorization_basic,
    build_extension,
    build_host,
    build_subprotocol,
    parse_connection,
    parse_extension,
    parse_subprotocol,
    parse_upgrade,
)
from .http11 import Request, Response
from .protocol import CLIENT, CONNECTING, OPEN, Protocol, State
from .typing import (
    ConnectionOption,
    ExtensionHeader,
    LoggerLike,
    Origin,
    Subprotocol,
    UpgradeProtocol,
)
from .uri import WebSocketURI
from .utils import accept_key, generate_key


# See #940 for why lazy_import isn't used here for backwards compatibility.
from .legacy.client import *  # isort:skip  # noqa


__all__ = ["ClientProtocol"]


class ClientProtocol(Protocol):
    """
    Sans-I/O implementation of a WebSocket client connection.

    Args:
        wsuri: URI of the WebSocket server, parsed
            with :func:`~websockets.uri.parse_uri`.
        origin: value of the ``Origin`` header. This is useful when connecting
            to a server that validates the ``Origin`` header to defend against
            Cross-Site WebSocket Hijacking attacks.
        extensions: list of supported extensions, in order in which they
            should be tried.
        subprotocols: list of supported subprotocols, in order of decreasing
            preference.
        state: initial state of the WebSocket connection.
        max_size: maximum size of incoming messages in bytes;
            :obj:`None` to disable the limit.
        logger: logger for this connection;
            defaults to ``logging.getLogger("websockets.client")``;
            see the :doc:`logging guide <../../topics/logging>` for details.

    """

    def __init__(
        self,
        wsuri: WebSocketURI,
        *,
        origin: Optional[Origin] = None,
        extensions: Optional[Sequence[ClientExtensionFactory]] = None,
        subprotocols: Optional[Sequence[Subprotocol]] = None,
        state: State = CONNECTING,
        max_size: Optional[int] = 2**20,
        logger: Optional[LoggerLike] = None,
    ):
        super().__init__(
            side=CLIENT,
            state=state,
            max_size=max_size,
            logger=logger,
        )
        self.wsuri = wsuri
        self.origin = origin
        self.available_extensions = extensions
        self.available_subprotocols = subprotocols
        self.key = generate_key()

    def connect(self) -> Request:  # noqa: F811
        """
        Create a handshake request to open a connection.

        You must send the handshake request with :meth:`send_request`.

        You can modify it before sending it, for example to add HTTP headers.

        Returns:
            Request: WebSocket handshake request event to send to the server.

        """
        headers = Headers()

        headers["Host"] = build_host(
            self.wsuri.host, self.wsuri.port, self.wsuri.secure
        )

        if self.wsuri.user_info:
            headers["Authorization"] = build_authorization_basic(*self.wsuri.user_info)

        if self.origin is not None:
            headers["Origin"] = self.origin

        headers["Upgrade"] = "websocket"
        headers["Connection"] = "Upgrade"
        headers["Sec-WebSocket-Key"] = self.key
        headers["Sec-WebSocket-Version"] = "13"

        if self.available_extensions is not None:
            extensions_header = build_extension(
                [
                    (extension_factory.name, extension_factory.get_request_params())
                    for extension_factory in self.available_extensions
                ]
            )
            headers["Sec-WebSocket-Extensions"] = extensions_header

        if self.available_subprotocols is not None:
            protocol_header = build_subprotocol(self.available_subprotocols)
            headers["Sec-WebSocket-Protocol"] = protocol_header

        return Request(self.wsuri.resource_name, headers)

    def process_response(self, response: Response) -> None:
        """
        Check a handshake response.

        Args:
            request: WebSocket handshake response received from the server.

        Raises:
            InvalidHandshake: if the handshake response is invalid.

        """

        if response.status_code != 101:
            raise InvalidStatus(response)

        headers = response.headers

        connection: List[ConnectionOption] = sum(
            [parse_connection(value) for value in headers.get_all("Connection")], []
        )

        if not any(value.lower() == "upgrade" for value in connection):
            raise InvalidUpgrade(
                "Connection", ", ".join(connection) if connection else None
            )

        upgrade: List[UpgradeProtocol] = sum(
            [parse_upgrade(value) for value in headers.get_all("Upgrade")], []
        )

        # For compatibility with non-strict implementations, ignore case when
        # checking the Upgrade header. It's supposed to be 'WebSocket'.
        if not (len(upgrade) == 1 and upgrade[0].lower() == "websocket"):
            raise InvalidUpgrade("Upgrade", ", ".join(upgrade) if upgrade else None)

        try:
            s_w_accept = headers["Sec-WebSocket-Accept"]
        except KeyError as exc:
            raise InvalidHeader("Sec-WebSocket-Accept") from exc
        except MultipleValuesError as exc:
            raise InvalidHeader(
                "Sec-WebSocket-Accept",
                "more than one Sec-WebSocket-Accept header found",
            ) from exc

        if s_w_accept != accept_key(self.key):
            raise InvalidHeaderValue("Sec-WebSocket-Accept", s_w_accept)

        self.extensions = self.process_extensions(headers)

        self.subprotocol = self.process_subprotocol(headers)

    def process_extensions(self, headers: Headers) -> List[Extension]:
        """
        Handle the Sec-WebSocket-Extensions HTTP response header.

        Check that each extension is supported, as well as its parameters.

        :rfc:`6455` leaves the rules up to the specification of each
        extension.

        To provide this level of flexibility, for each extension accepted by
        the server, we check for a match with each extension available in the
        client configuration. If no match is found, an exception is raised.

        If several variants of the same extension are accepted by the server,
        it may be configured several times, which won't make sense in general.
        Extensions must implement their own requirements. For this purpose,
        the list of previously accepted extensions is provided.

        Other requirements, for example related to mandatory extensions or the
        order of extensions, may be implemented by overriding this method.

        Args:
            headers: WebSocket handshake response headers.

        Returns:
            List[Extension]: List of accepted extensions.

        Raises:
            InvalidHandshake: to abort the handshake.

        """
        accepted_extensions: List[Extension] = []

        extensions = headers.get_all("Sec-WebSocket-Extensions")

        if extensions:

            if self.available_extensions is None:
                raise InvalidHandshake("no extensions supported")

            parsed_extensions: List[ExtensionHeader] = sum(
                [parse_extension(header_value) for header_value in extensions], []
            )

            for name, response_params in parsed_extensions:

                for extension_factory in self.available_extensions:

                    # Skip non-matching extensions based on their name.
                    if extension_factory.name != name:
                        continue

                    # Skip non-matching extensions based on their params.
                    try:
                        extension = extension_factory.process_response_params(
                            response_params, accepted_extensions
                        )
                    except NegotiationError:
                        continue

                    # Add matching extension to the final list.
                    accepted_extensions.append(extension)

                    # Break out of the loop once we have a match.
                    break

                # If we didn't break from the loop, no extension in our list
                # matched what the server sent. Fail the connection.
                else:
                    raise NegotiationError(
                        f"Unsupported extension: "
                        f"name = {name}, params = {response_params}"
                    )

        return accepted_extensions

    def process_subprotocol(self, headers: Headers) -> Optional[Subprotocol]:
        """
        Handle the Sec-WebSocket-Protocol HTTP response header.

        If provided, check that it contains exactly one supported subprotocol.

        Args:
            headers: WebSocket handshake response headers.

        Returns:
           Optional[Subprotocol]: Subprotocol, if one was selected.

        """
        subprotocol: Optional[Subprotocol] = None

        subprotocols = headers.get_all("Sec-WebSocket-Protocol")

        if subprotocols:

            if self.available_subprotocols is None:
                raise InvalidHandshake("no subprotocols supported")

            parsed_subprotocols: Sequence[Subprotocol] = sum(
                [parse_subprotocol(header_value) for header_value in subprotocols], []
            )

            if len(parsed_subprotocols) > 1:
                subprotocols_display = ", ".join(parsed_subprotocols)
                raise InvalidHandshake(f"multiple subprotocols: {subprotocols_display}")

            subprotocol = parsed_subprotocols[0]

            if subprotocol not in self.available_subprotocols:
                raise NegotiationError(f"unsupported subprotocol: {subprotocol}")

        return subprotocol

    def send_request(self, request: Request) -> None:
        """
        Send a handshake request to the server.

        Args:
            request: WebSocket handshake request event.

        """
        if self.debug:
            self.logger.debug("> GET %s HTTP/1.1", request.path)
            for key, value in request.headers.raw_items():
                self.logger.debug("> %s: %s", key, value)

        self.writes.append(request.serialize())

    def parse(self) -> Generator[None, None, None]:
        if self.state is CONNECTING:
            try:
                response = yield from Response.parse(
                    self.reader.read_line,
                    self.reader.read_exact,
                    self.reader.read_to_eof,
                )
            except Exception as exc:
                self.handshake_exc = exc
                self.parser = self.discard()
                next(self.parser)  # start coroutine
                yield

            if self.debug:
                code, phrase = response.status_code, response.reason_phrase
                self.logger.debug("< HTTP/1.1 %d %s", code, phrase)
                for key, value in response.headers.raw_items():
                    self.logger.debug("< %s: %s", key, value)
                if response.body is not None:
                    self.logger.debug("< [body] (%d bytes)", len(response.body))

            try:
                self.process_response(response)
            except InvalidHandshake as exc:
                response._exception = exc
                self.events.append(response)
                self.handshake_exc = exc
                self.parser = self.discard()
                next(self.parser)  # start coroutine
                yield

            assert self.state is CONNECTING
            self.state = OPEN
            self.events.append(response)

        yield from super().parse()


class ClientConnection(ClientProtocol):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "ClientConnection was renamed to ClientProtocol",
            DeprecationWarning,
        )
        super().__init__(*args, **kwargs)
