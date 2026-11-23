"""Extract bounded public-source leads from files selected on the local workbench."""

import csv
import io
import json
import re
import subprocess
from pathlib import PurePath
from urllib.parse import urljoin

from scripts.data_policy import SECRET

from . import shared_metadata
from .public_web import Blocks

FORMATS = {".html", ".htm", ".pdf", ".csv"}
MAX_LINKS = 5000
URL = re.compile(r"https?://[a-z0-9._~%!$&()*+,;=/?#@:\-]+", re.I)
CHARSET = re.compile(rb"charset\s*=\s*['\"]?([a-z0-9_.-]+)", re.I)
PRIVATE_CONTEXT = re.compile(r"\b(?:api[_ -]?(?:key|token)|access[_ -]?token|password|private key|bearer)\b|密码|令牌|密钥", re.I)
LINK_FIELDS = re.compile(r"(?:^|_)(?:urls?|uris?|href|links?|downloads?|homepage|repo|repository)$", re.I)
CATALOG_TITLE = ("title_zh", "name_zh", "title", "name", "topic", "heading")
CATALOG_CONTEXT = (*CATALOG_TITLE, "summary", "description", "category", "stage", "format", "owner", "publisher",
                   "version", "license", "kind", "segment", "subsegment", "data_summary")
CATALOG_SKIP = {"raw", "content", "body", "samples", "sample_data", "attachments", "snapshots", "expected_answer"}
TITLE_FIELDS = {"title", "name", "document", "标题", "名称", "文献名", "文件名"}
CONTEXT_FIELDS = TITLE_FIELDS | {
    "publisher", "version", "licence", "license", "description", "summary", "abstract", "notes", "evidence",
    "process", "domain", "发布方", "来源说明", "版本", "日期", "许可", "摘要", "说明", "备注", "证据", "工序", "领域",
}


def source_url(value):
    value = value.strip().rstrip(".,;:!?）")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    key = shared_metadata.canonical_key("sources", value)
    if SECRET.search(key.encode("utf-8")):
        raise ValueError("原文链接疑似包含凭据")
    return key


def compact(value, limit):
    """Pass on plain, bounded context without other URLs or recognized credentials."""
    value = " ".join(URL.sub(" ", value).split())
    if PRIVATE_CONTEXT.search(value) or SECRET.search(value.encode("utf-8")):
        return ""
    return value[:limit]


class Links:
    def __init__(self):
        self.items = {}
        self.topic = ""

    def add(self, url, title="", context="", *, priority=0):
        try:
            key = source_url(url)
        except ValueError:
            return
        if key not in self.items and len(self.items) >= MAX_LINKS:
            raise ValueError(f"文档含超过 {MAX_LINKS} 条链接，请拆分后导入")
        title, context = compact(title, 160), compact(context, 360)
        if key not in self.items or priority > self.items[key][0] or (
            priority == self.items[key][0] and len(title + context) > len(self.items[key][1]["title"] + self.items[key][1]["context"])
        ):
            self.items[key] = priority, {"url": key, "title": title, "context": context}

    def scan(self, text, title="", context=None):
        for match in URL.finditer(text):
            nearby = text[max(0, match.start() - 160):match.end() + 180]
            self.add(match[0], title, nearby if context is None else context)


class ReportHTML(Blocks):
    def __init__(self):
        super().__init__()
        self.origin = ""
        self.heading = ""
        self.section = ""
        self.links = []
        self.anchor = None
        self.catalogs = []
        self.catalog_parts = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if not self.skip and tag == "script" and (values.get("type") or "").lower().strip() == "application/json":
            self.catalog_parts = []
        super().handle_starttag(tag, attrs)
        if self.skip:
            return
        if tag == "a" and values.get("href"):
            self.anchor = (values["href"], self.section, len(self.parts), [])
        elif tag in ("title", "h1", "h2", "h3", "h4"):
            self.heading = tag
        elif tag == "link" and "canonical" in (values.get("rel") or "").lower().split():
            self.origin = values.get("href") or self.origin
        elif tag == "meta" and values.get("property") == "og:url" and not self.origin:
            self.origin = values.get("content") or ""

    def handle_endtag(self, tag):
        if tag == "script" and self.catalog_parts is not None:
            self.catalogs.append("".join(self.catalog_parts))
            self.catalog_parts = None
        if not self.skip:
            if tag == "a" and self.anchor:
                url, section, position, parts = self.anchor
                self.links.append((url, " ".join(parts), section, position))
                self.anchor = None
            elif tag == self.heading:
                self.heading = ""
        super().handle_endtag(tag)

    def handle_data(self, data):
        if self.catalog_parts is not None:
            self.catalog_parts.append(data)
            return
        if self.skip:
            return
        if self.heading and data.strip():
            self.section = data.strip()[:160]
        if self.anchor:
            self.anchor[3].append(data)
        super().handle_data(data)


def decode_text(content, html=False):
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    hint = CHARSET.search(content[:4096]) if html else None
    encodings = ["utf-8-sig", "gb18030"]
    if hint and hint[1].lower() in {b"utf-8", b"gbk", b"gb2312", b"gb18030", b"big5", b"windows-1252"}:
        encodings.insert(0, hint[1].decode("ascii"))
    if b"\0" not in content:
        for encoding in encodings:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
    raise ValueError("文件无法按常见文本编码读取，请转为 UTF-8")


def catalog_field(value):
    if isinstance(value, str):
        return value[:180]
    if isinstance(value, list):
        return "、".join(item[:60] for item in value[:6] if isinstance(item, str))
    return ""


def catalog_links(value, links, depth=0):
    if depth > 20:
        return
    if isinstance(value, list):
        for item in value:
            catalog_links(item, links, depth + 1)
    elif isinstance(value, dict):
        label = next((value[key] for key in CATALOG_TITLE if isinstance(value.get(key), str) and value[key].strip()), "")
        context = "；".join(f"{key}={item}" for key in CATALOG_CONTEXT if (item := catalog_field(value.get(key))))
        for key, child in value.items():
            if key.startswith("_") or key.casefold() in CATALOG_SKIP:
                continue
            if LINK_FIELDS.search(key):
                values = child if isinstance(child, list) else [child]
                for item in values:
                    if isinstance(item, str):
                        links.scan(item, label, context)
            catalog_links(child, links, depth + 1)


def read_html(content, links, origin):
    parser = ReportHTML()
    parser.feed(decode_text(content, html=True))
    parser.close()
    lines = [line for line in "".join(parser.parts).splitlines() if line.strip()]
    title = lines[0] if lines else ""
    links.topic = compact(title, 120)
    base = origin or parser.origin
    links.add(base, title, " ".join(lines[:8]), priority=3)
    for href, label, section, position in parser.links:
        nearby = parser.parts[max(0, position - 3):position + 5]
        context = " ".join([section, *(part[:120] for part in nearby)])
        links.add(urljoin(base, href) if base else href, label or section, context, priority=1)
    for line in lines:
        links.scan(line, title)
    for embedded in parser.catalogs:
        try:
            catalog = json.loads(embedded)
        except (ValueError, RecursionError):
            continue
        catalog_links(catalog, links)


def read_csv(content, links, origin):
    text = decode_text(content)
    dialect: type[csv.Dialect] | str
    try:
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = "excel"
        rows = csv.reader(io.StringIO(text, newline=""), dialect)
        first = next(rows, ())
        fields = [cell.strip().casefold() for cell in first]
        links.add(origin, context="、".join(field for field in first if not URL.search(field)), priority=3)
        for cell in first:
            links.scan(cell, context="")
        for row in rows:
            title = next((cell for key, cell in zip(fields, row) if key in TITLE_FIELDS and cell.strip()), "")
            context = "；".join(key + "=" + cell[:200] for key, cell in zip(fields, row) if key in CONTEXT_FIELDS)
            for cell in row:
                links.scan(cell, title, context)
    except csv.Error:
        raise ValueError("CSV 无法解析，请检查列分隔符和字段长度") from None


def read_pdf(content, links, origin):
    if not content.startswith(b"%PDF-"):
        raise ValueError("文件内容不符合 PDF 格式")
    try:
        result = subprocess.run(["pdftotext", "-enc", "UTF-8", "-layout", "-", "-"], input=content,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12, check=True)
    except FileNotFoundError:
        raise ValueError("读取 PDF 需要安装 Poppler（pdftotext）") from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError("PDF 无法读取，可能已加密、损坏或超过解析时间") from None
    text = result.stdout.decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = lines[0] if lines else ""
    links.add(origin, title, " ".join(lines[:8]), priority=3)
    for index, line in enumerate(lines):
        if URL.search(line):
            links.scan(line, title, " ".join(lines[max(0, index - 1):index + 2]))
    try:
        annotations = subprocess.run(["pdfinfo", "-url", "-"], input=content, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, timeout=8, check=True)
    except (OSError, subprocess.SubprocessError):
        annotations = None
    if annotations:
        for line in annotations.stdout.decode("utf-8", "replace").splitlines():
            links.scan(line, title, "")
    if not lines and not links.items:
        raise ValueError("PDF 未找到可选文本或链接，扫描件请先做文字识别")


def extract(name, content, origin=""):
    """Return URL leads only. Full file bodies are released after parsing."""
    if SECRET.search(content):
        raise ValueError("发现疑似密钥或令牌，文件已跳过")
    links = Links()
    suffix = PurePath(name).suffix.lower()
    parse = {".html": read_html, ".htm": read_html, ".csv": read_csv, ".pdf": read_pdf}[suffix]
    parse(content, links, origin)
    if not links.items:
        raise ValueError("未找到可共享的原文 HTTPS 链接")
    return [{**item, **({"topic": links.topic} if links.topic else {})} for _, item in links.items.values()]
