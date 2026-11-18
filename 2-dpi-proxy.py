#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# [2/3] DPI BYPASS PROXY :8888  (HTTP + SOCKS5, TCP-фрагментация TLS)
# Что это: обход DPI для YouTube / Discord / GoogleVideo в браузере.
#          Без DNS, без WinDivert, без прав администратора.
# Порт: 8888 (HTTP-прокси и SOCKS5 на одном порту).
# Запуск: отдельно:  python 2-dpi-proxy.py
#          вместе:   python 0-start-all-tray.py  (сидит в трее)
# Ограничение: везёт TCP. Голос Discord (UDP) через него не идёт.
# ============================================================================
"""
Домашний обход без DNS и без WinDivert.
Запуск на ПК:  python home_proxy.py
  - слушает 0.0.0.0:8888 (HTTP-прокси + SOCKS5 на одном порту)
  - режет TLS ClientHello на куски с микрозадержкой (аналог zapret multisplit),
    DPI не узнаёт SNI и пропускает youtube / discord / googlevideo по TCP.
  - ПК: прописать прокси 127.0.0.1:8888
  - Телефон в том же Wi-Fi: прокси вручную = LAN_IP ПК : 8888
Только стандартная библиотека, без прав администратора.
Ограничение честно: HTTP/SOCKS везут TCP. Текст/видео работают.
Голос Discord и QUIC это UDP - через такой прокси не идут, в браузере
они сами падают в TCP-режим, в нативных приложениях голос может молчать.
Для голоса нужен WinDivert (zapret) или VPN.
"""
from __future__ import annotations

import argparse
import os
import select
import socket
import sys
import threading
import time

try:
    # Консоль Windows часто в cp1251 и падает на символах вне её.
    if getattr(sys.stdout, "reconfigure", None):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if getattr(sys.stderr, "reconfigure", None):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUF = 64 * 1024
TIMEOUT = 15


def log(*a):
    print(*a, flush=True)


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def send_fragmented(sock: socket.socket, data: bytes, delay: float) -> None:
    """Режем ClientHello: 1 байт + кусок до 512 + остаток, с паузой."""
    if not data:
        return
    parts = []
    if len(data) > 1:
        parts.append(data[:1])
        if len(data) > 512:
            parts.append(data[1:512])
            parts.append(data[512:])
        else:
            parts.append(data[1:])
    else:
        parts.append(data)
    for p in parts:
        if not p:
            continue
        sock.sendall(p)
        if delay > 0 and p is not parts[-1]:
            time.sleep(delay)


def pipe_direct(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            r, _, _ = select.select([a], [], [], 60)
            if not r:
                break
            d = a.recv(BUF)
            if not d:
                break
            b.sendall(d)
    except Exception:
        pass
    try:
        a.shutdown(socket.SHUT_RD)
    except Exception:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except Exception:
        pass


def relay_with_split(client: socket.socket, remote: socket.socket, delay: float) -> None:
    """client -> remote: первый пакет режем, дальше прозрачно. remote -> client: прозрачно."""
    t = threading.Thread(target=pipe_direct, args=(remote, client), daemon=True)
    t.start()
    try:
        client.settimeout(TIMEOUT)
        first = client.recv(BUF)
        if first:
            # TLS ClientHello начинается с 0x16 0x03, остальное (plain http) шлём как есть
            if len(first) >= 3 and first[0] == 0x16 and first[1] == 0x03:
                send_fragmented(remote, first, delay)
            else:
                remote.sendall(first)
        while True:
            r, _, _ = select.select([client], [], [], 60)
            if not r:
                break
            d = client.recv(BUF)
            if not d:
                break
            remote.sendall(d)
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            remote.close()
        except Exception:
            pass


def handle_http(client: socket.socket, first: bytes, delay: float) -> None:
    try:
        head = first
        client.settimeout(5)
        while b"\r\n\r\n" not in head and len(head) < 65536:
            d = client.recv(BUF)
            if not d:
                break
            head += d
    except Exception:
        client.close()
        return
    try:
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
    except Exception:
        client.close()
        return
    parts = request_line.split()
    if len(parts) < 2:
        client.close()
        return
    method, target = parts[0].upper(), parts[1]

    hostport = None
    if method == "CONNECT":
        hostport = target
        try:
            h, p = hostport.rsplit(":", 1)
            remote = socket.create_connection((h, int(p)), timeout=TIMEOUT)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
            client.close()
            return
        try:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        except Exception:
            remote.close()
            client.close()
            return
        relay_with_split(client, remote, delay)
        return
    else:
        # обычный GET http://host/path
        if target.startswith("http://"):
            rest = target[7:]
            slash = rest.find("/")
            hostport_s = rest if slash == -1 else rest[:slash]
            path = "/" if slash == -1 else rest[slash:]
            if ":" in hostport_s:
                h, p = hostport_s.rsplit(":", 1)
                port = int(p)
            else:
                h, port = hostport_s, 80
            try:
                remote = socket.create_connection((h, port), timeout=TIMEOUT)
            except Exception:
                try:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                except Exception:
                    pass
                client.close()
                return
            # переписываем абсолютный URI в относительный
            lines = head.split(b"\r\n")
            lines[0] = f"{method} {path} HTTP/1.1".encode("latin-1")
            remote.sendall(b"\r\n".join(lines))
            relay_with_split(client, remote, 0)
            return
        else:
            try:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            except Exception:
                pass
            client.close()
            return


def handle_socks5(client: socket.socket, first: bytes, delay: float) -> None:
    try:
        client.settimeout(TIMEOUT)
        # first = VER NMETHODS METHODS...
        if len(first) < 2:
            first += client.recv(BUF)
        nmethods = first[1]
        while len(first) < 2 + nmethods:
            first += client.recv(BUF)
        client.sendall(b"\x05\x00")  # no auth
        # запрос: VER CMD RSV ATYP ADDR PORT
        req = client.recv(4)
        if len(req) < 4:
            client.close()
            return
        ver, cmd, _, atyp = req[0], req[1], req[2], req[3]
        if ver != 5 or cmd != 1:
            try:
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            except Exception:
                pass
            client.close()
            return
        if atyp == 1:  # IPv4
            addr = client.recv(4)
            host = socket.inet_ntoa(addr)
        elif atyp == 3:  # domain
            ln = client.recv(1)[0]
            host = client.recv(ln).decode()
        elif atyp == 4:  # IPv6
            addr = client.recv(16)
            host = socket.inet_ntop(socket.AF_INET6, addr)
        else:
            client.close()
            return
        port = int.from_bytes(client.recv(2), "big")
        try:
            remote = socket.create_connection((host, port), timeout=TIMEOUT)
        except Exception:
            try:
                client.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            except Exception:
                pass
            client.close()
            return
        try:
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            remote.close()
            client.close()
            return
        relay_with_split(client, remote, delay if port == 443 else 0)
    except Exception:
        try:
            client.close()
        except Exception:
            pass


def handle_client(conn: socket.socket, delay: float) -> None:
    try:
        conn.settimeout(5)
        first = conn.recv(4096)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return
    if not first:
        try:
            conn.close()
        except Exception:
            pass
        return
    if first[0] == 0x05:
        handle_socks5(conn, first, delay)
    else:
        handle_http(conn, first, delay)


def main() -> None:
    ap = argparse.ArgumentParser(description="Домашний прокси для ПК и телефона (HTTP+SOCKS5, TCP-фрагментация TLS)")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--delay", type=float, default=0.005, help="пауза между кусками ClientHello, сек (0.002-0.02)")
    args = ap.parse_args()
    if os.name == "nt" and not getattr(sys, "frozen", False):  # подпись окна в панели задач
        try:
            os.system("title [2/3] DPI Proxy :8888 (YouTube/Discord)")
        except Exception:
            pass

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((args.host, args.port))
    except OSError as e:
        log(f"[X] Порт {args.port} занят: {e}")
        sys.exit(1)
    srv.listen(100)
    lan = get_lan_ip()
    log("")
    log("=" * 60)
    log("  Домашний прокси запущен")
    log(f"  Слушаю: {args.host}:{args.port}  (HTTP + SOCKS5)")
    log("")
    log(f"  ПК:        127.0.0.1:{args.port}")
    log(f"  ТЕЛЕФОН:   {lan}:{args.port}  (тот же Wi-Fi!)")
    log("")
    log("  ПК Windows: Параметры -> Сеть и Интернет -> Прокси ->")
    log(f"              вручную HTTP+S/HTTPS 127.0.0.1:{args.port}")
    log("  Android: Wi-Fi -> долгая тап по сети -> Прокси вручную ->")
    log(f"           имя {lan} порт {args.port}")
    log("  iPhone: Wi-Fi -> (i) сети -> Прокси вручную ->")
    log(f"          сервер {lan} порт {args.port}")
    log("  Telegram Desktop: Настройки -> Продвинутые -> Тип соединения ->")
    log(f"              SOCKS5/HTTP 127.0.0.1:{args.port}")
    log("")
    log("  Если YouTube долго стартует - это QUIC/UDP мимо прокси,")
    log("  браузер сам упадёт в TCP. Можно отключить QUIC в chrome://flags.")
    log("  Голос Discord (UDP) через прокси не идёт - это ограничение метода,")
    log("  текст/видео идут. Для голоса нужен zapret/WinDivert.")
    log("  Стоп: Ctrl+C")
    log("=" * 60)
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle_client, args=(conn, args.delay), daemon=True).start()
    except KeyboardInterrupt:
        log("\nОстановлено.")


if __name__ == "__main__":
    import sys
    main()
