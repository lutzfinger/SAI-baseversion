"""Last-resort isolated browser executor for newsletter unsubscribes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol
from urllib.parse import urlparse

_ALLOWED_CONTROL_PATTERNS = (
    re.compile(r"\bunsubscribe\b", re.IGNORECASE),
    re.compile(r"\bopt[- ]?out\b", re.IGNORECASE),
    re.compile(r"\bremove me\b", re.IGNORECASE),
    re.compile(r"\bstop emails?\b", re.IGNORECASE),
    re.compile(r"\bstop sending\b", re.IGNORECASE),
)
_BLOCKED_CONTROL_PATTERNS = (
    re.compile(r"\bmanage preferences?\b", re.IGNORECASE),
    re.compile(r"\bemail preferences?\b", re.IGNORECASE),
    re.compile(r"\bupdate preferences?\b", re.IGNORECASE),
    re.compile(r"\bsubscribe\b", re.IGNORECASE),
    re.compile(r"\bresubscribe\b", re.IGNORECASE),
)
_SAFE_SCHEMES = {"about", "data"}
_UNSAFE_VISIBLE_INPUT_TYPES = {
    "",
    "text",
    "search",
    "tel",
    "url",
    "number",
    "date",
    "datetime-local",
    "month",
    "week",
    "time",
    "password",
    "file",
    "checkbox",
    "radio",
}


class BrowserUnsubscribeAccessError(RuntimeError):
    """Raised when the isolated browser flow cannot proceed safely."""


@dataclass(frozen=True)
class BrowserUnsubscribeExecution:
    final_url: str
    text_excerpt: str
    matched_control_label: str
    filled_email_fields: list[str]
    screenshot_paths: list[str]
    blocked_request_urls: list[str]


class BrowserUnsubscribeRunner(Protocol):
    def execute(
        self,
        *,
        target_url: str,
        allowed_origin_prefix: str,
        recipient_email: str,
    ) -> BrowserUnsubscribeExecution: ...


class BrowserUnsubscribeExecutor:
    """Execute a single unsubscribe click in an ephemeral Chromium profile."""

    def __init__(
        self,
        *,
        screenshot_root: Path,
        headless: bool = True,
        timeout_ms: int = 15_000,
    ) -> None:
        self.screenshot_root = screenshot_root
        self.headless = headless
        self.timeout_ms = timeout_ms

    def execute(
        self,
        *,
        target_url: str,
        allowed_origin_prefix: str,
        recipient_email: str,
    ) -> BrowserUnsubscribeExecution:
        screenshot_dir = self._create_screenshot_dir(target_url=target_url)
        return self._execute_playwright(
            target_url=target_url,
            allowed_origin_prefix=allowed_origin_prefix,
            recipient_email=recipient_email,
            screenshot_dir=screenshot_dir,
        )

    def _create_screenshot_dir(self, *, target_url: str) -> Path:
        parsed = urlparse(target_url)
        host = (parsed.hostname or "unsubscribe").replace(".", "-")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        directory = self.screenshot_root / f"{host}_{timestamp}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _execute_playwright(
        self,
        *,
        target_url: str,
        allowed_origin_prefix: str,
        recipient_email: str,
        screenshot_dir: Path,
    ) -> BrowserUnsubscribeExecution:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError(
                "Playwright is not installed. Install it with "
                "`.venv/bin/pip install playwright` and "
                "`.venv/bin/python -m playwright install chromium`."
            ) from error

        blocked_request_urls: list[str] = []
        screenshot_paths: list[str] = []
        with (
            TemporaryDirectory(prefix="sai-unsubscribe-browser-") as profile_dir,
            sync_playwright() as playwright,
        ):
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=self.headless,
                accept_downloads=False,
                args=[
                    "--disable-extensions",
                    "--disable-sync",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-features=AutofillServerCommunication,OptimizationHints",
                    "--no-first-run",
                ],
            )
            context.set_default_timeout(self.timeout_ms)
            context.set_default_navigation_timeout(self.timeout_ms)

            def _route_handler(route: Any, request: Any) -> None:
                url = str(request.url)
                if _request_url_allowed(url=url, allowed_origin_prefix=allowed_origin_prefix):
                    route.continue_()
                    return
                blocked_request_urls.append(url)
                route.abort()

            context.route("**/*", _route_handler)
            page = context.new_page()
            try:
                page.goto(target_url, wait_until="domcontentloaded")
                _ensure_exact_origin(
                    url=str(page.url),
                    allowed_origin_prefix=allowed_origin_prefix,
                )

                before_path = screenshot_dir / "before.png"
                page.screenshot(path=str(before_path), full_page=True)
                screenshot_paths.append(str(before_path))

                unsafe_controls = _visible_unsafe_controls(page)
                if unsafe_controls:
                    raise BrowserUnsubscribeAccessError(
                        "Visible interactive controls were not safe for automated unsubscribe: "
                        + ", ".join(unsafe_controls)
                    )

                filled_email_fields = _fill_visible_email_fields(
                    page,
                    recipient_email=recipient_email,
                )
                control, label = _find_unsubscribe_control(page)
                if control is None or label is None:
                    raise BrowserUnsubscribeAccessError(
                        "No visible unsubscribe-only control was found on the page."
                    )

                control.scroll_into_view_if_needed()
                control.click(timeout=self.timeout_ms)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(750)
                _ensure_exact_origin(
                    url=str(page.url),
                    allowed_origin_prefix=allowed_origin_prefix,
                )

                after_path = screenshot_dir / "after.png"
                page.screenshot(path=str(after_path), full_page=True)
                screenshot_paths.append(str(after_path))

                return BrowserUnsubscribeExecution(
                    final_url=str(page.url),
                    text_excerpt=_page_text(page)[:4000],
                    matched_control_label=label,
                    filled_email_fields=filled_email_fields,
                    screenshot_paths=screenshot_paths,
                    blocked_request_urls=blocked_request_urls,
                )
            finally:
                context.close()


def _request_url_allowed(*, url: str, allowed_origin_prefix: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in _SAFE_SCHEMES:
        return True
    return url.startswith(allowed_origin_prefix)


def _ensure_exact_origin(*, url: str, allowed_origin_prefix: str) -> None:
    if not url.startswith(allowed_origin_prefix):
        raise BrowserUnsubscribeAccessError(
            "Browser navigation left the allowed unsubscribe origin."
        )


def _is_allowed_control_label(label: str) -> bool:
    normalized = re.sub(r"\s+", " ", label).strip()
    if not normalized:
        return False
    if any(pattern.search(normalized) for pattern in _BLOCKED_CONTROL_PATTERNS):
        return False
    return any(pattern.search(normalized) for pattern in _ALLOWED_CONTROL_PATTERNS)


def _visible_unsafe_controls(page: Any) -> list[str]:
    unsafe: list[str] = []
    for selector in ("input", "textarea", "select"):
        locator = page.locator(selector)
        count = min(locator.count(), 64)
        for index in range(count):
            control = locator.nth(index)
            try:
                if not control.is_visible(timeout=200):
                    continue
            except Exception:
                continue
            metadata = control.evaluate(
                """element => ({
                    tag: element.tagName.toLowerCase(),
                    type: (element.getAttribute("type") || "").toLowerCase(),
                    name: element.getAttribute("name") || "",
                    disabled: !!element.disabled,
                    readonly: element.hasAttribute("readonly")
                })"""
            )
            if not isinstance(metadata, dict):
                continue
            tag = str(metadata.get("tag", "")).lower()
            field_name = str(metadata.get("name", "")).strip() or tag
            if tag in {"textarea", "select"}:
                unsafe.append(field_name)
                continue
            input_type = str(metadata.get("type", "")).lower()
            if input_type == "hidden":
                continue
            if _is_email_input(metadata):
                continue
            if input_type in {"submit", "button"}:
                continue
            if input_type in _UNSAFE_VISIBLE_INPUT_TYPES:
                unsafe.append(field_name)
    return unsafe


def _fill_visible_email_fields(page: Any, *, recipient_email: str) -> list[str]:
    filled_fields: list[str] = []
    locator = page.locator("input")
    count = min(locator.count(), 64)
    for index in range(count):
        control = locator.nth(index)
        try:
            if not control.is_visible(timeout=200):
                continue
            if not control.is_enabled(timeout=200):
                continue
        except Exception:
            continue
        metadata = control.evaluate(
            """element => ({
                type: (element.getAttribute("type") || "").toLowerCase(),
                name: element.getAttribute("name") || "",
                readonly: element.hasAttribute("readonly")
            })"""
        )
        if not isinstance(metadata, dict) or metadata.get("readonly"):
            continue
        if not _is_email_input(metadata):
            continue
        control.fill(recipient_email, timeout=1_000)
        field_name = str(metadata.get("name", "")).strip() or "email"
        filled_fields.append(field_name)
    return filled_fields


def _find_unsubscribe_control(page: Any) -> tuple[Any | None, str | None]:
    locator = page.locator("button, a, input[type='submit'], input[type='button']")
    count = min(locator.count(), 64)
    for index in range(count):
        control = locator.nth(index)
        try:
            if not control.is_visible(timeout=200):
                continue
            if not control.is_enabled(timeout=200):
                continue
        except Exception:
            continue
        label = _control_label(control)
        if _is_allowed_control_label(label):
            return control, label
    return None, None


def _control_label(control: Any) -> str:
    metadata = control.evaluate(
        """element => ({
            text: (element.innerText || element.textContent || "").trim(),
            value: (element.getAttribute("value") || "").trim(),
            ariaLabel: (element.getAttribute("aria-label") || "").trim()
        })"""
    )
    if not isinstance(metadata, dict):
        return ""
    for key in ("ariaLabel", "text", "value"):
        value = str(metadata.get(key, "")).strip()
        if value:
            return value
    return ""


def _page_text(page: Any) -> str:
    try:
        return re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
    except Exception:
        return ""


def _is_email_input(metadata: dict[str, Any]) -> bool:
    input_type = str(metadata.get("type", "")).lower()
    field_name = str(metadata.get("name", "")).lower()
    return input_type == "email" or "email" in field_name
