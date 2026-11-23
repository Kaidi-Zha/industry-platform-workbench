"""Bounded public search and read-only retrieval for industrial evidence."""

import csv
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from time import monotonic
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


SOURCE_BYTES = 4 * 1024 * 1024
USER_AGENT = "industplatform/0.1 (+public industrial reference verification)"

ENTRIES = (
    {"id": "nist", "publisher": "NIST", "kind": "国家实验室与研究数据",
     "url": "https://www.nist.gov/programs-projects/digital-thread-smart-manufacturing",
     "hosts": ["nist.gov"], "stage": "S1/S4/S5", "focus": "设计、制造和检验数据的数字线程"},
    {"id": "dmsc", "publisher": "DMSC", "kind": "计量标准组织",
     "url": "https://qifstandards.org/resources/", "hosts": ["qifstandards.org"], "stage": "S5/S8",
     "focus": "QIF、DMIS、CMM 计量示例与格式"},
    {"id": "iaqg", "publisher": "IAQG", "kind": "航空供应链标准组织",
     "url": "https://access.iaqg.org/standard/9102-first-article-inspection-requirement/",
     "hosts": ["iaqg.org"], "stage": "S5/S8", "focus": "9102 首件检验流程、表单与常见问题"},
    {"id": "mitutoyo", "publisher": "Mitutoyo", "kind": "计量厂商",
     "url": "https://www.mitutoyo.com/", "hosts": ["mitutoyo.com"], "stage": "S5",
     "focus": "坐标测量系统与数据报告工具"},
    {"id": "zenodo", "publisher": "CERN / OpenAIRE", "kind": "数据仓库",
     "url": "https://zenodo.org/", "hosts": ["zenodo.org"], "stage": "S1–S8", "focus": "原始试验数据、版本与许可"},
    {"id": "hf", "publisher": "Hugging Face", "kind": "数据仓库",
     "url": "https://huggingface.co/datasets", "hosts": ["huggingface.co"], "stage": "S1–S8", "focus": "公开资产及数据卡"},
)


def authority(url):
    host = (urlsplit(url).hostname or "").lower()
    return next((entry["id"] for entry in ENTRIES for name in entry["hosts"]
                 if host == name or host.endswith("." + name)), "")


def backend(deepseek_key=None):
    if os.getenv("INDUST_BRAVE_SEARCH_KEY"):
        return "brave"
    if os.getenv("INDUST_SEARXNG_URL"):
        return "searxng"
    if deepseek_key or os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    return "crossref"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SearchError(ValueError):
    def __init__(self, engine, status=None, *, invalid=False):
        message = "检索结果格式无效" if invalid else (
            f"检索失败（HTTP {status}）" if status else "检索失败（连接或响应异常）")
        super().__init__(f"{engine} {message}")
        self.status = status


class SourcePolicyError(ValueError):
    """A source was rejected before connecting to its destination."""


def search(query, limit=5, *, deepseek_key=None):
    """Search the configured web engine, with Crossref available as a publication-only index."""
    query = query.strip()
    if len(query) < 3:
        raise ValueError("检索式至少需要 3 个字符")
    deepseek_key = deepseek_key or os.getenv("DEEPSEEK_API_KEY")
    choice = backend(deepseek_key)
    payload = None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if choice == "brave":
        address = "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": limit})
        headers["X-Subscription-Token"] = os.environ["INDUST_BRAVE_SEARCH_KEY"]
    elif choice == "searxng":
        root = os.environ["INDUST_SEARXNG_URL"].rstrip("/")
        parsed = urlsplit(root)
        if (not parsed.hostname or parsed.username is not None or parsed.query or parsed.fragment
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in
                                                     {"127.0.0.1", "localhost", "::1"}))):
            raise ValueError("SearXNG 地址须为 HTTPS，或本机 HTTP 服务")
        address = root + "/search?" + urlencode({"q": query, "format": "json", "categories": "general"})
    elif choice == "deepseek":
        address = "https://api.deepseek.com/v1/tools/search"
        headers.update({"Authorization": "Bearer " + deepseek_key, "Content-Type": "application/json"})
        query = query[:128].rstrip()
        payload = json.dumps({"query": query, "count": min(50, limit), "need_content": False}).encode()
    else:
        address = "https://api.crossref.org/works?" + urlencode({
            "query.bibliographic": query, "rows": limit, "select": "title,URL,link,publisher,resource",
        })
    try:
        local = urlsplit(address).hostname in {"127.0.0.1", "localhost", "::1"}
        proxy = ProxyHandler({}) if local else ProxyHandler()
        with build_opener(NoRedirect(), proxy).open(Request(address, data=payload, headers=headers), timeout=12) as response:
            data = json.loads(response.read(1024 * 1024))
        if choice == "brave":
            rows = data.get("web", {}).get("results", [])
        elif choice in ("searxng", "deepseek"):
            rows = data["results"]
        else:
            rows = []
            for item in data["message"]["items"]:
                url = (item.get("resource") or {}).get("primary", {}).get("URL") or item.get("URL")
                rows.append({"url": url, "title": " ".join(item.get("title", [])), "description": item.get("publisher", "")})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise SearchError(choice, getattr(exc, "code", None)) from None
    if not isinstance(rows, list):
        raise SearchError(choice, invalid=True)
    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            url = public_url(row.get("url"))
        except ValueError:
            continue
        if any(item["url"] == url for item in results):
            continue
        results.append({"url": url, "title": clean_text(row.get("title"))[:180],
                        "snippet": clean_text(row.get("snippet") or row.get("description") or row.get("content"))[:160],
                        "authority": authority(url)})
        if len(results) == limit:
            break
    return {"query": query, "backend": choice, "scope": "publications" if choice == "crossref" else "open_web",
            "results": results}


def clean_text(value):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", value)).split()) if isinstance(value, str) else ""


def public_url(value):
    try:
        url = urlsplit(value if isinstance(value, str) else "")
        if (url.scheme == "https" and url.hostname and url.username is None and url.port in (None, 443)
                and len(value) <= 1600 and not any(char.isspace() or ord(char) < 32 for char in value)
                and "\\" not in value):
            host = url.hostname.rstrip(".").encode("idna").decode()
            # Require DNS names to rule out local hostnames and alternative IP spellings.
            if re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]*", host) and not host.endswith(
                (".local", ".internal", ".localhost", ".test", ".invalid", ".onion")
            ):
                return urlunsplit(("https", host, quote(url.path or "/", safe="/%:@!$&'()*+,;=~-._"),
                                   quote(url.query, safe="/?%:@!$&'()*+,;=~-._"), ""))
    except ValueError:
        pass
    raise SourcePolicyError("原文链接须为公开 HTTPS 地址")


def public_addresses(host):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    for address in addresses:
        ip = ipaddress.ip_address(address)
        normalized = getattr(ip, "ipv4_mapped", None) or ip
        if not normalized.is_global or normalized.is_multicast:
            raise SourcePolicyError("原文地址解析到非公网网络")
    if not addresses:
        raise ValueError("无法解析原文域名")
    return addresses


def fetch(url, limit=SOURCE_BYTES):
    url = public_url(url)
    deadline = monotonic() + 15
    for _ in range(4):
        try:
            with open_source(url) as response:
                headers = response.headers
                if response.status in (301, 302, 303, 307, 308) and headers.get("Location"):
                    url = public_url(urljoin(url, headers["Location"]))
                    continue
                if response.status != 200:
                    raise ValueError(f"公开原文返回 HTTP {response.status}")
                if headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("原文返回了未支持的内容编码")
                length = headers.get("Content-Length")
                if length and length.isdecimal() and int(length) > limit:
                    raise ValueError("原文超过读取上限")
                body = bytearray()
                while len(body) <= limit:
                    if monotonic() >= deadline:
                        raise ValueError("公开原文读取超时")
                    chunk = response.read1(min(65536, limit + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                if len(body) > limit:
                    raise ValueError("原文超过读取上限")
                return url, headers, bytes(body)
        except (OSError, http.client.HTTPException) as exc:
            raise ValueError(f"公开原文读取失败（{type(exc).__name__}）") from None
    raise ValueError("公开原文重定向次数超出上限")


@contextmanager
def open_source(url, *, byte_range=None):
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
               "Accept": "text/html,application/pdf,application/json,application/xml,text/plain,text/csv,*/*;q=0.2"}
    if byte_range is not None:
        headers.update({"Range": byte_range, "Accept": "application/zip,application/octet-stream"})
    parts = urlsplit(url)
    proxy = os.getenv("INDUST_PUBLIC_WEB_PROXY")
    if proxy:
        # The operator's egress proxy is responsible for vetting its own DNS targets and CONNECT destinations.
        config = urlsplit(proxy)
        if (config.scheme != "http" or not config.hostname or config.username is not None
                or config.path not in ("", "/") or config.query or config.fragment):
            raise ValueError("公开原文代理须为受信任的 HTTP CONNECT 地址")
        connection = http.client.HTTPSConnection(config.hostname, config.port or 80, timeout=6)
        connection.set_tunnel(parts.hostname, port=443)
    else:
        addresses = public_addresses(parts.hostname)
        connection = http.client.HTTPSConnection(parts.hostname, timeout=6)
        # Pin the checked address, and verify TLS against the original hostname.
        connection._create_connection = lambda _address, timeout, source: socket.create_connection(
            (addresses[0], 443), timeout, source,
        )
    try:
        connection.request("GET", urlunsplit(("", "", parts.path, parts.query, "")), headers=headers)
        with connection.getresponse() as response:
            yield response
    finally:
        connection.close()


class Blocks(HTMLParser):
    BLOCK = {"p", "article", "section", "div", "li", "tr", "title", "h1", "h2", "h3", "h4", "pre", "br"}
    IGNORE = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if self.skip:
            return
        if tag in self.IGNORE:
            self.skip = tag
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skip == tag:
            self.skip = None
        elif not self.skip and tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def extract(url, headers, body, focus, *, page=None, artifact_format=None):
    mime = headers.get_content_type()
    structured = {}
    pdf = mime == "application/pdf" or body.startswith(b"%PDF-")
    if pdf:
        try:
            text = subprocess.run(["pdftotext", "-f", str(page or 1), "-l", str(page or 30), "-layout", "-", "-"], input=body,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=6, check=True)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("PDF 无法提取文本（需要 Poppler pdftotext；扫描件需另行核验）") from None
        pages = text.stdout.decode("utf-8", "replace").split("\f")
    else:
        text = body.decode(headers.get_content_charset() or "utf-8", "replace")
        if mime == "text/html" or text.lstrip().lower().startswith(("<!doctype html", "<html")):
            parser = Blocks()
            parser.feed(text)
            text = "".join(parser.parts)
        elif mime in {"application/json", "text/csv", "application/xml", "text/xml"} or url.endswith((".xml", ".csv", ".json")):
            try:
                if "xml" in mime or url.endswith(".xml"):
                    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
                        raise ValueError
                    root = ET.fromstring(body)
                    structured = {"format": "XML", "root": root.tag.rsplit("}", 1)[-1]}
                elif "json" in mime or url.endswith(".json"):
                    data = json.loads(text)
                    structured = {"format": "JSON", "root": type(data).__name__}
                else:
                    headers_row = next(csv.reader(io.StringIO(text)), [])
                    if len(headers_row) > 1:
                        structured = {"format": "CSV", "fields": [field[:60] for field in headers_row[:18]]}
            except (ValueError, ET.ParseError, csv.Error, RecursionError):
                raise ValueError("结构化文件无法解析") from None
        elif text.lstrip().startswith("ISO-10303-21;"):
            structured = {"format": "STEP", "root": "ISO-10303-21"}
        elif urlsplit(url).path.lower().endswith((".nc", ".gcode", ".tap")) and re.search(r"^\s*(?:N\d+\s*)?G\d+", text, re.M):
            structured = {"format": "NC", "root": "G-code blocks"}
        elif not artifact_format and not mime.startswith("text/"):
            raise ValueError("当前只核验 HTML、PDF、文本及小型结构化文件")
        pages = [text]
    terms = re.findall(r"[\w.-]{2,}", focus.casefold())
    blocks = []
    for number, text in enumerate(pages, page or 1):
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        for index in range(0, len(lines), 3):
            value = " ".join(lines[index:index + 3])
            for part, position in enumerate(range(0, len(value), 620), 1):
                excerpt = value[position:position + 620]
                score = sum(term in excerpt.casefold() for term in terms)
                label = f"第 {number} 页 / L{index + 1}.{part}" if pdf else f"L{index + 1}.{part}"
                blocks.append((score, {"locator": label, "text": excerpt}))
    passages = [item for _, item in sorted(blocks, key=lambda row: -row[0])[:8]]
    if not passages:
        raise ValueError("原文没有可抽取的文本证据")
    return {"url": url, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "mime": mime, "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "authority": authority(url), "structured": structured, "passages": passages}
