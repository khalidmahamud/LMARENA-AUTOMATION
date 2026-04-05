import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.proxy.xlsx_source import load_proxy_candidates_from_xlsx


class LoadProxyCandidatesFromXlsxTests(unittest.TestCase):
    def _write_workbook(self, rows) -> str:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)

        path = Path(tmpdir.name) / "proxies.xlsx"
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        wb.save(path)
        wb.close()
        return str(path)

    def test_reads_proxy_server_column_and_respects_limit(self) -> None:
        path = self._write_workbook(
            [
                ("proxy_server", "latency_ms", "protocol"),
                ("http://1.2.3.4:8080", 100, "http"),
                ("http://2.3.4.5:8080", 120, "http"),
            ]
        )

        proxies = load_proxy_candidates_from_xlsx(path, protocol="http", limit=1)

        self.assertEqual(proxies, [{"server": "http://1.2.3.4:8080"}])

    def test_builds_servers_from_ip_port_and_filters_by_protocol(self) -> None:
        path = self._write_workbook(
            [
                ("ip", "port", "protocol"),
                ("10.0.0.1", 9000, "http"),
                ("10.0.0.2", 1080, "socks5"),
            ]
        )

        proxies = load_proxy_candidates_from_xlsx(path, protocol="socks5")

        self.assertEqual(proxies, [{"server": "socks5://10.0.0.2:1080"}])

    def test_skips_invalid_rows_and_deduplicates_servers(self) -> None:
        path = self._write_workbook(
            [
                ("proxy_server", "username", "password"),
                ("http://1.2.3.4:8080", "alice", "secret"),
                ("1.2.3.4:8080", "", ""),
                ("not-a-proxy", "", ""),
                ("http://5.6.7.8:8080", "", ""),
            ]
        )

        proxies = load_proxy_candidates_from_xlsx(path, protocol="http")

        self.assertEqual(
            proxies,
            [
                {
                    "server": "http://1.2.3.4:8080",
                    "username": "alice",
                    "password": "secret",
                },
                {"server": "http://5.6.7.8:8080"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
