import io
import zipfile
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from industplatform import archive_sample


class ArchiveSampleTests(unittest.TestCase):
    def test_reads_only_the_ranges_needed_for_the_member(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("inspection.qif", b"<QIFDocument />")
            archive.writestr("unrelated.bin", b"x" * (160 * 1024))
        body = data.getvalue()
        requests = []

        @contextmanager
        def open_source(url, *, byte_range):
            requests.append(byte_range)
            if byte_range == "bytes=-65536":
                first, last = len(body) - 65536, len(body) - 1
            else:
                first, last = map(int, byte_range.removeprefix("bytes=").split("-"))
            yield Mock(status=206, headers={"Content-Range": f"bytes {first}-{last}/{len(body)}"},
                       read1=io.BytesIO(body[first:last + 1]).read)

        with patch.object(archive_sample.public_web, "open_source", open_source):
            payload, transferred = archive_sample.read_member("https://archive.example.org/data.zip", "inspection.qif")
        self.assertEqual(payload, b"<QIFDocument />")
        self.assertLess(transferred, len(body))
        self.assertGreater(len(requests), 1)

    def test_refuses_full_archive_response_before_reading_the_body(self):
        response = Mock(status=200, headers={})

        @contextmanager
        def open_source(*args, **kwargs):
            yield response

        with patch.object(archive_sample.public_web, "open_source", open_source):
            with self.assertRaisesRegex(ValueError, "HTTP 206"):
                archive_sample.read_member("https://archive.example.org/data.zip", "inspection.qif")
        response.read1.assert_not_called()
