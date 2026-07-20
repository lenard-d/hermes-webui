"""Guarded outbound transport and bounded audio buffering for speech."""

from __future__ import annotations

import errno
import http.client
import ipaddress
import socket
import sys
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler


TTS_PROXY_MAX_BYTES = 16 * 1024 * 1024
TTS_LOCALHOST_HOSTS = {"127.0.0.1", "::1", "localhost"}


def tts_addr_is_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def tts_host_is_blocked_target(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host:
        return True
    try:
        ipaddress.ip_address(host)
        return tts_addr_is_blocked(host)
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    return any(
        sockaddr and tts_addr_is_blocked(str(sockaddr[0]))
        for *_prefix, sockaddr in infos
    )


def tts_resolve_pinned_addresses(hostname: str, port: int | None) -> list[str]:
    host = (hostname or "").strip().lower()
    if not host:
        raise ValueError("invalid OpenAI TTS base_url host")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise ValueError("could not resolve OpenAI TTS base_url host") from exc

    pinned_hosts = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        pinned_host = str(sockaddr[0])
        if tts_addr_is_blocked(pinned_host):
            raise ValueError("resolved OpenAI TTS target is not allowed")
        pinned_hosts.append(pinned_host)
    if not pinned_hosts:
        raise ValueError("could not resolve OpenAI TTS base_url host")
    return pinned_hosts


def tts_resolve_pinned_address(hostname: str) -> str:
    return tts_resolve_pinned_addresses(hostname, None)[0]


def normalize_openai_tts_base_url(base_url: str) -> str:
    raw = str(base_url or "").strip()
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").strip().lower()
    if parsed.username or parsed.password:
        raise ValueError("invalid OpenAI base_url in config")
    if not parsed.scheme or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("invalid OpenAI base_url in config")
    if parsed.scheme == "https":
        if tts_host_is_blocked_target(hostname):
            raise ValueError("invalid OpenAI base_url in config")
    elif parsed.scheme == "http" and hostname in TTS_LOCALHOST_HOSTS:
        pass
    else:
        raise ValueError("invalid OpenAI base_url in config")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def buffer_tts_audio_response(resp, *, max_bytes: int = TTS_PROXY_MAX_BYTES) -> bytes:
    headers = getattr(resp, "headers", None)
    content_type = ""
    if headers is not None:
        try:
            content_type = str(headers.get("Content-Type") or "")
        except Exception:
            content_type = ""
    if not content_type:
        try:
            content_type = str(resp.info().get("Content-Type") or "")
        except Exception:
            content_type = ""
    if content_type and not content_type.lower().startswith("audio/"):
        raise ValueError("upstream returned non-audio content")

    audio_data = bytearray()
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        audio_data.extend(chunk)
        if len(audio_data) > max_bytes:
            raise ValueError("upstream audio exceeded byte limit")
    return bytes(audio_data)


class NoRedirectTtsHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("OpenAI TTS upstream attempted a redirect")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Dial a vetted literal address while retaining hostname TLS verification."""

    def connect(self):
        sys.audit("http.client.connect", self, self.host, self.port)
        last_error = None
        for pinned_host in tts_resolve_pinned_addresses(self.host, self.port):
            try:
                self.sock = socket.create_connection(
                    (pinned_host, self.port), self.timeout, self.source_address
                )
                break
            except OSError as exc:
                last_error = exc
        else:
            if last_error is not None:
                raise last_error
            raise OSError("could not connect to any pinned OpenAI TTS target")
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT:
                raise
        if self._tunnel_host:
            self._tunnel()
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=server_hostname,
        )


class PinnedHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PinnedHTTPSConnection, req, context=self._context)


def tts_open(req, *, timeout=30, opener_factory=None):
    if opener_factory is not None:
        return opener_factory().open(req, timeout=timeout)
    from urllib.request import urlopen

    return urlopen(req, timeout=timeout)
