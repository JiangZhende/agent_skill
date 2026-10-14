#!/usr/bin/env python
"""
沙箱出口代理：联网的 skill 只能经它访问外网。

只做一件事：转发前检查目标地址，拒绝一切非公网地址（内网、回环、链路本地、
云元数据地址等），并且只连接检查过的那个 IP，防止 DNS 在检查之后换成内网地址。

支持 HTTP（绝对 URI）和 HTTPS（CONNECT 隧道），只用标准库，不解密 HTTPS。

环境变量:
    EGRESS_PORT            监听端口，默认 3128
    EGRESS_ALLOWED_PORTS   允许访问的目标端口，默认 "80,443"
    EGRESS_ALLOW_DOMAINS   可选，逗号分隔的域名白名单（含子域）；不设则放行所有公网域名
    EGRESS_UPSTREAM_PROXY  可选，上游 HTTP 代理（如 http://10.1.2.3:8118）。所在网络本身要经代理才能
                           访问外网时使用。目标地址检查照常进行；通过检查后由上游代理去连接和解析域名，
                           所以上游代理自己不应该能访问到需要保护的内网。
"""
import base64
import ipaddress
import os
import select
import socket
import socketserver
import sys
from urllib.parse import urlsplit

PORT = int(os.getenv("EGRESS_PORT", "3128"))
ALLOWED_PORTS = {int(p) for p in os.getenv("EGRESS_ALLOWED_PORTS", "80,443").split(",") if p.strip()}
ALLOW_DOMAINS = [d.strip().lower().lstrip(".") for d in os.getenv("EGRESS_ALLOW_DOMAINS", "").split(",") if d.strip()]
_up = urlsplit(os.getenv("EGRESS_UPSTREAM_PROXY", "").strip())
UPSTREAM = (_up.hostname, _up.port or 8080) if _up.hostname else None
UPSTREAM_AUTH = (
    b"Proxy-Authorization: Basic " + base64.b64encode(f"{_up.username}:{_up.password or ''}".encode()) + b"\r\n"
    if _up.username else b""
)
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 60
MAX_HEADER_BYTES = 64 * 1024


class Denied(Exception):
    pass


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global 为假的包括：私网、回环、链路本地（含 169.254.169.254）、CGNAT、保留地址、组播
    return ip.is_global and not ip.is_multicast


def resolve_checked(host: str, port: int) -> tuple[int, str]:
    """检查目标并返回 (地址族, IP)。不允许时抛 Denied。"""
    host = host.strip("[]").rstrip(".").lower()
    if port not in ALLOWED_PORTS:
        raise Denied(f"port {port} not allowed")
    if ALLOW_DOMAINS and not any(host == d or host.endswith("." + d) for d in ALLOW_DOMAINS):
        raise Denied("domain not in allowlist")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Denied("cannot resolve host")
    addrs = [(family, sockaddr[0]) for family, _, _, _, sockaddr in infos]
    if not addrs:
        raise Denied("cannot resolve host")
    # 只要有一个解析结果不是公网地址就整体拒绝，不给"公网和内网地址混着返回"留空子
    for _, addr in addrs:
        if not _is_public(ipaddress.ip_address(addr.split("%")[0])):
            raise Denied("destination is not a public address")
    return addrs[0]


def _log(verdict: str, client: str, target: str, detail: str = ""):
    print(f"{verdict} {client} -> {target} {detail}".rstrip(), flush=True)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(IDLE_TIMEOUT)
        peer = self.client_address[0]
        try:
            head, rest = self._read_head(client)
        except (OSError, ValueError):
            return
        try:
            request_line, _, header_block = head.partition(b"\r\n")
            method, target, version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            self._reply(client, 400, "Bad Request")
            return

        try:
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                family, ip = resolve_checked(host, int(port))
                upstream = self._tunnel(host, int(port)) if UPSTREAM else self._connect(family, ip, int(port))
                _log("ALLOW", peer, target)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                url = urlsplit(target)
                if url.scheme != "http" or not url.hostname:
                    raise Denied("only absolute http:// URLs or CONNECT are supported")
                port = url.port or 80
                family, ip = resolve_checked(url.hostname, port)
                if UPSTREAM:
                    # 交给上游代理：保留绝对 URI（上游通常只允许对 443 用 CONNECT）
                    upstream = self._connect(socket.AF_UNSPEC, *UPSTREAM)
                    path = target
                else:
                    upstream = self._connect(family, ip, port)
                    path = url.path or "/"
                    if url.query:
                        path += "?" + url.query
                _log("ALLOW", peer, f"{url.hostname}:{port}")
                headers = [
                    h for h in header_block.split(b"\r\n")
                    if h and not h.lower().startswith((b"proxy-", b"connection:"))
                ]
                upstream.sendall(
                    f"{method} {path} {version}\r\n".encode("latin-1")
                    + (UPSTREAM_AUTH if UPSTREAM else b"")
                    + b"\r\n".join(headers + [b"Connection: close"]) + b"\r\n\r\n" + rest
                )
        except Denied as e:
            _log("DENY", peer, target, f"({e})")
            self._reply(client, 403, f"Blocked by sandbox egress proxy: {e}")
            return
        except (OSError, ValueError) as e:
            _log("ERROR", peer, target, f"({type(e).__name__})")
            self._reply(client, 502, "Bad Gateway")
            return

        with upstream:
            self._relay(client, upstream)

    @staticmethod
    def _read_head(sock) -> tuple[bytes, bytes]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(8192)
            if not chunk:
                raise ValueError("closed")
            buf += chunk
            if len(buf) > MAX_HEADER_BYTES:
                raise ValueError("header too large")
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    @staticmethod
    def _connect(family: int, ip: str, port: int) -> socket.socket:
        if family == socket.AF_UNSPEC:  # 上游代理：按主机名连接
            s = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
        else:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect((ip, port))  # 连接的是检查过的 IP，而不是重新解析域名
        s.settimeout(IDLE_TIMEOUT)
        return s

    def _tunnel(self, host: str, port: int) -> socket.socket:
        """经上游代理建立到 host:port 的隧道。"""
        s = self._connect(socket.AF_UNSPEC, *UPSTREAM)
        try:
            s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n".encode("latin-1")
                      + UPSTREAM_AUTH + b"\r\n")
            head, _ = self._read_head(s)
            status = head.split(b"\r\n", 1)[0].split(b" ")
            if len(status) < 2 or status[1] != b"200":
                raise OSError("upstream proxy refused the tunnel")
        except (OSError, ValueError):
            s.close()
            raise OSError("upstream proxy tunnel failed")
        return s

    @staticmethod
    def _reply(sock, code: int, message: str):
        body = (message + "\n").encode()
        try:
            sock.sendall(
                f"HTTP/1.1 {code} {message.split(':')[0]}\r\nContent-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1") + body
            )
        except OSError:
            pass

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket):
        socks = [a, b]
        try:
            while True:
                readable, _, _ = select.select(socks, [], [], IDLE_TIMEOUT)
                if not readable:
                    return
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


if __name__ == "__main__":
    print(f"egress proxy listening on :{PORT} ports={sorted(ALLOWED_PORTS)} "
          f"domains={ALLOW_DOMAINS or 'any public'} upstream={'%s:%s' % UPSTREAM if UPSTREAM else 'none'}", flush=True)
    with Server(("0.0.0.0", PORT), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            sys.exit(0)
