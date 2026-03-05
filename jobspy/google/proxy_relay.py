"""
Local proxy relay that injects HTTP Basic Auth credentials into upstream proxy requests.

Chromium doesn't support HTTP proxy authentication for HTTPS CONNECT tunnels.
This relay listens on localhost without auth and forwards to the upstream proxy
with credentials injected, solving the auth problem transparently.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import socket
import threading
from urllib.parse import urlparse

log = logging.getLogger("JobSpy:ProxyRelay")


class ProxyRelay:
    def __init__(self, upstream_proxy: str, host: str = "127.0.0.1"):
        parsed = urlparse(upstream_proxy)
        self.upstream_host = parsed.hostname
        self.upstream_port = parsed.port or 12321
        self.host = host
        self.port = self._find_free_port()

        creds = ""
        if parsed.username:
            password = parsed.password or ""
            creds = f"{parsed.username}:{password}"
        self._auth_header = base64.b64encode(creds.encode()).decode() if creds else None

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for server to be ready
        import time
        for _ in range(20):
            time.sleep(0.1)
            if self._server is not None:
                break

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        # Don't join — daemon thread will die with process

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        log.debug(f"Proxy relay listening on {self.host}:{self.port}")
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        up_writer = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
            first_line = header.split(b"\r\n")[0]
            method = first_line.split(b" ")[0]

            up_reader, up_writer = await asyncio.open_connection(
                self.upstream_host, self.upstream_port
            )

            if method == b"CONNECT":
                host_port = first_line.split(b" ")[1].decode()
                connect_req = f"CONNECT {host_port} HTTP/1.1\r\nHost: {host_port}\r\n"
                if self._auth_header:
                    connect_req += f"Proxy-Authorization: Basic {self._auth_header}\r\n"
                connect_req += "\r\n"

                up_writer.write(connect_req.encode())
                await up_writer.drain()

                up_resp = await asyncio.wait_for(
                    up_reader.readuntil(b"\r\n\r\n"), timeout=15
                )
                status_line = up_resp.split(b"\r\n")[0]

                if b"200" in status_line:
                    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    await writer.drain()
                    await asyncio.gather(
                        self._pipe(reader, up_writer),
                        self._pipe(up_reader, writer),
                    )
                else:
                    writer.write(up_resp)
                    await writer.drain()
            else:
                # Regular HTTP request — inject auth header
                lines = header.split(b"\r\n")
                new_lines = [lines[0]]
                if self._auth_header:
                    new_lines.append(
                        f"Proxy-Authorization: Basic {self._auth_header}".encode()
                    )
                for line in lines[1:]:
                    if line:
                        new_lines.append(line)
                up_writer.write(b"\r\n".join(new_lines) + b"\r\n\r\n")
                await up_writer.drain()
                await asyncio.gather(
                    self._pipe(reader, up_writer),
                    self._pipe(up_reader, writer),
                )

        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
            if up_writer:
                try:
                    up_writer.close()
                except Exception:
                    pass

    @staticmethod
    async def _pipe(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                data = await reader.read(16384)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
