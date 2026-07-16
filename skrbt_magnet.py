#!/usr/bin/env python3
"""从 SkrBT 搜索结果的详情页提取磁力链接。"""

from __future__ import annotations

import argparse
import gzip
import html
import os
import re
import socket
import sys
import threading
import time
import zlib
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from getpass import getpass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener


DEFAULT_BASE_URL = "https://skrbtso.top"
DEFAULT_COOKIE_FILE = Path("cookie.txt")
DEFAULT_LIMIT = 120
DEFAULT_WORKERS = 8
DEFAULT_SEARCH_WORKERS = 1
DEFAULT_RETRIES = 3
DEFAULT_DELAY = 0.2
MAX_RETRY_DELAY = 30.0
# 站点搜索页通常大约每页 10 条；翻页上限按此估算并留一点余量。
DEFAULT_RESULTS_PER_PAGE = 10
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

SIZE_RE = re.compile(
    r"(?<![\w.])"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>TiB|TB|GiB|GB|MiB|MB|KiB|KB|T|G|M|K|B)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
SIZE_ARGUMENT_RE = re.compile(
    r"^\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>TiB|TB|GiB|GB|MiB|MB|KiB|KB|T|G|M|K|B)?\s*$",
    re.IGNORECASE,
)
UNIT_MULTIPLIERS = {
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
    "TIB": 1024**4,
}

MAGNET_CANDIDATE_RE = re.compile(
    r"magnet:\?xt=urn:btih:[^\s\"'<>]+",
    re.IGNORECASE,
)
BTIH_RE = re.compile(
    r"(?:\?|&)xt=urn:btih:"
    r"(?P<hash>[A-Fa-f0-9]{40}|[A-Za-z2-7]{32})(?:&|$)",
    re.IGNORECASE,
)
FILE_EXTENSION_RE = re.compile(
    r"\.(?:"
    r"mkv|mp4|avi|mov|wmv|flv|ts|m2ts|mpg|mpeg|webm|rm|rmvb|iso|vob|"
    r"mp3|flac|aac|wav|ape|m4a|ogg|srt|ass|ssa|sub|"
    r"zip|rar|7z|tar|gz|bz2|xz|pdf|epub|mobi|azw3|"
    r"exe|msi|apk|dmg|img|bin|cue"
    r")(?:\b|$)",
    re.IGNORECASE,
)
FILE_MARKER_RE = re.compile(
    r"(?:^|[\s_-])(?:file|fileitem|file-item|file_row|file-row|"
    r"filelist|file-list|files-list)(?:$|[\s_-])",
    re.IGNORECASE,
)
SUMMARY_SIZE_RE = re.compile(
    r"(?:文件|资源|种子|内容|总体|总计|合计)\s*(?:总)?(?:大小|尺寸|体积|容量)"
    r"|总(?:大小|尺寸|体积|容量)",
    re.IGNORECASE,
)
CHALLENGE_MARKERS = (
    "/recaptcha/v4/challenge",
    "cf-chl-",
    "challenge-platform",
    "just a moment...",
    "cloudflare ray id",
)


class ScraperError(RuntimeError):
    """可向用户展示的抓取错误。"""


class AccessChallengeError(ScraperError):
    """站点要求 Cloudflare 或验证码验证。"""


@dataclass(frozen=True)
class SearchResult:
    title: str
    href: str
    total_size: int | None = None


@dataclass(frozen=True)
class BlockCandidate:
    tag: str
    marker: str
    text: str
    has_child_block: bool


@dataclass(frozen=True)
class DetailPage:
    title: str
    magnets: tuple[str, ...]
    blocks: tuple[BlockCandidate, ...]


@dataclass(frozen=True)
class SizeDecision:
    keep: bool
    known: bool
    reason: str
    individual_sizes: tuple[int, ...] = ()


@dataclass
class _AnchorState:
    href: str
    classes: set[str]
    parts: list[str] = field(default_factory=list)
    depth: int = 1


@dataclass
class _SearchRowState:
    anchors: list[tuple[str, set[str], str]] = field(default_factory=list)
    metadata: list[str] = field(default_factory=list)


@dataclass
class _OpenBlock:
    tag: str
    marker: str
    parts: list[str] = field(default_factory=list)
    has_child_block: bool = False


def normalize_space(value: str) -> str:
    return " ".join(value.split())


def _size_to_bytes(number: str, unit: str) -> int:
    value = float(number.replace(",", ""))
    return int(value * UNIT_MULTIPLIERS[unit.upper()])


def find_sizes(value: str) -> list[int]:
    """提取文本中带单位的文件大小。"""
    return [
        _size_to_bytes(match.group("number"), match.group("unit"))
        for match in SIZE_RE.finditer(value)
    ]


def parse_size_argument(value: str) -> int:
    """解析 600M、1.5GB 等命令行大小；省略单位时按 MiB。"""
    match = SIZE_ARGUMENT_RE.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"无效大小 {value!r}，示例：600M、1.5GB"
        )
    unit = match.group("unit") or "M"
    return _size_to_bytes(match.group("number"), unit)


def format_sofs_token(size: int) -> str:
    """把字节数转成站点 sofs 使用的 token，如 1gb、100mb。"""
    if size <= 0:
        raise ScraperError("大小必须大于 0")
    for multiplier, unit in (
        (1024**4, "tb"),
        (1024**3, "gb"),
        (1024**2, "mb"),
        (1024, "kb"),
    ):
        if size % multiplier == 0:
            return f"{size // multiplier}{unit}"
    # 非整除时落到 MB，并至少为 1。
    return f"{max(1, round(size / (1024**2)))}mb"


def build_sofs_filter(
    size_min: int | None = None,
    size_max: int | None = None,
) -> str:
    """
    生成站点 sofs 参数。

    示例：
      size_min=1GiB, size_max=5GiB → gt1gb-lt5gb
      size_min=100MiB, size_max=500MiB → gt100mb-lt500mb
      size_min=5GiB → gt5gb
    """
    if size_min is None and size_max is None:
        raise ScraperError("至少指定 --size-min 或 --size-max 之一")
    if size_min is not None and size_max is not None and size_min >= size_max:
        raise ScraperError("--size-min 必须小于 --size-max")

    parts: list[str] = []
    if size_min is not None:
        parts.append(f"gt{format_sofs_token(size_min)}")
    if size_max is not None:
        parts.append(f"lt{format_sofs_token(size_max)}")
    return "-".join(parts)


def resolve_sofs(args: argparse.Namespace) -> str:
    has_range = args.size_min is not None or args.size_max is not None
    if args.sofs is not None and has_range:
        raise ScraperError("请只使用 --sofs，或使用 --size-min/--size-max，不要混用")
    if has_range:
        return build_sofs_filter(args.size_min, args.size_max)
    if args.sofs is not None:
        return args.sofs.strip() or "all"
    return "gt600mb"


def format_size(size: int) -> str:
    for unit, multiplier in (
        ("TiB", 1024**4),
        ("GiB", 1024**3),
        ("MiB", 1024**2),
        ("KiB", 1024),
    ):
        if size >= multiplier:
            return f"{size / multiplier:.2f} {unit}"
    return f"{size} B"


def normalize_magnet(value: str) -> str | None:
    candidate = html.unescape(value.strip()).replace("\\u0026", "&")
    if candidate.lower().startswith("magnet%3a"):
        candidate = unquote(candidate)
    candidate = candidate.rstrip(".,;")
    if not candidate.lower().startswith("magnet:?"):
        return None
    if not BTIH_RE.search(candidate):
        return None
    return candidate


def magnet_key(value: str) -> str:
    match = BTIH_RE.search(value)
    return match.group("hash").upper() if match else value


class SearchPageParser(HTMLParser):
    """解析已知的 a.rrt 搜索结果结构。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_SearchRowState] = []
        self.all_anchors: list[tuple[str, set[str], str]] = []
        self._row_stack: list[_SearchRowState] = []
        self._anchor: _AnchorState | None = None
        self._span_parts: list[str] | None = None
        self._span_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attributes = {name.lower(): value or "" for name, value in attrs}

        if self._anchor is not None:
            self._anchor.depth += 1
        elif tag == "a":
            self._anchor = _AnchorState(
                href=attributes.get("href", ""),
                classes=set(attributes.get("class", "").split()),
            )

        if self._span_parts is not None:
            self._span_depth += 1
        elif tag == "span" and "rrmiv" in attributes.get("class", "").split():
            self._span_parts = []
            self._span_depth = 1

        if tag == "ul":
            self._row_stack.append(_SearchRowState())

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor.parts.append(data)
        if self._span_parts is not None:
            self._span_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if self._anchor is not None:
            self._anchor.depth -= 1
            if self._anchor.depth == 0:
                anchor = (
                    self._anchor.href,
                    self._anchor.classes,
                    normalize_space(" ".join(self._anchor.parts)),
                )
                self.all_anchors.append(anchor)
                if self._row_stack:
                    self._row_stack[-1].anchors.append(anchor)
                self._anchor = None

        if self._span_parts is not None:
            self._span_depth -= 1
            if self._span_depth == 0:
                if self._row_stack:
                    self._row_stack[-1].metadata.append(
                        normalize_space(" ".join(self._span_parts))
                    )
                self._span_parts = None

        if tag == "ul" and self._row_stack:
            self.rows.append(self._row_stack.pop())

    def results(self) -> list[SearchResult]:
        parsed: list[SearchResult] = []
        for row in self.rows:
            if any("/play/video/" in href for href, _, _ in row.anchors):
                continue
            targets = [
                anchor for anchor in row.anchors if "rrt" in anchor[1]
            ]
            if not targets:
                continue
            href, _, title = targets[0]
            sizes = find_sizes(row.metadata[0]) if row.metadata else []
            parsed.append(
                SearchResult(
                    title=title,
                    href=href,
                    total_size=sizes[0] if sizes else None,
                )
            )

        if not parsed:
            for href, classes, title in self.all_anchors:
                if "rrt" in classes and "/play/video/" not in href:
                    parsed.append(SearchResult(title=title, href=href))

        unique: list[SearchResult] = []
        seen: set[str] = set()
        for result in parsed:
            if result.href and result.href not in seen:
                seen.add(result.href)
                unique.append(result)
        return unique


class DetailPageParser(HTMLParser):
    """提取详情页标题、磁力链接和可能的文件列表行。"""

    _BLOCK_TAGS = {"li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.magnets: list[str] = []
        self.blocks: list[BlockCandidate] = []
        self.title = ""
        self._title_parts: list[str] | None = None
        self._title_depth = 0
        self._blocks: list[_OpenBlock] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attributes = {name.lower(): value or "" for name, value in attrs}
        marker = " ".join(
            part
            for part in (
                attributes.get("class", ""),
                attributes.get("id", ""),
                attributes.get("role", ""),
            )
            if part
        )

        if self._title_parts is not None:
            self._title_depth += 1
        elif tag == "h3" and not self.title:
            self._title_parts = []
            self._title_depth = 1

        href = attributes.get("href", "")
        magnet = normalize_magnet(href)
        if magnet:
            self.magnets.append(magnet)

        capture_div = tag in {"div", "p"} and bool(FILE_MARKER_RE.search(marker))
        if tag in self._BLOCK_TAGS or capture_div:
            if self._blocks:
                self._blocks[-1].has_child_block = True
            self._blocks.append(_OpenBlock(tag=tag, marker=marker))

        extra_text = " ".join(
            attributes.get(name, "")
            for name in ("title", "aria-label", "data-size")
            if attributes.get(name)
        )
        if extra_text:
            for block in self._blocks:
                block.parts.append(extra_text)

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        for block in self._blocks:
            block.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._title_parts is not None:
            self._title_depth -= 1
            if self._title_depth == 0:
                self.title = normalize_space(" ".join(self._title_parts))
                self._title_parts = None

        for index in range(len(self._blocks) - 1, -1, -1):
            block = self._blocks[index]
            if block.tag != tag:
                continue
            self.blocks.append(
                BlockCandidate(
                    tag=block.tag,
                    marker=block.marker,
                    text=normalize_space(" ".join(block.parts)),
                    has_child_block=block.has_child_block,
                )
            )
            del self._blocks[index]
            break


def parse_search_page(document: str) -> list[SearchResult]:
    parser = SearchPageParser()
    parser.feed(document)
    parser.close()
    return parser.results()


def parse_detail_page(document: str) -> DetailPage:
    parser = DetailPageParser()
    parser.feed(document)
    parser.close()

    candidates = list(parser.magnets)
    decoded_document = html.unescape(document).replace("\\u0026", "&")
    candidates.extend(
        match.group(0) for match in MAGNET_CANDIDATE_RE.finditer(decoded_document)
    )

    magnets: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        magnet = normalize_magnet(value)
        if not magnet:
            continue
        key = magnet_key(magnet)
        if key not in seen:
            seen.add(key)
            magnets.append(magnet)

    return DetailPage(
        title=parser.title,
        magnets=tuple(magnets),
        blocks=tuple(parser.blocks),
    )


def extract_individual_file_sizes(
    blocks: tuple[BlockCandidate, ...],
) -> list[int]:
    """从文件行中提取大小，并尽量排除“种子总大小”等汇总值。"""
    strong: list[int] = []
    weak: list[int] = []

    for block in blocks:
        if block.has_child_block:
            continue
        sizes = find_sizes(block.text)
        if not sizes:
            continue

        file_hint = bool(
            FILE_EXTENSION_RE.search(block.text)
            or FILE_MARKER_RE.search(block.marker)
        )
        if SUMMARY_SIZE_RE.search(block.text) and not file_hint:
            continue

        if file_hint:
            strong.extend(sizes)
        else:
            weak.extend(sizes)

    return strong if strong else weak


def decide_by_size(
    detail: DetailPage,
    search_total_size: int | None,
    minimum: int,
    skip_unknown: bool,
    match_total: bool = False,
) -> SizeDecision:
    if minimum <= 0:
        return SizeDecision(
            keep=True,
            known=True,
            reason="已关闭大小过滤（--min-file-size 0）",
        )

    individual = extract_individual_file_sizes(detail.blocks)

    # 按种子总大小判断：搜索页总大小或详情页汇总值。
    if match_total:
        total = search_total_size
        if total is None:
            for block in detail.blocks:
                if SUMMARY_SIZE_RE.search(block.text):
                    sizes = find_sizes(block.text)
                    if sizes:
                        total = max(sizes)
                        break
            if total is None and individual:
                total = sum(individual)
        if total is None:
            if skip_unknown:
                return SizeDecision(
                    keep=False,
                    known=False,
                    reason="无法识别资源总大小",
                )
            return SizeDecision(
                keep=True,
                known=False,
                reason="无法识别资源总大小，按保守策略保留",
            )
        if total >= minimum:
            return SizeDecision(
                keep=True,
                known=True,
                reason=f"资源总大小 {format_size(total)}",
            )
        return SizeDecision(
            keep=False,
            known=True,
            reason=f"资源总大小仅 {format_size(total)}",
        )

    if individual:
        largest = max(individual)
        if largest >= minimum:
            return SizeDecision(
                keep=True,
                known=True,
                reason=f"最大单文件 {format_size(largest)}",
                individual_sizes=tuple(individual),
            )
        return SizeDecision(
            keep=False,
            known=True,
            reason=(
                f"识别到的 {len(individual)} 个文件均小于 "
                f"{format_size(minimum)}"
            ),
            individual_sizes=tuple(individual),
        )

    # 总大小小于阈值时，可以确定其中不可能存在达到阈值的单文件。
    if search_total_size is not None and search_total_size < minimum:
        return SizeDecision(
            keep=False,
            known=True,
            reason=f"资源总大小仅 {format_size(search_total_size)}",
        )

    if skip_unknown:
        return SizeDecision(
            keep=False,
            known=False,
            reason="详情页未识别出单文件大小",
        )
    return SizeDecision(
        keep=True,
        known=False,
        reason="详情页未识别出单文件大小，按保守策略保留",
    )


@dataclass(frozen=True)
class FetchResult:
    document: str
    final_url: str


@dataclass(frozen=True)
class QueuedDetail:
    index: int
    result: SearchResult
    referer: str


@dataclass
class DetailOutcome:
    index: int
    title: str
    magnets: tuple[str, ...] = ()
    filtered: bool = False
    filter_reason: str = ""
    missing_magnet: bool = False
    error: str = ""
    keep_reason: str = ""
    known_size: bool = False


class RateLimiter:
    """线程安全的最小请求间隔控制。"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._condition = threading.Condition()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._condition:
            while True:
                now = time.monotonic()
                wait = self._next_at - now
                if wait <= 0:
                    self._next_at = now + self.delay
                    return
                # Condition.wait 会释放锁，使 429/503 能立即延长冷却窗口。
                self._condition.wait(timeout=wait)

    def defer(self, seconds: float) -> None:
        """遇到限流或服务端错误时，让所有请求共享同一个冷却窗口。"""
        if seconds <= 0:
            return
        with self._condition:
            self._next_at = max(self._next_at, time.monotonic() + seconds)
            self._condition.notify_all()


class HttpClient:
    """线程安全的 HTTP 客户端：Cookie 作为静态头发送，避免共享 CookieJar 竞态。"""

    def __init__(
        self,
        base_url: str,
        cookie_header: str,
        user_agent: str,
        timeout: float,
        retries: int,
        delay: float,
        show_curl_on_error: bool = False,
    ) -> None:
        parsed = urlsplit(base_url)
        self.hostname = parsed.hostname or ""
        self.secure = parsed.scheme == "https"
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.cookie_header = cookie_header.strip()
        self.show_curl_on_error = show_curl_on_error
        self.rate_limiter = RateLimiter(delay)
        self._local = threading.local()

    def _opener(self):
        opener = getattr(self._local, "opener", None)
        if opener is None:
            # 每线程独立 opener，避免 urllib 内部状态交叉。
            opener = build_opener()
            self._local.opener = opener
        return opener

    @staticmethod
    def _decode_body(body: bytes, headers: object) -> str:
        content_encoding = getattr(headers, "get", lambda *_: "")(
            "Content-Encoding", ""
        ).lower()
        try:
            if content_encoding == "gzip":
                body = gzip.decompress(body)
            elif content_encoding == "deflate":
                body = zlib.decompress(body)
        except (OSError, zlib.error) as exc:
            raise ScraperError(f"响应解压失败：{exc}") from exc

        get_charset = getattr(headers, "get_content_charset", None)
        charset = get_charset() if callable(get_charset) else None
        for encoding in (charset, "utf-8", "gb18030"):
            if not encoding:
                continue
            try:
                return body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("utf-8", errors="replace")

    @staticmethod
    def _looks_like_challenge(document: str, final_url: str) -> bool:
        sample = f"{final_url}\n{document[:200_000]}".lower()
        return any(marker in sample for marker in CHALLENGE_MARKERS)

    def _request_headers(self, url: str, referer: str) -> dict[str, str]:
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": (
                "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6"
            ),
            "Connection": "keep-alive",
            "Priority": "u=0, i",
            "Referer": referer,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": self.user_agent,
        }
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header

        target = urlsplit(url)
        source = urlsplit(referer)
        headers["Sec-Fetch-Site"] = (
            "same-origin"
            if (target.scheme, target.netloc.lower())
            == (source.scheme, source.netloc.lower())
            else "cross-site"
        )

        chrome = re.search(
            r"(?:Chrome|Chromium)/(\d+(?:\.\d+){0,3})", self.user_agent
        )
        edge = re.search(r"Edg/(\d+(?:\.\d+){0,3})", self.user_agent)
        if chrome:
            chrome_version = chrome.group(1)
            chrome_major = chrome_version.split(".", 1)[0]
            brands = (
                f'"Not;A=Brand";v="8", "Chromium";v="{chrome_major}"'
            )
            if edge:
                edge_version = edge.group(1)
                edge_major = edge_version.split(".", 1)[0]
                brands += f', "Microsoft Edge";v="{edge_major}"'
            headers["Sec-CH-UA"] = brands
            headers["Sec-CH-UA-Mobile"] = "?0"
            if self.user_agent == DEFAULT_USER_AGENT:
                chrome_version = "150.0.7871.115"
                edge_version = "150.0.4078.65"
            else:
                edge_version = edge.group(1) if edge else chrome_version
            full_versions = (
                '"Not;A=Brand";v="8.0.0.0", '
                f'"Chromium";v="{chrome_version}"'
            )
            if edge:
                full_versions += f', "Microsoft Edge";v="{edge_version}"'
            headers["Sec-CH-UA-Full-Version"] = f'"{edge_version}"'
            headers["Sec-CH-UA-Full-Version-List"] = full_versions
        if "Windows" in self.user_agent:
            headers["Sec-CH-UA-Arch"] = '"x86"'
            headers["Sec-CH-UA-Bitness"] = '"64"'
            headers["Sec-CH-UA-Model"] = '""'
            headers["Sec-CH-UA-Platform"] = '"Windows"'
            headers["Sec-CH-UA-Platform-Version"] = '"19.0.0"'
        return headers

    @staticmethod
    def _retry_delay(attempt: int, headers: object) -> float:
        delay = min(MAX_RETRY_DELAY, 1.0 * (2**attempt))
        retry_after = getattr(headers, "get", lambda *_: None)("Retry-After")
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except (TypeError, ValueError):
                pass
        return min(delay, MAX_RETRY_DELAY)

    @staticmethod
    def _shell_quote(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"

    def build_curl_command(self, url: str, referer: str) -> str:
        """生成与实际请求一致、可直接复制诊断的 curl 命令。"""
        headers = self._request_headers(url, referer)
        quote = self._shell_quote
        parts = [f"curl -i {quote(url)}"]
        for name in ("Accept", "Accept-Language"):
            parts.append(f"-H {quote(f'{name.lower()}: {headers[name]}')}")
        if self.cookie_header:
            parts.append(f"-b {quote(self.cookie_header)}")
        for name in (
            "Priority",
            "Referer",
            "Sec-CH-UA",
            "Sec-CH-UA-Arch",
            "Sec-CH-UA-Bitness",
            "Sec-CH-UA-Full-Version",
            "Sec-CH-UA-Full-Version-List",
            "Sec-CH-UA-Mobile",
            "Sec-CH-UA-Model",
            "Sec-CH-UA-Platform",
            "Sec-CH-UA-Platform-Version",
            "Sec-Fetch-Dest",
            "Sec-Fetch-Mode",
            "Sec-Fetch-Site",
            "Sec-Fetch-User",
            "Upgrade-Insecure-Requests",
            "User-Agent",
        ):
            if name in headers:
                parts.append(f"-H {quote(f'{name.lower()}: {headers[name]}')}")
        return " \\\n  ".join(parts)

    def _show_failed_curl(self, code: int, url: str, referer: str) -> None:
        if not self.show_curl_on_error:
            return
        print(
            f"\n[curl] HTTP {code} 的完整复现命令（包含敏感 Cookie）：\n"
            f"{self.build_curl_command(url, referer)}\n",
            file=sys.stderr,
        )

    def fetch(self, url: str, referer: str) -> FetchResult:
        last_error: BaseException | None = None
        opener = self._opener()
        for attempt in range(self.retries + 1):
            self.rate_limiter.wait()
            request = Request(
                url,
                headers=self._request_headers(url, referer),
            )
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ScraperError(
                            f"响应超过 {MAX_RESPONSE_BYTES // 1024 // 1024} MiB，"
                            "已停止读取"
                        )
                    document = self._decode_body(body, response.headers)
                    final_url = response.geturl()
                    if self._looks_like_challenge(document, final_url):
                        raise AccessChallengeError(
                            "站点返回了 Cloudflare/验证码页面。请先在浏览器完成"
                            "验证，再更新 Cookie；User-Agent 必须与获取 "
                            "cf_clearance 时使用的浏览器一致。"
                        )
                    return FetchResult(document=document, final_url=final_url)
            except HTTPError as exc:
                body = exc.read(MAX_RESPONSE_BYTES)
                document = self._decode_body(body, exc.headers)
                if exc.code in {401, 403} or self._looks_like_challenge(
                    document, exc.geturl()
                ):
                    self._show_failed_curl(exc.code, url, referer)
                    raise AccessChallengeError(
                        f"站点返回 HTTP {exc.code}。Cookie 可能已过期，"
                        "请在浏览器重新完成验证后更新 Cookie，并保持相同的 "
                        "User-Agent。"
                    ) from exc
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.retries:
                    self._show_failed_curl(exc.code, url, referer)
                    raise ScraperError(
                        f"请求失败：HTTP {exc.code}，URL={url}"
                    ) from exc
                delay = self._retry_delay(attempt, exc.headers)
                self.rate_limiter.defer(delay)
                print(
                    f"[重试] HTTP {exc.code}，至少等待 {delay:g}s"
                    f"（{attempt + 1}/{self.retries}）",
                    file=sys.stderr,
                )
            except (URLError, socket.timeout, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise ScraperError(f"请求失败：{exc}，URL={url}") from exc
                delay = self._retry_delay(attempt, getattr(exc, "headers", None))
                self.rate_limiter.defer(delay)

        raise ScraperError(f"请求失败：{last_error}")  # pragma: no cover


class MagnetAppender:
    """逐条追加磁力链接，并按 infohash 去重（线程安全）。"""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.output.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if self.output.exists():
            existing = self.output.read_text(encoding="utf-8", errors="ignore")
        self.seen = {
            magnet_key(match.group(0))
            for match in MAGNET_CANDIDATE_RE.finditer(html.unescape(existing))
            if normalize_magnet(match.group(0))
        }
        self._needs_separator = bool(existing and not existing.endswith(("\n", "\r")))
        self._handle = self.output.open("a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def append(self, magnet: str) -> bool:
        key = magnet_key(magnet)
        with self._lock:
            if key in self.seen:
                return False
            if self._needs_separator:
                self._handle.write("\n")
                self._needs_separator = False
            self._handle.write(f"{magnet}\n")
            self._handle.flush()
            self.seen.add(key)
            return True

    def close(self) -> None:
        with self._lock:
            self._handle.close()

    def __enter__(self) -> MagnetAppender:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError("base-url 必须是 http/https 网址")
    if parsed.username or parsed.password:
        raise argparse.ArgumentTypeError("base-url 不应包含用户名或密码")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def build_search_url(
    base_url: str,
    keyword: str,
    page: int,
    sos: str,
    sofs: str,
) -> str:
    query = urlencode(
        {
            "keyword": keyword,
            "sos": sos,
            "sofs": sofs,
            "sot": "all",
            "soft": "all",
            "som": "auto",
            "p": page,
        }
    )
    return f"{base_url}/search?{query}"


def _normalized_host(value: str) -> str:
    host = (urlsplit(value).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def resolve_detail_result(
    result: SearchResult,
    search_url: str,
    allowed_hosts: set[str],
) -> SearchResult | None:
    absolute = urljoin(search_url, result.href)
    parsed = urlsplit(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = _normalized_host(absolute)
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts):
        return None
    if "/play/video/" in parsed.path:
        return None
    absolute = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )
    return SearchResult(
        title=result.title,
        href=absolute,
        total_size=result.total_size,
    )


def load_cookie_file(path: Path, hostname: str) -> str:
    content = path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return ""

    # 同时兼容浏览器导出的 Netscape cookies.txt。
    pairs: list[str] = []
    is_netscape_file = False
    for line in content.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif not line or line.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) >= 7:
            is_netscape_file = True
            cookie_domain = columns[0].lstrip(".").lower()
            if not (
                hostname == cookie_domain
                or hostname.endswith(f".{cookie_domain}")
            ):
                continue
            pairs.append(f"{columns[5]}={columns[6]}")
    if is_netscape_file:
        return "; ".join(pairs)

    if content.lower().startswith("cookie:"):
        content = content.split(":", 1)[1].strip()
    return normalize_space(content)


def resolve_cookie(args: argparse.Namespace) -> str:
    if args.cookie is not None:
        cookie = args.cookie.strip()
        if cookie:
            return cookie
        raise ScraperError("--cookie 不能为空")

    if args.prompt_cookie:
        cookie = getpass("Cookie（输入不会回显）: ").strip()
        if cookie:
            return cookie
        raise ScraperError("未输入 Cookie")

    candidates = [Path(args.cookie_file)]
    # 在其他目录执行时，也尝试脚本同目录下的 cookie.txt
    script_cookie = Path(__file__).resolve().parent / "cookie.txt"
    if script_cookie not in {path.resolve() for path in candidates if path.exists()}:
        candidates.append(script_cookie)

    hostname = (urlsplit(args.base_url).hostname or "").lower()
    for cookie_file in candidates:
        if not cookie_file.exists():
            continue
        try:
            cookie = load_cookie_file(cookie_file, hostname)
        except OSError as exc:
            raise ScraperError(f"无法读取 Cookie 文件：{exc}") from exc
        if cookie:
            print(f"[Cookie] 已从 {cookie_file.resolve()} 读取")
            return cookie
        raise ScraperError(f"Cookie 文件为空：{cookie_file.resolve()}")

    env_cookie = os.environ.get("SKRBT_COOKIE", "").strip()
    if env_cookie:
        print("[Cookie] 已从环境变量 SKRBT_COOKIE 读取")
        return env_cookie

    searched = " , ".join(str(path) for path in candidates)
    raise ScraperError(
        f"未找到 Cookie。请把浏览器 Cookie 写入以下之一：{searched}，"
        "或使用 --cookie / --prompt-cookie / 环境变量 SKRBT_COOKIE。"
    )


def resolve_page_count(limit: int, pages: int | None) -> int:
    if pages is not None:
        return pages
    # 按每页约 10 条估算，并多翻几页，避免结果偏少或分页不均。
    return max(1, (limit + DEFAULT_RESULTS_PER_PAGE - 1) // DEFAULT_RESULTS_PER_PAGE + 2)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负数字") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负数字")
    return parsed


def make_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "搜索 SkrBT，逐个打开详情页，过滤没有达到指定单文件大小的资源，"
            "并把磁力链接追加写入文件。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("keyword", help='搜索关键词，例如“三国演义”')
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("magnets.txt"),
        help="追加写入的文件（默认 magnets.txt，可省略 -o）",
    )
    parser.add_argument(
        "--base-url",
        type=normalize_base_url,
        default=DEFAULT_BASE_URL,
        help="站点根网址",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_LIMIT,
        help="最多处理的搜索结果条数",
    )
    parser.add_argument(
        "--pages",
        type=positive_int,
        default=None,
        help=(
            "最多翻页数；默认按 --limit 和每页约 "
            f"{DEFAULT_RESULTS_PER_PAGE} 条自动估算"
        ),
    )
    parser.add_argument(
        "--start-page", type=positive_int, default=1, help="起始页码"
    )
    parser.add_argument(
        "--min-file-size",
        type=parse_size_argument,
        default="0",
        metavar="SIZE",
        help=(
            "本地二次过滤：至少一个单文件达到此大小；"
            "默认 0（关闭），因站点 sofs 已按总大小筛选"
        ),
    )
    parser.add_argument(
        "--sos",
        default="relevance",
        help="传给站点的排序参数",
    )
    parser.add_argument(
        "--size-min",
        type=parse_size_argument,
        default=None,
        metavar="SIZE",
        help="站点搜索的最小总大小，如 100M、1G；会生成 sofs=gt…",
    )
    parser.add_argument(
        "--size-max",
        type=parse_size_argument,
        default=None,
        metavar="SIZE",
        help="站点搜索的最大总大小，如 500M、5G；会生成 sofs=…lt…",
    )
    parser.add_argument(
        "--sofs",
        default=None,
        help=(
            "直接传站点 sofs 原始值，如 gt1gb-lt5gb、gt100mb-lt500mb；"
            "与 --size-min/--size-max 互斥。都不指定时默认 gt600mb"
        ),
    )
    parser.add_argument(
        "--skip-unknown-size",
        action="store_true",
        help="详情页无法识别单文件大小时也跳过（默认保留）",
    )
    parser.add_argument(
        "--match-total-size",
        action="store_true",
        help=(
            "按资源总大小过滤，而不是要求单个文件达到阈值"
            "（更适合剧集合集）"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=non_negative_float,
        default=20.0,
        help="单次请求超时秒数",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="临时网络错误重试次数",
    )
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=DEFAULT_WORKERS,
        help="详情页并发线程数",
    )
    parser.add_argument(
        "--search-workers",
        type=positive_int,
        default=DEFAULT_SEARCH_WORKERS,
        help="搜索页并发线程数（同时抓取的页数）",
    )
    parser.add_argument(
        "--delay",
        type=non_negative_float,
        default=DEFAULT_DELAY,
        help="两次 HTTP 请求的全局最小间隔秒数",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="必须与获取 cf_clearance Cookie 时的浏览器一致",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=DEFAULT_COOKIE_FILE,
        help="包含原始 Cookie 或 Netscape cookies.txt 的文件",
    )
    parser.add_argument(
        "--cookie",
        help="原始 Cookie 字符串（会进入命令历史，优先于 Cookie 文件）",
    )
    parser.add_argument(
        "--prompt-cookie",
        action="store_true",
        help="运行时隐藏输入 Cookie（优先于 Cookie 文件）",
    )
    parser.add_argument(
        "--show-curl-on-error",
        action="store_true",
        help="HTTP 最终失败时输出含当前 Cookie 和完整请求头的 curl 命令",
    )
    return parser


def collect_search_results(
    client: HttpClient,
    *,
    base_url: str,
    keyword: str,
    start_page: int,
    page_count: int,
    limit: int,
    sos: str,
    sofs: str,
    search_workers: int,
    on_batch: Callable[[list[QueuedDetail]], None] | None = None,
) -> list[QueuedDetail]:
    """滑动窗口并行抓取搜索页；新链接通过 on_batch 边搜边处理。"""
    base_host = _normalized_host(base_url)
    queued: list[QueuedDetail] = []
    seen_details: set[str] = set()
    page_errors = 0
    empty_streak = 0
    print_lock = threading.Lock()

    def fetch_page(page: int) -> tuple[int, FetchResult | None, list[SearchResult], str]:
        search_url = build_search_url(base_url, keyword, page, sos, sofs)
        referer = (
            base_url
            if page <= 1
            else build_search_url(base_url, keyword, page - 1, sos, sofs)
        )
        try:
            response = client.fetch(search_url, referer)
        except AccessChallengeError:
            raise
        except ScraperError as exc:
            return page, None, [], str(exc)

        allowed_hosts = {
            base_host,
            _normalized_host(response.final_url),
        }
        raw_results = parse_search_page(response.document)
        results: list[SearchResult] = []
        seen_on_page: set[str] = set()
        for raw_result in raw_results:
            resolved = resolve_detail_result(
                raw_result,
                response.final_url,
                allowed_hosts,
            )
            if resolved and resolved.href not in seen_on_page:
                seen_on_page.add(resolved.href)
                results.append(resolved)
        return page, response, results, ""

    end_page = start_page + page_count
    next_page = start_page
    stop = False

    with ThreadPoolExecutor(max_workers=search_workers) as pool:
        inflight: dict = {}

        def fill_window() -> None:
            nonlocal next_page
            while not stop and next_page < end_page and len(inflight) < search_workers:
                inflight[pool.submit(fetch_page, next_page)] = next_page
                next_page += 1

        fill_window()
        while inflight and not stop:
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            for _, future in sorted((inflight[f], f) for f in done):
                page = inflight.pop(future)
                try:
                    p, response, results, error = future.result()
                except AccessChallengeError:
                    for leftover in inflight:
                        leftover.cancel()
                    raise

                if error:
                    page_errors += 1
                    with print_lock:
                        print(f"[搜索] 第 {p} 页失败：{error}", file=sys.stderr)
                    continue
                assert response is not None
                with print_lock:
                    print(f"[搜索] 第 {p} 页找到 {len(results)} 条")
                if not results:
                    empty_streak += 1
                    if empty_streak >= search_workers:
                        stop = True
                    continue
                empty_streak = 0

                new_items: list[QueuedDetail] = []
                for result in results:
                    if result.href in seen_details:
                        continue
                    seen_details.add(result.href)
                    item = QueuedDetail(
                        index=len(queued) + 1,
                        result=result,
                        referer=response.final_url,
                    )
                    queued.append(item)
                    new_items.append(item)
                    if len(queued) >= limit:
                        stop = True
                        break

                if new_items:
                    if on_batch is not None:
                        on_batch(new_items)
                    with print_lock:
                        print(
                            f"[搜索] 累计链接 {len(queued)}/{limit}，"
                            f"本批新增 {len(new_items)}"
                        )
                if stop:
                    break
            fill_window()

        for leftover in inflight:
            leftover.cancel()

    if len(queued) >= limit:
        print(f"[搜索] 已收集 {len(queued)} 条，达到 --limit")
    print(
        f"[搜索] 共收集 {len(queued)} 条详情链接"
        + (f"（{page_errors} 页失败）" if page_errors else "")
    )
    return queued


def process_detail(
    client: HttpClient,
    item: QueuedDetail,
    *,
    minimum: int,
    skip_unknown: bool,
    match_total: bool,
) -> DetailOutcome:
    title = item.result.title or item.result.href
    try:
        response = client.fetch(item.result.href, item.referer)
    except AccessChallengeError:
        raise
    except ScraperError as exc:
        return DetailOutcome(
            index=item.index,
            title=title,
            error=str(exc),
        )

    detail = parse_detail_page(response.document)
    if not detail.magnets:
        return DetailOutcome(
            index=item.index,
            title=title,
            missing_magnet=True,
        )

    decision = decide_by_size(
        detail=detail,
        search_total_size=item.result.total_size,
        minimum=minimum,
        skip_unknown=skip_unknown,
        match_total=match_total,
    )
    if not decision.keep:
        return DetailOutcome(
            index=item.index,
            title=title,
            filtered=True,
            filter_reason=decision.reason,
        )

    return DetailOutcome(
        index=item.index,
        title=title,
        magnets=detail.magnets,
        keep_reason=decision.reason,
        known_size=decision.known,
    )


def count_magnet_lines(path: Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8", errors="ignore")
    return sum(
        1
        for match in MAGNET_CANDIDATE_RE.finditer(html.unescape(text))
        if normalize_magnet(match.group(0))
    )


def apply_detail_outcome(
    outcome: DetailOutcome,
    *,
    appender: MagnetAppender,
    total: int,
    counters: dict[str, int],
    print_lock: threading.Lock,
) -> None:
    counters["processed"] += 1
    with print_lock:
        prefix = f"[详情 {outcome.index}/{total}]"
        if outcome.error:
            counters["detail_errors"] += 1
            print(
                f"{prefix} {outcome.title}\n  请求失败，已跳过：{outcome.error}",
                file=sys.stderr,
            )
            return
        if outcome.missing_magnet:
            counters["missing_magnet"] += 1
            print(f"{prefix} {outcome.title}\n  未找到 magnet 链接")
            return
        if outcome.filtered:
            counters["filtered"] += 1
            print(f"{prefix} {outcome.title}\n  已过滤：{outcome.filter_reason}")
            return

        label = "大小检查通过" if outcome.known_size else "提示"
        print(f"{prefix} {outcome.title}\n  {label}：{outcome.keep_reason}")
        for magnet in outcome.magnets:
            if appender.append(magnet):
                counters["written"] += 1
            else:
                counters["duplicates"] += 1


def run(args: argparse.Namespace) -> int:
    if not args.keyword.strip():
        raise ScraperError("关键词不能为空")
    if args.timeout == 0:
        raise ScraperError("timeout 必须大于 0")
    if args.retries < 0:
        raise ScraperError("retries 不能小于 0")

    started = time.monotonic()
    output_path = args.output.expanduser().resolve()
    cookie = resolve_cookie(args)
    sofs = resolve_sofs(args)
    client = HttpClient(
        base_url=args.base_url,
        cookie_header=cookie,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
        delay=args.delay,
        show_curl_on_error=args.show_curl_on_error,
    )
    page_count = resolve_page_count(args.limit, args.pages)
    existing_before = count_magnet_lines(output_path)

    print(f"[工作目录] {Path.cwd()}")
    print(
        f"[计划] 最多处理 {args.limit} 条，最多翻 {page_count} 页，"
        f"详情并发 {args.workers}，搜索并发 {args.search_workers}，"
        f"间隔 {args.delay}s，失败重试 {args.retries} 次"
    )
    print(f"[搜索] 站点大小筛选 sofs={sofs}")
    print(
        f"[输出] 追加写入 {output_path}（运行前已有 {existing_before} 条）"
    )
    print("[说明] 边搜索边抓详情并写入；搜索页“找到 10 条”还不是最终磁力")
    if args.min_file_size > 0:
        if args.match_total_size:
            print(
                f"[过滤] 本地按资源总大小 >= {format_size(args.min_file_size)} 保留"
            )
        else:
            print(
                f"[过滤] 本地要求至少一个单文件 >= {format_size(args.min_file_size)}"
            )
    else:
        print("[过滤] 本地单文件大小过滤已关闭（依赖站点 sofs）")

    counters = {
        "processed": 0,
        "written": 0,
        "duplicates": 0,
        "filtered": 0,
        "missing_magnet": 0,
        "detail_errors": 0,
    }
    print_lock = threading.Lock()
    pending: dict = {}
    expected_total = args.limit

    with MagnetAppender(output_path) as appender:
        with ThreadPoolExecutor(max_workers=args.workers) as detail_pool:

            def drain_completed(*, wait: bool = False) -> None:
                if wait and pending:
                    iterator = as_completed(list(pending.keys()))
                else:
                    iterator = [fut for fut in list(pending.keys()) if fut.done()]
                for future in iterator:
                    item = pending.pop(future, None)
                    if item is None:
                        continue
                    try:
                        outcome = future.result()
                    except AccessChallengeError:
                        for leftover in pending:
                            leftover.cancel()
                        raise
                    apply_detail_outcome(
                        outcome,
                        appender=appender,
                        total=expected_total,
                        counters=counters,
                        print_lock=print_lock,
                    )

            def on_batch(items: list[QueuedDetail]) -> None:
                for item in items:
                    future = detail_pool.submit(
                        process_detail,
                        client,
                        item,
                        minimum=args.min_file_size,
                        skip_unknown=args.skip_unknown_size,
                        match_total=args.match_total_size,
                    )
                    pending[future] = item
                drain_completed(wait=False)

            queued = collect_search_results(
                client,
                base_url=args.base_url,
                keyword=args.keyword.strip(),
                start_page=args.start_page,
                page_count=page_count,
                limit=args.limit,
                sos=args.sos,
                sofs=sofs,
                search_workers=args.search_workers,
                on_batch=on_batch,
            )
            expected_total = max(len(queued), 1)
            if not queued:
                print("[详情] 没有可处理的链接")
            else:
                print(f"[详情] 等待剩余 {len(pending)} 个详情任务完成…")
                drain_completed(wait=True)

    elapsed = time.monotonic() - started
    total_in_file = count_magnet_lines(output_path)
    print(
        f"[完成] 处理 {counters['processed']} 条，新增 {counters['written']} 条，"
        f"重复跳过 {counters['duplicates']} 条，大小过滤 {counters['filtered']} 条，"
        f"无 magnet {counters['missing_magnet']} 条，"
        f"详情请求失败 {counters['detail_errors']} 条，耗时 {elapsed:.1f}s"
    )
    print(
        f"[输出] 文件现有 {total_in_file} 条磁力"
        f"（本次新增 {counters['written']}，运行前 {existing_before}）"
    )
    print(f"[输出] 完整路径：{output_path}")
    if counters["processed"] and counters["filtered"] >= max(1, counters["processed"] // 2):
        print(
            "[提示] 大半结果被本地大小规则过滤了。"
            "可试：--match-total-size  或  --min-file-size 0"
        )
    return 0


def main() -> int:
    parser = make_argument_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except AccessChallengeError as exc:
        print(f"访问验证失败：{exc}", file=sys.stderr)
        return 2
    except ScraperError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消；此前成功提取的链接仍保留在输出文件中。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
