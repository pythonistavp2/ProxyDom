#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# [1/3] TELEGRAM MTProxy :1443  (tg-ws-proxy)
# Что это: ускорение Telegram через WebSocket-мост. Для Telegram Desktop и
#          приложения Telegram на телефоне (MTProto-прокси).
# Порт: 1443. Secret хранится в secret.txt рядом (не удаляй).
# Запуск: отдельно:  python 1-telegram-mtproxy.py
#          вместе:   python 0-start-all-tray.py  (сидит в трее)
# ============================================================================
"""
Домашний запуск tg-ws-proxy (Flowseal/tg-ws-proxy).
Запуск:  python run_home_proxy.py
Что делает:
  1) проверяет/доустанавливает зависимости (httpx[http2], certifi, h2)
  2) если рядом нет исходников прокси — сам скачивает их с GitHub
  3) генерирует и сохраняет secret в secret.txt (чтобы не перенастраивать Telegram каждый раз)
  4) слушает на 0.0.0.0:1443 — доступно и с самого ПК, и с телефона в том же Wi-Fi
  5) печатает готовые tg:// ссылки для ПК и для телефона
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import socket
import subprocess
import sys
import urllib.request
import zipfile

REPO_ZIP_URL = "https://github.com/Flowseal/tg-ws-proxy/archive/refs/heads/main.zip"

try:
    # Консоль Windows часто в cp1251 и падает на символах вне её (краш UnicodeEncodeError).
    # UTF-8 чинит отображение, errors=replace страхует от падения.
    if getattr(sys.stdout, "reconfigure", None):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if getattr(sys.stderr, "reconfigure", None):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
NEED_DEPS = ["httpx[http2]", "certifi", "h2", "cryptography"]
SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret.txt")


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_deps(auto_install: bool = True) -> None:
    if getattr(sys, "frozen", False):
        # Внутри exe зависимости уже упакованы — только проверяем
        try:
            import httpx  # noqa: F401
            import h2  # noqa: F401
            import certifi  # noqa: F401
        except ImportError as e:
            log(f"[X] в сборке не хватает зависимости: {e}")
            sys.exit(2)
        return
    missing = []
    try:
        import httpx  # noqa: F401
    except ImportError:
        missing.append("httpx")
    try:
        import h2  # noqa: F401
    except ImportError:
        missing.append("h2")
    try:
        import certifi  # noqa: F401
    except ImportError:
        missing.append("certifi")

    if not missing:
        return

    log(f"[!] Не хватает зависимостей: {', '.join(missing)}")
    if not auto_install:
        log("    Установи вручную: pip install " + " ".join(NEED_DEPS))
        sys.exit(1)

    log("    Ставлю автоматически: pip install " + " ".join(NEED_DEPS))
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
        subprocess.check_call([sys.executable, "-m", "pip", "install", *NEED_DEPS])
    except subprocess.CalledProcessError:
        log("[X] pip install не удался. Попробуй вручную:")
        log(f"    {sys.executable} -m pip install {' '.join(NEED_DEPS)}")
        sys.exit(1)
    log("    [ok] зависимости установлены")


def find_proxy_root() -> str | None:
    """Ищем корень исходников: папка с proxy/tg_ws_proxy.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        here,                                            # скрипт лежит в корне репозитория
        os.path.join(here, "tg-ws-proxy"),               # рядом клон
        os.path.join(here, "tg-ws-proxy-main"),          # рядом распакованный zip
        os.path.join(here, "tg-ws-proxy-src"),           # наша папка (см. ensure_source)
    ]
    for c in candidates:
        if os.path.isfile(os.path.join(c, "proxy", "tg_ws_proxy.py")):
            return c
    # вдруг скрипт положили внутрь репозитория в подпапку
    parent = os.path.dirname(here)
    if os.path.isfile(os.path.join(parent, "proxy", "tg_ws_proxy.py")):
        return parent
    return None


def ensure_source(auto_download: bool = True) -> str:
    if getattr(sys, "frozen", False):
        return "<exe>"  # движок proxy уже упакован в exe, скачивать нечего
    root = find_proxy_root()
    if root:
        return root

    if not auto_download:
        log("[X] Не нашёл proxy/tg_ws_proxy.py рядом со скриптом.")
        log("    Скачай https://github.com/Flowseal/tg-ws-proxy и положи скрипт в корень репозитория.")
        sys.exit(1)

    here = os.path.dirname(os.path.abspath(__file__))
    dest = os.path.join(here, "tg-ws-proxy-src")
    os.makedirs(dest, exist_ok=True)
    # может уже скачано, но find_proxy_root не нашёл из-за имени подпапки
    for name in os.listdir(dest):
        p = os.path.join(dest, name, "proxy", "tg_ws_proxy.py")
        if os.path.isfile(p):
            return os.path.join(dest, name)

    log("[*] Исходников рядом нет — качаю с GitHub...")
    zippath = os.path.join(dest, "tg-ws-proxy.zip")
    try:
        urllib.request.urlretrieve(REPO_ZIP_URL, zippath)
    except Exception as e:
        log(f"[X] Не смог скачать: {e}")
        sys.exit(1)

    try:
        with zipfile.ZipFile(zippath, "r") as z:
            z.extractall(dest)
    except Exception as e:
        log(f"[X] Не смог распаковать zip: {e}")
        sys.exit(1)
    finally:
        try:
            os.remove(zippath)
        except OSError:
            pass

    root = find_proxy_root()
    # fallback: ищем любую подпапку с proxy/tg_ws_proxy.py
    if not root:
        for name in os.listdir(dest):
            p = os.path.join(dest, name, "proxy", "tg_ws_proxy.py")
            if os.path.isfile(p):
                root = os.path.join(dest, name)
                break
    if not root:
        log("[X] Скачал, но proxy/tg_ws_proxy.py не найден. Проверь архив вручную.")
        sys.exit(1)
    log(f"    [ok] исходники в: {root}")
    return root


def get_or_create_secret(cli_secret: str | None) -> str:
    if cli_secret:
        s = cli_secret.strip().lower()
    elif os.path.isfile(SECRET_FILE):
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            s = f.read().strip().lower()
    else:
        s = ""

    if s.startswith("dd"):
        s = s[2:]  # в secret.txt храним чистый hex, dd добавляем только в ссылку
    if not re.fullmatch(r"[0-9a-f]{32}", s or ""):
        s = secrets.token_hex(16)
        with open(SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(s)
        log(f"[*] Сгенерировал новый secret и сохранил в secret.txt: {s}")
    else:
        # закрепим на диске, чтобы не менялся при перезапусках
        try:
            with open(SECRET_FILE, "w", encoding="utf-8") as f:
                f.write(s)
        except OSError:
            pass
    return s


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def check_port_free(port: int) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
    except OSError:
        log(f"[!] Порт {port} уже занят. Закрой другую копию прокси или запусти с --port 2443")
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Домашний запуск tg-ws-proxy: ПК + телефон в одном Wi-Fi")
    ap.add_argument("--port", type=int, default=1443, help="Порт прокси (по умолчанию 1443)")
    ap.add_argument("--host", type=str, default="0.0.0.0",
                    help="0.0.0.0 = доступно с телефона в том же Wi-Fi; 127.0.0.1 = только этот ПК")
    ap.add_argument("--secret", type=str, default=None, help="32 hex-символа. Если не указан — берётся из secret.txt или генерируется")
    ap.add_argument("--no-auto-install", action="store_true", help="Не ставить зависимости автоматически")
    ap.add_argument("--no-download", action="store_true", help="Не качать исходники с GitHub автоматически")
    ap.add_argument("-v", "--verbose", action="store_true", help="Подробные логи прокси")
    args = ap.parse_args()
    if os.name == "nt" and not getattr(sys, "frozen", False):  # подпись окна в панели задач
        try:
            os.system("title [1/3] Telegram MTProxy :1443")
        except Exception:
            pass

    ensure_deps(auto_install=not args.no_auto_install)
    proxy_root = ensure_source(auto_download=not args.no_download)
    secret = get_or_create_secret(args.secret)
    lan_ip = get_lan_ip()
    check_port_free(args.port)

    if proxy_root != "<exe>":  # в exe движок уже упакован, chdir не нужен
        if sys.path[0] != proxy_root:
            sys.path.insert(0, proxy_root)
        os.chdir(proxy_root)

    link_pc = f"tg://proxy?server=127.0.0.1&port={args.port}&secret=dd{secret}"
    link_phone = f"tg://proxy?server={lan_ip}&port={args.port}&secret=dd{secret}"

    print()
    print("=" * 60)
    print("  TG WS Proxy запущен (домашний режим)")
    print(f"  Слушаю: {args.host}:{args.port}")
    print(f"  Локальный IP для телефона: {lan_ip}")
    print()
    print("  Для ЭТОГО ПК (Telegram Desktop -> Настройки -> Прокси -> MTProto):")
    print(f"    сервер: 127.0.0.1   порт: {args.port}   secret: dd{secret}")
    print(f"    ссылка: {link_pc}")
    print()
    print("  Для ТЕЛЕФОНА (тот же Wi-Fi; Telegram -> Данные и память -> Прокси):")
    print(f"    сервер: {lan_ip}   порт: {args.port}   secret: dd{secret}")
    print(f"    ссылка: {link_phone}")
    print()
    print("  Телефон должен быть в ТОМ ЖЕ Wi-Fi, что и ПК.")
    print("  Если телефон не коннектится — разреши Python в брандмауэре Windows")
    print("  (при первом запуске Windows обычно сам спрашивает).")
    print("  Остановка: Ctrl+C")
    print("=" * 60)
    print(flush=True)

    if getattr(sys, "frozen", False):
        # Внутри exe: запускаем движок в этом же процессе (subprocess не нужен)
        sys.argv = ["tg_ws_proxy",
                    "--host", args.host, "--port", str(args.port),
                    "--secret", secret] + (["--verbose"] if args.verbose else [])
        try:
            from proxy.tg_ws_proxy import main as _tg_main
        except ImportError as e:
            log(f"[X] движок proxy не найден в сборке: {e}")
            sys.exit(2)
        try:
            _tg_main()
        except KeyboardInterrupt:
            log("\nОстановлено. Secret сохранён в secret.txt")
        return

    cmd = [sys.executable, "-m", "proxy.tg_ws_proxy",
           "--host", args.host, "--port", str(args.port), "--secret", secret]
    if args.verbose:
        cmd.append("--verbose")

    try:
        subprocess.check_call(cmd)
    except KeyboardInterrupt:
        print("\nОстановлено (Ctrl+C). Secret сохранён в secret.txt — настройки Telegram не слетят.")
    except subprocess.CalledProcessError as e:
        log(f"[X] Прокси завершился с ошибкой (код {e.returncode}).")
        log("    Частые причины: порт занят, нет интернета, старая версия исходников.")
        log("    Попробуй: python 1-telegram-mtproxy.py --port 2443 --verbose")
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()
