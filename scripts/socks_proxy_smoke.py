"""真机检查：系统代理设成 SOCKS 时，翻译、反馈、组件下载的请求照样能发出去，而且确实走了代理。

在 Windows / macOS 上跑（PR checks 手动触发时由 CI 跑，也可以在本机跑）：

    python scripts/socks_proxy_smoke.py

本机起一个 SOCKS5 代理和一个假的服务器，全在 127.0.0.1 上。
- Windows：把当前用户「Internet 选项」的代理临时改成只填 SOCKS（``socks=127.0.0.1:端口``），
  这正是 Python 读成 ``socks4://``、httpx 原本建不了客户端的那种设置；跑完改回原样。
- macOS：Python 不读系统设置里的 SOCKS 代理，只有环境变量 ALL_PROXY 会让请求走 SOCKS，就用它。
都成功时退出 0。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Socks5Server:
    """No-auth SOCKS5 CONNECT only; records the hosts it was asked to reach."""

    def __init__(self) -> None:
        self.targets: list[str] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    @staticmethod
    def _read(sock: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _handle(self, client: socket.socket) -> None:
        try:
            version, methods = self._read(client, 2)
            if version != 5:
                raise OSError(f"SOCKS version {version}")
            self._read(client, methods)
            client.sendall(b"\x05\x00")
            _, command, _, address_type = self._read(client, 4)
            if command != 1:
                raise OSError(f"SOCKS command {command}")
            if address_type == 1:
                host = socket.inet_ntoa(self._read(client, 4))
            elif address_type == 3:
                host = self._read(client, self._read(client, 1)[0]).decode()
            else:
                host = socket.inet_ntop(socket.AF_INET6, self._read(client, 16))
            (port,) = struct.unpack("!H", self._read(client, 2))
            self.targets.append(f"{host}:{port}")
            upstream = socket.create_connection((host, port), timeout=5)
            client.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", port))
        except Exception:  # noqa: BLE001
            client.close()
            return
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()
        _pipe(client, upstream)

    def close(self) -> None:
        self._listener.close()


class Origin:
    """Answers every request like an OpenAI-compatible chat endpoint would."""

    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.dumps({
                    "ok": True,
                    "choices": [{"finish_reason": "stop", "message": {"content": "Bonjour"}}],
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def _no_proxy_environment() -> Iterator[None]:
    saved = {name: os.environ.pop(name, None) for var in _PROXY_VARS for name in (var, var.upper())}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


@contextmanager
def _windows_socks_setting(port: int) -> Iterator[None]:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    names = ("ProxyEnable", "ProxyServer", "ProxyOverride")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
        saved = {}
        for name in names:
            try:
                saved[name] = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                saved[name] = None
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"socks=127.0.0.1:{port}")
        try:
            winreg.DeleteValue(key, "ProxyOverride")
        except FileNotFoundError:
            pass
        try:
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(key, name, 0, value[1], value[0])


@contextmanager
def system_socks_proxy(port: int) -> Iterator[None]:
    with _no_proxy_environment():
        if sys.platform == "win32":
            with _windows_socks_setting(port):
                yield
        else:
            os.environ["ALL_PROXY"] = f"socks5://127.0.0.1:{port}"
            try:
                yield
            finally:
                os.environ.pop("ALL_PROXY", None)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'OK  ' if ok else 'FAIL'} {label}{f'：{detail}' if detail else ''}", flush=True)
    return ok


def main() -> int:
    import httpx

    from core import component_download, feedback, system_proxy
    from core.text_transport import request_text

    socks, origin = Socks5Server(), Origin()
    results: list[bool] = []
    try:
        with system_socks_proxy(socks.port):
            proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https", "all")}
            print(f"Python 读到的系统代理：{proxies}", flush=True)
            results.append(_check("系统代理指向本机 SOCKS", any(str(socks.port) in v for v in proxies.values())))
            try:
                httpx.Client().close()
                print("（这台机器上原本的 httpx 也能建客户端）", flush=True)
            except (ValueError, ImportError) as exc:
                print(f"（原本的 httpx 在这里会报错：{exc}）", flush=True)

            before = len(socks.targets)
            with system_proxy.http_client(timeout=10) as client:
                results.append(_check("普通请求", client.get(origin.url + "/models").status_code == 200))

            async def fetch() -> int:
                async with system_proxy.async_client(timeout=10) as client:
                    return (await client.post(origin.url + "/v1/chat/completions", json={})).status_code

            results.append(_check("异步请求", asyncio.run(fetch()) == 200))
            text, _route = request_text(base_url=origin.url + "/v1", api_key="k", model="m",
                                        system="translate", user="你好", api_mode="chat")
            results.append(_check("翻译请求", text == "Bonjour", text))
            with feedback._open_client() as client:
                results.append(_check("反馈发送", client.post(origin.url + "/v1/feedback", json={}).status_code == 200))
            with component_download.open_client() as client:
                results.append(_check("组件下载", client.get(origin.url + "/a.whl").status_code == 200))
            routed = len(socks.targets) - before
            results.append(_check("以上请求都经过了 SOCKS 代理", routed == 5, f"{routed} 次"))
    finally:
        socks.close()
        origin.close()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
