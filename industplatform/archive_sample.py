"""Read an individually verified ZIP member via bounded HTTPS Range requests."""

import io
import re
import zipfile
from http.client import HTTPException
from time import monotonic
from urllib.parse import urljoin

from . import public_web


MAX_TRANSFER = 2 * 1024 * 1024
MAX_MEMBER = 1024 * 1024
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class RangeArchive(io.RawIOBase):
    def __init__(self, url, stop=None):
        self.url = public_web.public_url(url)
        self.stop = stop
        self.bytes_read = self.requests = self.offset = 0
        self.deadline = monotonic() + 35
        start, end, self.size, self.tail = self._range("bytes=-65536", 65536)
        if end != self.size - 1 or start != max(0, self.size - 65536):
            raise ValueError("归档尾部范围与请求不一致")

    def _range(self, header, maximum):
        if maximum > MAX_TRANSFER - self.bytes_read:
            raise ValueError("样本超出本轮 Range 预算")
        try:
            for _ in range(4):
                if self.requests >= 20 or monotonic() >= self.deadline or (self.stop and self.stop.is_set()):
                    raise ValueError("样本请求中断或达到本轮读取上限")
                self.requests += 1
                with public_web.open_source(self.url, byte_range=header) as response:
                    if response.status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                        self.url = public_web.public_url(urljoin(self.url, response.headers["Location"]))
                        continue
                    if response.status != 206 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ValueError("上游未返回可核验的 HTTP 206 范围，已停止读取归档")
                    matched = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                    if not matched:
                        raise ValueError("归档缺少有效的 Content-Range")
                    start, end, total = map(int, matched.groups())
                    size = end - start + 1
                    if not 0 < size <= maximum or not 0 <= start <= end < total:
                        raise ValueError("上游返回了超出限制的文件范围")
                    chunks = bytearray()
                    while len(chunks) < size:
                        if monotonic() >= self.deadline or (self.stop and self.stop.is_set()):
                            raise ValueError("样本读取已中断或超时")
                        chunk = response.read1(min(65536, size - len(chunks)))
                        if not chunk:
                            raise ValueError("上游范围正文不完整")
                        chunks.extend(chunk)
                        self.bytes_read += len(chunk)
                    return start, end, total, bytes(chunks)
        except (OSError, HTTPException) as exc:
            raise ValueError(f"样本 Range 请求失败（{type(exc).__name__}）") from None
        raise ValueError("样本归档重定向次数超出限制")

    def seek(self, offset, whence=io.SEEK_SET):
        origin = {io.SEEK_SET: 0, io.SEEK_CUR: self.offset, io.SEEK_END: self.size}[whence]
        if origin + offset < 0:
            raise OSError("Invalid seek")
        self.offset = origin + offset
        return self.offset

    def seekable(self):
        return True

    def tell(self):
        return self.offset

    def read(self, count=-1):
        start = self.offset
        end = self.size if count < 0 else min(self.size, start + count)
        if end <= start:
            return b""
        tail_start = self.size - len(self.tail)
        if start >= tail_start:
            data = self.tail[start - tail_start:end - tail_start]
        else:
            actual_start, actual_end, total, data = self._range(f"bytes={start}-{end - 1}", end - start)
            if (actual_start, actual_end, total) != (start, end - 1, self.size):
                raise ValueError("上游归档文件范围在读取过程中发生变化")
        self.offset = end
        return data


def read_member(url, member, stop=None):
    archive = RangeArchive(url, stop)
    try:
        with zipfile.ZipFile(archive) as zip_data:
            entry = zip_data.getinfo(member)
            if not 0 < entry.file_size <= MAX_MEMBER:
                raise ValueError("归档成员超出样本读取大小")
            with zip_data.open(entry) as stream:
                payload = stream.read(MAX_MEMBER + 1)
            if len(payload) != entry.file_size:
                raise ValueError("归档成员长度不符")
    except (zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError) as exc:
        raise ValueError(f"无法核验归档成员（{type(exc).__name__}）") from None
    return payload, archive.bytes_read
