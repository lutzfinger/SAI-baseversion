"""Deliberately restricted web connector for reading pages and submitting forms."""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from app.connectors.base import ConnectorAction, ConnectorDescriptor
from app.connectors.web_config import RestrictedWebPolicy
from app.workers.web_models import RestrictedWebRequest, RestrictedWebResult


class RestrictedWebAccessError(RuntimeError):
    """Raised when a requested web action violates policy or safety constraints."""


class HostResolver(Protocol):
    """Small protocol so tests can replace DNS resolution."""

    def __call__(self, host: str) -> list[str]: ...


class RestrictedWebConnector:
    """Access a narrow allowlist of web pages and simple forms only."""

    def __init__(
        self,
        *,
        policy: RestrictedWebPolicy,
        opener: OpenerDirector | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        self.policy = policy
        self.opener = opener or build_opener(_NoRedirectHandler())
        self.resolver = resolver or _resolve_host_addresses

    def required_actions(self) -> list[ConnectorAction]:
        return [
            ConnectorAction(
                action="connector.web.read_page",
                reason="Reading internet pages requires an explicit domain allowlist.",
            ),
            ConnectorAction(
                action="connector.web.submit_form",
                reason="Form submission requires an explicit URL and field allowlist.",
            ),
        ]

    def describe(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            component_name="connector.restricted-web",
            source_details={
                "read_allowlist_count": len(self.policy.allowed_read_url_prefixes),
                "form_allowlist_count": len(self.policy.allowed_form_url_prefixes),
                "allowed_form_methods": list(self.policy.allowed_form_methods),
                "max_response_bytes": self.policy.max_response_bytes,
                "max_redirects": self.policy.max_redirects,
                "block_private_networks": self.policy.block_private_networks,
            },
        )

    def perform(self, request: RestrictedWebRequest) -> RestrictedWebResult:
        if request.action == "read_page":
            self._ensure_url_allowed(
                request.url,
                allowed_prefixes=self.policy.allowed_read_url_prefixes,
            )
            return self._request_text(
                request=request,
                body=None,
                allowed_prefixes=self.policy.allowed_read_url_prefixes,
            )

        if request.method not in self.policy.allowed_form_methods:
            raise RestrictedWebAccessError(
                f"Form method {request.method} is not allowed by policy."
            )
        self._ensure_url_allowed(
            request.url,
            allowed_prefixes=self.policy.allowed_form_url_prefixes,
        )
        self._ensure_form_fields_allowed(request)
        body = urlencode(request.form_fields).encode("utf-8")
        return self._request_text(
            request=request,
            body=body,
            allowed_prefixes=self.policy.allowed_form_url_prefixes,
        )

    def _request_text(
        self,
        *,
        request: RestrictedWebRequest,
        body: bytes | None,
        allowed_prefixes: list[str],
    ) -> RestrictedWebResult:
        current_url = request.url
        current_method = request.method
        redirect_count = 0
        while True:
            response = self._open_once(
                url=current_url,
                method=current_method,
                body=body,
            )
            status_code = _response_status_code(response)
            location = response.headers.get("Location")
            if status_code in {301, 302, 303, 307, 308} and location:
                if redirect_count >= self.policy.max_redirects:
                    raise RestrictedWebAccessError("Maximum redirect count exceeded.")
                next_url = urljoin(current_url, location)
                self._ensure_url_allowed(next_url, allowed_prefixes=allowed_prefixes)
                current_url = next_url
                current_method = "GET" if status_code == 303 else current_method
                body = None if current_method == "GET" else body
                redirect_count += 1
                continue

            content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower()
            if content_type not in self.policy.allowed_content_types:
                raise RestrictedWebAccessError(
                    f"Content type {content_type or '(missing)'} is not allowed."
                )
            raw_bytes = response.read(self.policy.max_response_bytes + 1)
            truncated = len(raw_bytes) > self.policy.max_response_bytes
            if truncated:
                raw_bytes = raw_bytes[: self.policy.max_response_bytes]
            decoded = raw_bytes.decode("utf-8", errors="replace")
            title, text_excerpt = _extract_title_and_text(
                decoded,
                max_chars=self.policy.max_text_chars,
            )
            return RestrictedWebResult(
                action=request.action,
                requested_url=request.url,
                final_url=response.geturl(),
                status_code=status_code,
                content_type=content_type,
                title=title,
                text_excerpt=text_excerpt,
                submitted_field_names=(
                    sorted(request.form_fields)
                    if request.action == "submit_form"
                    else []
                ),
                redirect_count=redirect_count,
                truncated=truncated or len(text_excerpt) >= self.policy.max_text_chars,
            )

    def _open_once(
        self,
        *,
        url: str,
        method: str,
        body: bytes | None,
    ) -> Any:
        request = Request(
            url=url,
            data=body,
            headers={
                "User-Agent": "SAI-RestrictedWeb/1.0",
                "Accept": "text/html, text/plain;q=0.9",
                **(
                    {"Content-Type": "application/x-www-form-urlencoded"}
                    if body is not None
                    else {}
                ),
            },
            method=method,
        )
        try:
            return self.opener.open(request, timeout=self.policy.timeout_seconds)
        except HTTPError as error:
            if error.code in {301, 302, 303, 307, 308}:
                return error
            raise RestrictedWebAccessError(
                f"HTTP request failed with status {error.code}: {error.reason}"
            ) from error
        except OSError as error:
            raise RestrictedWebAccessError(f"Web request failed: {error}") from error

    def _ensure_url_allowed(self, url: str, *, allowed_prefixes: list[str]) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"}:
            raise RestrictedWebAccessError("Only http(s) URLs are supported.")
        if parsed.username or parsed.password:
            raise RestrictedWebAccessError("URLs with embedded credentials are not allowed.")
        if parsed.scheme == "http" and not any(
            url.startswith(prefix) for prefix in self.policy.allow_insecure_url_prefixes
        ):
            raise RestrictedWebAccessError("Plain HTTP URLs are not allowed by policy.")
        if not any(url.startswith(prefix) for prefix in allowed_prefixes):
            raise RestrictedWebAccessError("URL is not on the policy allowlist.")
        if self.policy.block_private_networks:
            host = parsed.hostname
            if host is None:
                raise RestrictedWebAccessError("URL must contain a hostname.")
            self._ensure_public_host(host)

    def _ensure_public_host(self, host: str) -> None:
        try:
            direct_ip = ipaddress.ip_address(host)
        except ValueError:
            direct_ip = None
        addresses = [str(direct_ip)] if direct_ip is not None else self.resolver(host)
        for raw_address in addresses:
            address = ipaddress.ip_address(raw_address)
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
            ):
                raise RestrictedWebAccessError(
                    f"Host resolves to a non-public address: {raw_address}"
                )

    def _ensure_form_fields_allowed(self, request: RestrictedWebRequest) -> None:
        allowed_fields = _allowed_fields_for_url(
            request.url,
            self.policy.allowed_form_fields_by_url_prefix,
        )
        if allowed_fields is None:
            raise RestrictedWebAccessError(
                "No form field allowlist matches this form action URL."
            )
        unexpected = sorted(set(request.form_fields) - set(allowed_fields))
        if unexpected:
            raise RestrictedWebAccessError(
                f"Form fields are not allowed by policy: {', '.join(unexpected)}"
            )


class _NoRedirectHandler(HTTPRedirectHandler):
    """Disable automatic redirect following so each hop can be policy-checked."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = (
        lambda self, req, fp, code, msg, headers: HTTPError(
            req.full_url,
            code,
            msg,
            headers,
            fp,
        )
    )


def _resolve_host_addresses(host: str) -> list[str]:
    return sorted(
        {
            str(info[4][0])
            for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    )


def _response_status_code(response: Any) -> int:
    status_code = getattr(response, "status", None)
    if isinstance(status_code, int):
        return status_code
    code = response.getcode()
    if isinstance(code, int):
        return code
    raise RestrictedWebAccessError("Response did not contain a valid HTTP status code.")


def _allowed_fields_for_url(
    url: str,
    allowed_fields_by_prefix: dict[str, list[str]],
) -> list[str] | None:
    matching_prefixes = [
        prefix for prefix in allowed_fields_by_prefix if url.startswith(prefix)
    ]
    if not matching_prefixes:
        return None
    best_prefix = max(matching_prefixes, key=len)
    return allowed_fields_by_prefix[best_prefix]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self._ignored_depth = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "title":
            self._in_title = True
        if tag in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style"} and self._ignored_depth > 0:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        self.text_parts.append(cleaned)


def _extract_title_and_text(html_text: str, *, max_chars: int) -> tuple[str | None, str]:
    parser = _TextExtractor()
    parser.feed(html_text)
    title = " ".join(parser.title_parts).strip() or None
    text_excerpt = " ".join(parser.text_parts).strip()
    if len(text_excerpt) > max_chars:
        text_excerpt = text_excerpt[:max_chars].rstrip()
    return title, text_excerpt
