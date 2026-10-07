from __future__ import annotations

import html
import ipaddress
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from .config import Settings

_MAX_RESPONSE_BYTES = 2_000_000
_USER_AGENT = "UNNAMED-Operator/0.4 (+local read-only web worker)"


class WebAccessError(RuntimeError):
    pass


def _validate_public_http_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"}:
        raise WebAccessError("Only http/https URLs are allowed.")
    if not parsed.hostname:
        raise WebAccessError("URL is missing a hostname.")
    if parsed.username or parsed.password:
        raise WebAccessError("Credential-bearing URLs are not allowed.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebAccessError(f"Could not resolve web host: {parsed.hostname}") from exc
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if any((addr.is_private, addr.is_loopback, addr.is_link_local, addr.is_multicast, addr.is_reserved, addr.is_unspecified)):
            raise WebAccessError(f"Blocked non-public network destination: {parsed.hostname}")
    return urllib.parse.urlunparse(parsed)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_public(url: str, *, timeout: float = 12.0) -> tuple[str, bytes, str]:
    safe = _validate_public_http_url(url)
    request = urllib.request.Request(safe, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"})
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = _validate_public_http_url(response.geturl())
            content_type = response.headers.get_content_type() or "application/octet-stream"
            if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                raise WebAccessError(f"Unsupported web content type: {content_type}")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                body = body[:_MAX_RESPONSE_BYTES]
            charset = response.headers.get_content_charset() or "utf-8"
            return final_url, body, charset
    except WebAccessError:
        raise
    except Exception as exc:
        raise WebAccessError(f"Web request failed: {exc}") from exc


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            values = dict(attrs)
            self._link_href = values.get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link_href:
            text = " ".join(" ".join(self._link_text).split())
            if text:
                self.links.append({"url": self._link_href, "text": text})
            self._link_href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        clean = " ".join(data.split())
        if not clean:
            return
        if self._in_title:
            self._title_parts.append(clean)
        if self._link_href is not None:
            self._link_text.append(clean)
        self._text_parts.append(clean)

    @property
    def title(self) -> str:
        return " ".join(self._title_parts).strip()

    @property
    def text(self) -> str:
        return "\n".join(self._text_parts)


def web_open(settings: Settings, url: str, max_chars: int | None = None) -> dict[str, Any]:
    if not settings.web_enabled:
        raise WebAccessError("Web access is disabled in Operator settings.")
    final_url, body, charset = _open_public(url)
    text = body.decode(charset, errors="replace")
    cap = max(500, min(int(max_chars or settings.max_read_chars), 50_000))
    if "<html" in text[:2000].casefold() or "<!doctype" in text[:500].casefold():
        parser = _PageParser()
        parser.feed(text)
        content = parser.text
        title = parser.title
        links: list[dict[str, str]] = []
        for item in parser.links:
            absolute = urllib.parse.urljoin(final_url, item["url"])
            parsed = urllib.parse.urlparse(absolute)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                links.append({"url": absolute, "text": item["text"]})
            if len(links) >= 30:
                break
    else:
        content = text
        title = ""
        links = []
    content = html.unescape(content)
    return {"url": final_url, "title": title, "content": content[:cap], "chars": min(len(content), cap), "truncated": len(content) > cap, "links": links}


class _DuckSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture_title = False
        self._capture_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        values = dict(attrs).get("class") or ""
        return set(values.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        values = dict(attrs)
        if tag == "a" and "result__a" in classes:
            if self._current is not None and self._current.get("url") and self._current.get("title"):
                self.results.append(self._current)
            self._current = {"url": values.get("href") or "", "title": "", "snippet": ""}
            self._capture_title = True
            self._title_parts = []
        elif "result__snippet" in classes and self._current is not None:
            self._capture_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title and self._current is not None:
            self._current["title"] = " ".join(" ".join(self._title_parts).split())
            self._capture_title = False
        if self._capture_snippet and tag in {"a", "div", "span"} and self._current is not None:
            self._current["snippet"] = " ".join(" ".join(self._snippet_parts).split())
            self._capture_snippet = False
            if self._current.get("url") and self._current.get("title"):
                self.results.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_parts.append(data)
        if self._capture_snippet:
            self._snippet_parts.append(data)


def _unwrap_duck_url(url: str) -> str:
    absolute = urllib.parse.urljoin("https://html.duckduckgo.com/html/", url)
    parsed = urllib.parse.urlparse(absolute)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return absolute


def web_search(settings: Settings, query: str, limit: int = 5) -> dict[str, Any]:
    if not settings.web_enabled:
        raise WebAccessError("Web access is disabled in Operator settings.")
    clean = " ".join(str(query).split())
    if not clean:
        raise ValueError("Search query cannot be empty.")
    limit = max(1, min(int(limit), 10))
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": clean})
    _final, body, charset = _open_public(url)
    parser = _DuckSearchParser()
    parser.feed(body.decode(charset, errors="replace"))
    results: list[dict[str, str]] = []
    for item in parser.results:
        target = _unwrap_duck_url(item["url"])
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        results.append({"title": item["title"], "url": target, "snippet": item.get("snippet", "")})
        if len(results) >= limit:
            break
    return {"query": clean, "count": len(results), "results": results}
