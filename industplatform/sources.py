"""Bounded discovery of public industrial datasets using the Forest pilot routes."""

import csv
import hashlib
import io
import json
import os
import re
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

WORKSPACE_FOREST = Path(__file__).resolve().parents[1] / "forest_atlas"
RUN_BYTE_LIMIT = 4 * 1024 * 1024
METADATA_LIMIT = 512 * 1024
SAMPLE_LIMIT = 256 * 1024
RUN_SECONDS = 25
DATA_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl"}
PERMISSIVE_SAMPLE_LICENSES = {
    "cc0-1.0", "cc-by-4.0", "cc-by-sa-4.0", "mit", "apache-2.0",
    "bsd-2-clause", "bsd-3-clause", "odc-by", "odc-pddl",
}
HOSTS = {
    "zenodo": {"zenodo.org"},
    "huggingface": {
        "huggingface.co", "cdn-lfs.huggingface.co", "cdn-lfs-us-1.huggingface.co",
        "cdn-lfs-eu-1.huggingface.co", "cdn-lfs.hf.co", "cdn-lfs-us-1.hf.co",
        "cdn-lfs-eu-1.hf.co", "cas-bridge.xethub.hf.co",
    },
}
ROUTE_DESCRIPTIONS = {
    "zenodo-remodel": "枚举 REMODEL 线束制造、装夹与路径规划数据",
    "zenodo-bam": "枚举 BAM 的超声、冲击回波与 CT 检测数据",
    "zenodo-tubers": "枚举 TUBERS 管道巡检、内窥与裂纹数据",
    "hf-plaid": "枚举 PLAID 的流体、翼型与结构仿真数据",
    "hf-polymathic": "枚举 The Well 的流动、传热和流变基准",
    "hf-lerobot-insertion": "枚举 LeRobot 的轴孔插装轨迹与场景数据",
}
USER_AGENT = "industplatform/0.1.0"
REPO_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*/[A-Za-z0-9_][A-Za-z0-9._-]*")


class SourceError(RuntimeError):
    """The remote source or its response could not be verified."""


class _ManualRedirect(HTTPRedirectHandler):
    # urllib's default redirect handler consumes the response body before
    # following a redirect. Keep redirect bodies outside the download path.
    def http_error_302(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class _Reader:
    def __init__(self, platform):
        self.hosts = HOSTS[platform]
        self.bytes = 0
        self.deadline = monotonic() + RUN_SECONDS
        self.opener = build_opener(_ManualRedirect())

    def _check_url(self, url):
        try:
            parsed = urlsplit(url)
            allowed = (
                parsed.scheme == "https" and parsed.hostname in self.hosts
                and parsed.port in (None, 443) and parsed.username is None
                and parsed.password is None and "\\" not in url
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise SourceError("来源请求或重定向超出允许的 HTTPS 域名")

    def read(self, url, maximum, accept):
        for _ in range(4):
            self._check_url(url)
            remaining = min(maximum, RUN_BYTE_LIMIT - self.bytes)
            seconds = self.deadline - monotonic()
            if remaining <= 0 or seconds <= 0:
                raise SourceError("已达到本次公开采集的字节或时间上限")
            request = Request(url, headers={
                "Accept": accept, "Accept-Encoding": "identity", "User-Agent": USER_AGENT,
            })
            try:
                with self.opener.open(request, timeout=min(6, seconds)) as response:
                    headers = response.headers
                    length = headers.get("Content-Length")
                    if length and (not length.isdecimal() or int(length) > remaining):
                        raise SourceError("资源大于单次读取或本轮剩余容量")
                    if headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise SourceError("当前采样仅读取未压缩的 HTTP 正文")
                    body = bytearray()
                    while len(body) < remaining:
                        if monotonic() >= self.deadline:
                            raise SourceError("公开采集达到时间上限")
                        chunk = response.read(min(16384, remaining - len(body)))
                        self.bytes += len(chunk)
                        if not chunk:
                            break
                        body.extend(chunk)
                    if length and len(body) != int(length):
                        raise SourceError("HTTP 正文长度与声明不一致")
                    if not length and len(body) == remaining:
                        raise SourceError("已达到读取上限，无法确认正文完整")
                    return bytes(body), headers
            except HTTPError as exc:
                location = exc.headers.get("Location")
                exc.close()
                if exc.code in {301, 302, 303, 307, 308} and location:
                    url = urljoin(url, location)
                    continue
                raise SourceError(f"来源请求返回 HTTP {exc.code}") from None
            except (URLError, OSError, HTTPException):
                raise SourceError("来源网络连接失败或超时") from None
        raise SourceError("来源重定向次数超过上限")

    def json(self, url):
        body, headers = self.read(url, METADATA_LIMIT, "application/json")
        try:
            return json.loads(body), headers
        except (ValueError, UnicodeError, RecursionError):
            raise SourceError("来源返回了无法解析的 JSON") from None


def _forest():
    configured = os.environ.get("INDUSTPLATFORM_FOREST_DIR")
    return Path(configured).expanduser() if configured else WORKSPACE_FOREST


@lru_cache(maxsize=4)
def _load_catalog(root):
    config_file = root / "pilot" / "collection_routes.json"
    terms_file = root / "final" / "04_keyword_master.csv"
    routes = json.loads(config_file.read_text(encoding="utf-8"))["routes"] if config_file.is_file() else []
    if terms_file.is_file():
        with terms_file.open(encoding="utf-8-sig", newline="") as stream:
            keyword_count = sum(1 for _ in csv.DictReader(stream))
    else:
        keyword_count = 0
    return routes, keyword_count


def catalog():
    routes, keyword_count = _load_catalog(_forest())
    demo = {"id": "demo-cnc", "label": "离线演示 · CNC 首件检验",
            "description": "通过合成检验数据跑通核验、编题、判分及工具验收"}
    live_routes = [
        {"id": route["id"], "label": route["publisher_or_collection"],
         "description": ROUTE_DESCRIPTIONS.get(route["id"], "枚举公开数据来源")}
        for route in routes
    ]
    return {"routes": [demo, *live_routes], "keyword_count": keyword_count}


def _uri(host, path, **parameters):
    return f"https://{host}{path}" + ("?" + urlencode(parameters) if parameters else "")


def _list_records(reader, route):
    platform = route["platform"]
    size = min(route["page_size"], 12)
    if platform == "zenodo":
        location = _uri("zenodo.org", "/api/records", q=route["query"], size=size, page=1, sort="mostrecent")
    else:
        filters = {"author": route["locator"], "limit": size}
        if route["query"]:
            filters["search"] = route["query"]
        location = _uri("huggingface.co", "/api/datasets", **filters)
    records = []
    for page in range(1, min(route["max_pages"], 2) + 1):
        body, headers = reader.json(location)
        if platform == "zenodo":
            hits = body.get("hits", {}).get("hits") if isinstance(body, dict) else None
            if not isinstance(hits, list):
                raise SourceError("Zenodo 列表响应缺少记录")
            if not hits:
                break
            location = _uri("zenodo.org", "/api/records", q=route["query"], size=size, page=page + 1, sort="mostrecent")
        else:
            if not isinstance(body, list):
                raise SourceError("Hugging Face 列表响应缺少数据集")
            hits = body
            match = re.search(r'<([^>]+)>;\s*rel="?next"?', headers.get("Link", ""))
            location = match.group(1) if match else ""
            if location:
                uri = urlsplit(location)
                if uri.hostname != "huggingface.co" or uri.path != "/api/datasets":
                    raise SourceError("Hugging Face 分页链接超出数据集接口")
        records.extend(record for record in hits if isinstance(record, dict))
        if not location or not hits:
            break
    return records


def _license_id(value):
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else ""
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, str):
        return value.lower().strip()
    return ""


def _zenodo_record(reader, _route, record_id):
    uri = _uri("zenodo.org", "/api/records/" + record_id)
    record, _ = reader.json(uri)
    if not isinstance(record, dict) or str(record.get("id")) != record_id:
        raise SourceError("Zenodo 记录编号与请求不一致")
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise SourceError("Zenodo 记录缺少可解析的元数据")
    access = record.get("access") or {}
    file_access = access.get("files") if isinstance(access, dict) else None
    record_access = access.get("record") if isinstance(access, dict) else None
    legacy_open = metadata.get("access_right") == "open"
    if not legacy_open and record_access != "public":
        return None, []
    family = str(record.get("conceptdoi") or ("zenodo:" + str(record.get("conceptrecid") or record_id)))
    files = []
    entries = (record.get("files") or []) if (file_access == "public" or file_access is None and legacy_open) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("key") or entry.get("filename")
        if isinstance(name, str) and name:
            url = uri + "/files/" + quote(name, safe="") + "/content"
            files.append((name, entry.get("size"), url))
    title = metadata.get("title") or "Zenodo " + record_id
    license_id = _license_id(metadata.get("license"))
    license_basis = "metadata.license.id"
    if not license_id:
        license_id = _license_id(metadata.get("rights"))
        license_basis = "metadata.rights" if license_id else "missing"
    asset = {
        "id": "zenodo:" + record_id, "title": str(title), "source": f"https://zenodo.org/records/{record_id}",
        "family": family, "version": str(record.get("doi") or record_id), "version_source": uri,
        "license": license_id, "license_source": uri, "license_basis": license_basis,
    }
    return asset, files


def _hf_record(reader, route, record_id):
    repo = quote(record_id, safe="/")
    uri = _uri("huggingface.co", "/api/datasets/" + repo, blobs="true")
    record, _ = reader.json(uri)
    if not isinstance(record, dict) or str(record.get("id", "")).lower() != record_id.lower():
        raise SourceError("Hugging Face 仓库编号与请求不一致")
    if record.get("private") or record.get("gated"):
        return None, []
    version = str(record.get("sha") or "")
    if not re.fullmatch(r"[\da-f]{40}", version, flags=re.IGNORECASE):
        raise SourceError("Hugging Face 记录缺少可固定的提交版本")
    source = _uri("huggingface.co", "/datasets/" + repo)
    base = source + "/resolve/" + version + "/"
    card = record.get("cardData") or {}
    asset = {
        "id": "hf:" + record_id.lower() + "@" + version, "title": record_id, "source": source,
        "family": "hf:" + route.get("family_aliases", {}).get(record_id.lower(), record_id.lower()),
        "version": version, "version_source": uri,
        "license": _license_id(card.get("license") if isinstance(card, dict) else None),
        "license_source": source + "/blob/" + version + "/README.md", "license_basis": "cardData.license",
    }
    files = []
    for entry in record.get("siblings") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("rfilename")
        if isinstance(name, str) and all(part not in ("", ".", "..") for part in name.split("/")):
            files.append((name, entry.get("size"), base + quote(name, safe="/")))
    _read_pinned_license(reader, asset, files)
    return asset, files


def _has_known_size(size, limit):
    return type(size) is int and 0 < size <= limit


def _read_pinned_license(reader, asset, files):
    if not asset["license"]:
        return
    candidates = [item for item in files if item[0].lower() == "readme.md"
                  or re.fullmatch(r"licen[cs]e(?:\.\w+)?", item[0], re.I)]
    for name, size, url in sorted(candidates, key=lambda item: item[0].lower() == "readme.md")[:2]:
        if not _has_known_size(size, 64 * 1024):
            continue
        try:
            body, _ = reader.read(url, size, "text/plain, application/octet-stream")
            document = body.decode("utf-8-sig")
        except (SourceError, UnicodeError):
            continue
        if len(body) != size or not document.strip() or document.lstrip().startswith("<"):
            continue
        if name.lower() == "readme.md":
            declared = re.search(r"^license:\s*['\"]?([\w.-]+)", document, re.MULTILINE)
            if not declared or declared.group(1).lower() != asset["license"]:
                continue
            asset["license_basis"] = "pinned_readme_declaration"
        else:
            asset["license_basis"] = "pinned_license_file_and_card_declaration"
        asset["license_source"] = url
        asset["license_sha256"] = hashlib.sha256(body).hexdigest()
        return


def _fields(body, suffix):
    try:
        decoded = body.decode("utf-8-sig")
        if not decoded.strip() or "\x00" in decoded or decoded.lstrip().startswith(("<", "version https://git-lfs")):
            return [], 0
        if suffix in {".csv", ".tsv"}:
            delimiter = "," if suffix == ".csv" else "\t"
            reader = csv.reader(io.StringIO(decoded, newline=""), delimiter=delimiter, strict=True)
            header = [field.strip() for field in next(reader, [])]
            data = [row for row in reader if any(row)]
            if not header or not all(header) or len(set(header)) != len(header):
                return [], 0
            if not data or any(len(row) != len(header) for row in data):
                return [], 0
            return header, len(data)
        if suffix == ".jsonl":
            data = [json.loads(line) for line in decoded.splitlines() if line.strip()]
        else:
            value = json.loads(decoded)
            data = value if isinstance(value, list) else [value]
        if not data or not all(isinstance(row, dict) for row in data):
            return [], 0
        common = set(data[0])
        for row in data[1:]:
            common.intersection_update(row)
        return sorted(common), len(data)
    except (UnicodeError, ValueError, csv.Error, RecursionError):
        return [], 0


def _inspect_sample(reader, asset, files):
    if asset["license"] not in PERMISSIVE_SAMPLE_LICENSES:
        return None, "许可声明需要人工核对，保留为候选"
    if asset["license_basis"] == "cardData.license":
        return None, "数据卡已列许可，尚未核对该版本的许可文件或声明"
    eligible = [
        (name, size, url) for name, size, url in files
        if Path(name.lower()).suffix in DATA_SUFFIXES and _has_known_size(size, SAMPLE_LIMIT)
        and not re.match(r"(?i)^(readme|licen[cs]e|dataset_info|metadata|statistics)", Path(name).name)
    ]
    if not eligible:
        return None, "未发现符合容量上限的结构化小文件"
    issue = "样本内容未通过格式和字段校验"
    for name, size, url in sorted(eligible, key=lambda file: (Path(file[0].lower()).suffix != ".csv", file[1]))[:2]:
        try:
            body, _ = reader.read(url, size, "application/octet-stream, text/plain, application/json, text/csv")
        except SourceError as exc:
            issue = str(exc)
            continue
        if len(body) != size:
            issue = "实文件大小与版本清单不一致"
            continue
        suffix = Path(name.lower()).suffix
        headers, records = _fields(body, suffix)
        if headers and records:
            return {
                "file": name, "url": url, "format": suffix[1:], "fields": headers[:80],
                "field_count": len(headers), "rows": records, "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(), "extent": "full_file",
            }, ""
    return None, issue


def _terms(prompt):
    return re.findall(r"[\w.-]+", prompt.casefold())[:24]


def _candidate(record, route):
    if route["platform"] == "zenodo":
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            return None
        record_id = str(record.get("id", ""))
        if not re.fullmatch(r"\d+", record_id):
            return None
        label = str(metadata.get("title") or "")
    else:
        record_id = str(record.get("id") or record.get("name") or "")
        if not REPO_NAME.fullmatch(record_id) or record_id.split("/", 1)[0].lower() != route["locator"].lower():
            return None
        label = record_id
    target = next((item for item in route.get("targets", ()) if re.search(item["pattern"], label, re.I)), None)
    if route.get("targets") and target is None:
        return None
    return record_id, label, target or {}


def discover(route_id, prompt=""):
    """Enumerate a configured public route. The prompt ranks local hits only."""
    routes, _ = _load_catalog(_forest())
    route = next((item for item in routes if item["id"] == route_id), None)
    if route is None:
        raise ValueError("未知公开采集路线")
    remote = _Reader(route["platform"])
    records = _list_records(remote, route)
    input_terms = _terms(prompt)
    candidates = [candidate for candidate in (_candidate(record, route) for record in records) if candidate]
    candidates.sort(key=lambda row: -sum(term in row[1].casefold() for term in input_terms))
    fetch = _zenodo_record if route["platform"] == "zenodo" else _hf_record
    assets, failures, families = [], [], set()
    for record_id, _, target in candidates[:min(route["max_probes"], 5)]:
        try:
            asset, files = fetch(remote, route, record_id)
        except SourceError as exc:
            failures.append(str(exc))
            continue
        if asset is None or asset["family"] in families:
            continue
        families.add(asset["family"])
        evidence, reason = _inspect_sample(remote, asset, files)
        asset.update({
            "route": route_id, "status": "sampled" if evidence else "candidate",
            "fields": evidence["fields"] if evidence else [],
            "summary": f"本次解析 {evidence['rows']} 条记录，保留实文件校验值。用途许可待复核。" if evidence else reason,
            "evidence": evidence, "declared_files": len(files), "target": target,
        })
        assets.append(asset)
    if not assets and failures:
        raise SourceError("未能核验匹配记录：" + failures[0])
    for asset in assets:
        asset["run_evidence"] = {"downloaded_bytes": remote.bytes, "metadata_failures": len(failures)}
    return assets
