#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# [0/3] START ВСЁ ВМЕСТЕ (трей + панель)
# Запуск:  python 0-start-all-tray.py
#   - открывается ОБЫЧНОЕ окно (видно что происходит), его можно ЗАКРЫТЬ
#     в любой момент БЕЗ ПОСЛЕДСТВИЙ — прокси живут в фоне под иконкой в трее;
#   - двойной клик по иконке в трее (или пункт меню) открывает ПАНЕЛЬ:
#     живые консоли обоих прокси + настройки (порты, задержка, автозапуск).
#     Панель тоже можно закрывать сколько угодно — всё продолжит работать.
#   - локальный САЙТ для телефона: http://IP-ПК:8899 — подключить Telegram
#     и прокси одной кнопкой, перезапуск и настройки с телефона;
#   - python 0-start-all-tray.py --background  -> тихо, только трей
#   - --install-autostart / --remove-autostart -> автозапуск Windows
# Файлы рядом: 1-telegram-mtproxy.py  2-dpi-proxy.py  secret.txt  logs/
# ============================================================================
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))  # рядом с exe
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
TG_SCRIPT = os.path.join(HERE, "1-telegram-mtproxy.py")
DPI_SCRIPT = os.path.join(HERE, "2-dpi-proxy.py")
SECRET_FILE = os.path.join(HERE, "secret.txt")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
LOCK_FILE = os.path.join(HERE, "supervisor.lock")
LOG_DIR = os.path.join(HERE, "logs")
TG_LOG = os.path.join(LOG_DIR, "tg-1443.log")
DPI_LOG = os.path.join(LOG_DIR, "dpi-8888.log")
STARTUP_BAT_NAME = "home-proxies-autostart.bat"

DEFAULTS = {"tg_port": 1443, "dpi_port": 8888, "delay": 0.005, "web_port": 8899}
CUR = dict(DEFAULTS)  # текущие настройки (файл -> аргументы -> панель)

procs: dict = {"tg": None, "dpi": None}
_open_logs: dict = {}
TRAY_ICON = None

_panel_thread = None
_panel_raise = threading.Event()
_panel_close_req = threading.Event()


def log(msg: str = "") -> None:
    try:  # под pythonw консоли нет — молча терпим
        print(msg, flush=True)
    except Exception:
        pass


# ---------- настройки ----------

def load_settings() -> None:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in DEFAULTS:
            if k in data:
                CUR[k] = data[k]
    except Exception:
        pass


def save_settings() -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(CUR, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[X] не смог сохранить settings.json: {e}")


# ---------- сеть/инфо ----------

def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def read_secret() -> str:
    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            s = f.read().strip().lower()
        if s.startswith("dd"):
            s = s[2:]
        return s
    except Exception:
        return ""


def phone_text() -> str:
    lan = get_lan_ip()
    s = read_secret()
    lines = [
        f"Телефон должен быть в ТОМ ЖЕ Wi-Fi, что и ПК ({lan}).",
        "",
        "Telegram: Настройки -> Данные и память -> Прокси ->",
        f"  сервер {lan}  порт {CUR['tg_port']}",
    ]
    if s:
        lines.append(f"  secret dd{s}")
        lines.append(f"  ссылка tg://proxy?server={lan}&port={CUR['tg_port']}&secret=dd{s}")
    else:
        lines.append("  (secret появится в secret.txt после старта TG-прокси)")
    lines += [
        "",
        "Браузер/YouTube/Discord: Wi-Fi -> прокси вручную ->",
        f"  сервер {lan}  порт {CUR['dpi_port']}",
    ]
    return "\n".join(lines)


def read_tail(path: str, max_lines: int = 400, max_bytes: int = 40000) -> str:
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, sz - max_bytes))
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        if sz > max_bytes and lines:
            lines = lines[1:]
        out = "\n".join(lines[-max_lines:])
        return out if out.strip() else "(пока пусто)"
    except Exception:
        return "(пока пусто — логи появятся после старта)"


# ---------- процессы прокси (всегда в фоне, без окон) ----------

def is_alive(name: str) -> bool:
    p = procs.get(name)
    return bool(p and p.poll() is None)


def stop_one(name: str) -> None:
    # Гасим всё дерево (TG-скрипт порождает внучатый процесс),
    # иначе порт останется занят.
    p = procs.get(name)
    procs[name] = None
    fh = _open_logs.pop(name, None)
    try:
        if fh:
            fh.close()
    except Exception:
        pass
    if not p or p.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        else:
            import signal
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def start_one(name: str) -> None:
    # Всегда БЕЗ окна: консоль тут только просмотрщик, закрытие безопасно.
    stop_one(name)
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        # В exe воркеры — это мы сами с флагом --run-worker
        cmd = [sys.executable, "--run-worker", name,
               "--tg-port", str(CUR["tg_port"]),
               "--dpi-port", str(CUR["dpi_port"]),
               "--delay", str(CUR["delay"]),
               "--web-port", str(CUR["web_port"])]
    elif name == "tg":
        cmd = [sys.executable, TG_SCRIPT, "--port", str(CUR["tg_port"])]
    else:
        cmd = [sys.executable, DPI_SCRIPT, "--port", str(CUR["dpi_port"]),
               "--delay", str(CUR["delay"])]
    log_fh = None
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fname = "tg-1443.log" if name == "tg" else "dpi-8888.log"
        log_fh = open(os.path.join(LOG_DIR, fname), "a",
                      encoding="utf-8", buffering=1)
        log_fh.write(f"\n===== старт {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        log_fh.flush()
        _open_logs[name] = log_fh
    except Exception:
        log_fh = None
    flags = 0
    if os.name == "nt" and not frozen:
        # в windowed-exe консоли и так нет
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        procs[name] = subprocess.Popen(
            cmd, cwd=HERE, creationflags=flags, start_new_session=True,
            stdout=log_fh, stderr=subprocess.STDOUT if log_fh else None,
            stdin=subprocess.DEVNULL)
    except Exception as e:
        log(f"[X] не смог запустить {name}: {e}")


def start_all() -> None:
    start_one("tg")
    time.sleep(1)
    start_one("dpi")


def stop_all() -> None:
    stop_one("tg")
    stop_one("dpi")


# ---------- один супервизор ----------

def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def supervisor_running() -> bool:
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        return _pid_alive(pid)
    except Exception:
        return False


def write_lock() -> None:
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def remove_lock() -> None:
    try:
        if os.path.isfile(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


def is_pythonw() -> bool:
    return sys.executable.lower().endswith("pythonw.exe")


def pythonw_exe() -> str:
    py = sys.executable
    if py.lower().endswith("python.exe"):
        cand = py[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(cand):
            return cand
    return py


def resource_base() -> str:
    # В exe файлы-воркеры лежат в _MEIPASS/workers, иначе — рядом со скриптом
    mp = getattr(sys, "_MEIPASS", None)
    return str(mp) if mp else HERE


def self_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(__file__)


def run_bundled_worker(which: str) -> None:
    # Режим воркера внутри exe: исполняет скрипт-воркер этим же интерпретатором.
    if sys.stdout is None:
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass
    if sys.stderr is None:
        sys.stderr = sys.stdout
    name = "1-telegram-mtproxy.py" if which == "tg" else "2-dpi-proxy.py"
    path = os.path.join(resource_base(), "workers", name)
    if not os.path.isfile(path):
        path = os.path.join(HERE, name)  # fallback для .py-режима
    if which == "tg":
        sys.argv = [path, "--port", str(CUR["tg_port"])]
    else:
        sys.argv = [path, "--port", str(CUR["dpi_port"]),
                    "--delay", str(CUR["delay"])]
    import runpy
    runpy.run_path(path, run_name="__main__")


def spawn_supervisor() -> bool:
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--detached",
               "--tg-port", str(CUR["tg_port"]),
               "--dpi-port", str(CUR["dpi_port"]),
               "--delay", str(CUR["delay"]),
               "--web-port", str(CUR["web_port"])]
    else:
        cmd = [pythonw_exe(), self_path(), "--detached",
               "--tg-port", str(CUR["tg_port"]),
               "--dpi-port", str(CUR["dpi_port"]),
               "--delay", str(CUR["delay"]),
               "--web-port", str(CUR["web_port"])]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.Popen(cmd, cwd=HERE, creationflags=flags,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)
    return True


# ---------- буфер обмена / окна ----------

def copy_text(s: str) -> bool:
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(s)
        r.update()
        r.destroy()
        return True
    except Exception as e:
        log(f"[X] буфер обмена недоступен: {e}")
        return False


def open_logs_dir() -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.name == "nt":
            os.startfile(LOG_DIR)  # type: ignore
        else:
            log(f"[*] логи: {LOG_DIR}")
    except Exception as e:
        log(f"[X] не смог открыть логи: {e}")


def show_phone_info() -> None:
    txt = phone_text()
    log("\n" + txt + "\n")
    try:
        from tkinter import messagebox
        messagebox.showinfo("Настройки для телефона", txt)
    except Exception:
        pass


# ---------- локальный сайт для телефона ----------

WEB = {"server": None, "thread": None}

DASH_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Дом-прокси</title>
<style>
body{font-family:sans-serif;background:#14161a;color:#eee;margin:0;padding:12px}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:16px;margin:0 0 8px}
.card{background:#1e2228;border-radius:12px;padding:12px;margin-bottom:12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{border:0;border-radius:10px;padding:10px 14px;font-size:15px;background:#2e8b57;color:#fff}
button:active{opacity:.7}button.gray{background:#3a4048}button.red{background:#8b2e2e}
.dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px;vertical-align:baseline}
.on{background:#2ecc71}.off{background:#e74c3c}
code{background:#0f1114;padding:2px 6px;border-radius:6px;word-break:break-all}
input,select{background:#0f1114;color:#eee;border:1px solid #3a4048;border-radius:8px;padding:8px;font-size:15px;width:110px}
label{font-size:14px;color:#bbb}pre{background:#0f1114;border-radius:8px;padding:8px;font-size:12px;max-height:220px;overflow:auto;white-space:pre-wrap}
a.tg{display:inline-block;background:#2e8b57;color:#fff;border-radius:10px;padding:10px 14px;text-decoration:none;font-size:15px}
.small{font-size:13px;color:#999}
</style></head><body>
<h1>Дом-прокси</h1>
<div class="card"><h2>Статус</h2><div id="status">...</div></div>
<div class="card"><h2>Подключить этот телефон</h2>
<div id="conn">...</div>
<p class="small">Телефон должен быть в том же Wi-Fi, что и ПК.</p></div>
<div class="card"><h2>Управление ноутбуком</h2><div class="row">
<button onclick="ctl('restart_tg')">TG заново</button>
<button onclick="ctl('restart_dpi')">DPI заново</button>
<button onclick="ctl('restart_all')">Всё заново</button>
<button class="gray" onclick="ctl('stop_all')">Остановить всё</button>
<button class="gray" onclick="ctl('start_all')">Запустить всё</button>
</div><p class="small" id="msg"></p></div>
<div class="card"><h2>Настройки</h2>
<div class="row"><label>TG порт <input id="s_tg" inputmode="numeric"></label>
<label>DPI порт <input id="s_dpi" inputmode="numeric"></label></div>
<div class="row"><label>WEB порт <input id="s_web" inputmode="numeric"></label>
<label>Задержка <input id="s_delay" inputmode="decimal"></label></div>
<div class="row"><label><input type="checkbox" id="s_auto" style="width:auto"> автозапуск Windows</label>
<button onclick="save()">Сохранить и перезапустить</button></div>
<p class="small">WEB порт 0 = выключить сайт. Смена WEB порта — переподключись на новый адрес.</p></div>
<div class="card"><h2>Логи</h2><div class="row">
<button class="gray" onclick="logs()">Обновить</button>
<label><input type="checkbox" id="s_autolog" style="width:auto" checked> авто</label></div>
<pre id="l_tg">...</pre><pre id="l_dpi">...</pre></div>
<script>
async function jget(p){const r=await fetch(p);return r.json();}
async function jpost(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json();}
function copy(t){if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t);}else{const e=document.createElement('textarea');e.value=t;document.body.appendChild(e);e.select();try{document.execCommand('copy');}catch(_){}e.remove();}}
let S={};
async function refresh(){
 try{S=await jget('/api/status');}catch(e){document.getElementById('status').innerHTML='нет связи с ПК';return;}
 const d=(on)=>'<span class="dot '+(on?'on':'off')+'"></span>';
 document.getElementById('status').innerHTML=
  d(S.tg_alive)+'Telegram :'+S.tg_port+' '+(S.tg_alive?'работает':'ОСТАНОВЛЕН')+'<br>'+
  d(S.dpi_alive)+'DPI :'+S.dpi_port+' '+(S.dpi_alive?'работает':'ОСТАНОВЛЕН');
 let c='';
 if(S.secret){
  c+='<p>Telegram: сервер <code>'+S.lan+'</code> порт <code>'+S.tg_port+'</code> secret <code>dd'+S.secret+'</code></p>';
  c+='<div class="row"><a class="tg" href="'+S.tg_link+'">Открыть в Telegram</a><button class="gray" onclick="copy(S.tg_link)">Скопировать ссылку</button></div>';
 }else{c+='<p>secret пока нет — TG-прокси ещё стартует.</p>';}
 c+='<p>Браузер/YouTube: прокси вручную <code>'+S.lan+':'+S.dpi_port+'</code> <button class="gray" onclick="copy(S.lan+\\':\\'+S.dpi_port)">Скопировать</button></p>';
 document.getElementById('conn').innerHTML=c;
 if(document.activeElement===null||document.activeElement.tagName!=='INPUT'){
  document.getElementById('s_tg').value=S.tg_port;document.getElementById('s_dpi').value=S.dpi_port;
  document.getElementById('s_web').value=S.web_port;document.getElementById('s_delay').value=S.delay;
  document.getElementById('s_auto').checked=S.autostart;
 }
}
async function ctl(a){document.getElementById('msg').textContent='...';const r=await jpost('/api/control',{action:a});document.getElementById('msg').textContent=r.ok?'готово':'ошибка';refresh();}
async function save(){
 const b={tg_port:parseInt(document.getElementById('s_tg').value),dpi_port:parseInt(document.getElementById('s_dpi').value),web_port:parseInt(document.getElementById('s_web').value),delay:parseFloat((document.getElementById('s_delay').value||'').replace(',','.')),autostart:document.getElementById('s_auto').checked};
 document.getElementById('msg').textContent='...';const r=await jpost('/api/settings',b);
 document.getElementById('msg').textContent=r.ok?('применено'+(r.reconnect?(' — открой '+r.reconnect):'')):('ошибка: '+(r.error||''));
 if(r.ok&&r.reconnect){setTimeout(()=>{location.href=r.reconnect;},1500);}
 refresh();
}
async function logs(){
 try{
  const t=await (await fetch('/api/logs?which=tg')).text();
  const d=await (await fetch('/api/logs?which=dpi')).text();
  document.getElementById('l_tg').textContent='[TG]\\n'+t;
  document.getElementById('l_dpi').textContent='[DPI]\\n'+d;
 }catch(e){}
}
setInterval(refresh,5000);refresh();logs();
setInterval(()=>{if(document.getElementById('s_autolog').checked)logs();},5000);
</script></body></html>"""


def site_url() -> str:
    if not CUR.get("web_port"):
        return ""
    return f"http://{get_lan_ip()}:{CUR['web_port']}"


def status_payload() -> dict:
    lan = get_lan_ip()
    s = read_secret()
    return {
        "tg_alive": is_alive("tg"), "dpi_alive": is_alive("dpi"),
        "tg_port": CUR["tg_port"], "dpi_port": CUR["dpi_port"],
        "delay": CUR["delay"], "web_port": CUR["web_port"],
        "autostart": autostart_enabled(), "lan": lan, "secret": s,
        "tg_link": (f"tg://proxy?server={lan}&port={CUR['tg_port']}&secret=dd{s}"
                    if s else ""),
    }


def validate_settings(data: dict) -> tuple[bool, str]:
    try:
        tg = int(data.get("tg_port"))
        dpi = int(data.get("dpi_port"))
        web = int(data.get("web_port"))
        dl = float(str(data.get("delay")).replace(",", "."))
    except (TypeError, ValueError):
        return False, "порты — целые числа, задержка — число"
    if not (1 <= tg <= 65535 and 1 <= dpi <= 65535):
        return False, "порты 1-65535"
    if not (0 <= web <= 65535):
        return False, "WEB порт 0-65535 (0 = выкл)"
    if not (0 <= dl <= 1):
        return False, "задержка 0-1"
    if tg == dpi or (web and (web == tg or web == dpi)):
        return False, "порты должны различаться"
    return True, ""


def apply_settings(data: dict) -> dict:
    ok, err = validate_settings(data)
    if not ok:
        return {"ok": False, "error": err}
    old_web = CUR["web_port"]
    CUR["tg_port"] = int(data["tg_port"])
    CUR["dpi_port"] = int(data["dpi_port"])
    CUR["web_port"] = int(data["web_port"])
    CUR["delay"] = float(str(data["delay"]).replace(",", "."))
    if isinstance(data.get("autostart"), bool):
        if data["autostart"]:
            install_autostart()
        else:
            remove_autostart()
    save_settings()
    start_all()
    if CUR["web_port"] != old_web:
        start_web()
        if CUR["web_port"]:
            return {"ok": True,
                    "reconnect": f"http://{get_lan_ip()}:{CUR['web_port']}"}
    return {"ok": True}


class DashHandler(BaseHTTPRequestHandler):
    server_version = "HomeProxy/1.0"

    def log_message(self, fmt, *args):  # тихо, иначе спам
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(DASH_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(status_payload())
        elif path == "/api/logs":
            which = "dpi" if "which=dpi" in self.path else "tg"
            self._send(read_tail(DPI_LOG if which == "dpi" else TG_LOG,
                                 max_lines=200).encode("utf-8"),
                       "text/plain; charset=utf-8")
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            ln = int(self.headers.get("Content-Length") or 0)
        except Exception:
            ln = 0
        data = {}
        if 0 < ln <= 8192:
            try:
                raw = self.rfile.read(ln)
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._json({"ok": False, "error": "bad json"}, 400)
                return
        if path == "/api/control":
            a = data.get("action")
            if a == "restart_tg":
                threading.Thread(target=lambda: start_one("tg"), daemon=True).start()
            elif a == "restart_dpi":
                threading.Thread(target=lambda: start_one("dpi"), daemon=True).start()
            elif a == "restart_all":
                threading.Thread(target=start_all, daemon=True).start()
            elif a == "stop_all":
                threading.Thread(target=stop_all, daemon=True).start()
            elif a == "start_all":
                threading.Thread(target=start_all, daemon=True).start()
            else:
                self._json({"ok": False, "error": "unknown action"}, 400)
                return
            self._json({"ok": True})
        elif path == "/api/settings":
            self._json(apply_settings(data if isinstance(data, dict) else {}))
        elif path == "/api/autostart":
            on = bool(data.get("on"))
            self._json({"ok": install_autostart() if on else remove_autostart()})
        else:
            self._json({"ok": False, "error": "not found"}, 404)


def start_web() -> None:
    stop_web()
    port = int(CUR.get("web_port") or 0)
    if not port:
        return
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), DashHandler)
        srv.daemon_threads = True
        WEB["server"] = srv
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="web")
        t.start()
        WEB["thread"] = t
        log(f"[*] сайт для телефона: {site_url()}")
    except Exception as e:
        log(f"[X] сайт не стартовал на порту {port}: {e}")
        WEB["server"] = None


def stop_web() -> None:
    srv = WEB.get("server")
    WEB["server"] = None
    WEB["thread"] = None
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass


# ---------- автозапуск Windows ----------

def startup_dir() -> str | None:
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")


def autostart_bat() -> str | None:
    d = startup_dir()
    return os.path.join(d, STARTUP_BAT_NAME) if d else None


def autostart_enabled() -> bool:
    p = autostart_bat()
    return bool(p and os.path.isfile(p))


def install_autostart() -> bool:
    p = autostart_bat()
    if not p:
        log("[X] автозапуск настраивается только на Windows")
        return False
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
            if getattr(sys, "frozen", False):
                f.write(f'start "" /min "{self_path()}" '
                        f"--detached --tg-port {CUR['tg_port']} "
                        f"--dpi-port {CUR['dpi_port']} --delay {CUR['delay']} "
                        f"--web-port {CUR['web_port']}\r\n")
            else:
                f.write(f'start "" /min "{pythonw_exe()}" "{self_path()}" '
                        f"--detached --tg-port {CUR['tg_port']} "
                        f"--dpi-port {CUR['dpi_port']} --delay {CUR['delay']} "
                        f"--web-port {CUR['web_port']}\r\n")
        log(f"[*] автозапуск включён: {p}")
        return True
    except Exception as e:
        log(f"[X] не смог записать автозапуск: {e}")
        return False


def remove_autostart() -> bool:
    p = autostart_bat()
    if not p:
        log("[X] автозапуск настраивается только на Windows")
        return False
    try:
        if os.path.isfile(p):
            os.remove(p)
            log("[*] автозапуск выключен")
        else:
            log("[*] автозапуска и не было")
        return True
    except Exception as e:
        log(f"[X] не смог убрать автозапуск: {e}")
        return False


# ---------- панель: консоли + настройки ----------

def show_panel() -> None:
    global _panel_thread
    if _panel_thread is not None and _panel_thread.is_alive():
        _panel_raise.set()  # уже открыта — просто поднять
        return
    try:
        import tkinter  # noqa: F401
    except Exception:
        log("[!] панель недоступна (нет tkinter) — открываю папку логов")
        open_logs_dir()
        return
    _panel_raise.clear()
    _panel_close_req.clear()
    _panel_thread = threading.Thread(target=panel_main, daemon=True, name="panel")
    _panel_thread.start()


def panel_main() -> None:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox

    root = tk.Tk()
    root.title("Дом-прокси: консоли и настройки")
    root.geometry("780x600")
    try:
        _ico = os.path.join(resource_base(), "icon.ico")
        if not os.path.isfile(_ico):
            _ico = os.path.join(HERE, "icon.ico")
        if os.path.isfile(_ico):
            root.iconbitmap(_ico)
    except Exception:
        pass

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=8)

    # --- вкладка консолей ---
    tab_log = ttk.Frame(nb)
    nb.add(tab_log, text="Консоли")
    ttk.Label(tab_log, text="TG :1443 — Telegram (живой лог, окно можно закрывать)").pack(anchor="w")
    txt_tg = scrolledtext.ScrolledText(tab_log, height=11, state="disabled")
    txt_tg.pack(fill="both", expand=True)
    ttk.Label(tab_log, text="DPI :8888 — YouTube/Discord (живой лог, окно можно закрывать)").pack(anchor="w")
    txt_dpi = scrolledtext.ScrolledText(tab_log, height=11, state="disabled")
    txt_dpi.pack(fill="both", expand=True)
    pause_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(tab_log, text="Пауза (не обновлять логи)",
                    variable=pause_var).pack(anchor="w", pady=2)

    def set_text(w, text: str) -> None:
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("end", text)
        w.configure(state="disabled")
        w.see("end")

    # --- вкладка настроек ---
    tab_set = ttk.Frame(nb)
    nb.add(tab_set, text="Настройки")
    tg_port_var = tk.StringVar(value=str(CUR["tg_port"]))
    dpi_port_var = tk.StringVar(value=str(CUR["dpi_port"]))
    delay_var = tk.StringVar(value=str(CUR["delay"]))
    web_port_var = tk.StringVar(value=str(CUR["web_port"]))
    autostart_var = tk.BooleanVar(value=autostart_enabled())
    status_var = tk.StringVar(value="")
    web_var = tk.StringVar(value="")

    frm = ttk.Frame(tab_set)
    frm.pack(fill="x", padx=10, pady=10)
    ttk.Label(frm, text="Порт Telegram").grid(row=0, column=0, sticky="w", padx=4, pady=4)
    ttk.Entry(frm, textvariable=tg_port_var, width=10).grid(row=0, column=1, padx=4)
    ttk.Label(frm, text="Порт DPI (YouTube/Discord)").grid(row=1, column=0, sticky="w", padx=4, pady=4)
    ttk.Entry(frm, textvariable=dpi_port_var, width=10).grid(row=1, column=1, padx=4)
    ttk.Label(frm, text="Задержка фрагментации, сек").grid(row=2, column=0, sticky="w", padx=4, pady=4)
    ttk.Entry(frm, textvariable=delay_var, width=10).grid(row=2, column=1, padx=4)
    ttk.Label(frm, text="WEB порт сайта (0 = выкл)").grid(row=3, column=0, sticky="w", padx=4, pady=4)
    ttk.Entry(frm, textvariable=web_port_var, width=10).grid(row=3, column=1, padx=4)
    ttk.Checkbutton(frm, text="Автозапуск Windows (тихо, только трей)",
                    variable=autostart_var).grid(row=4, column=0, columnspan=2,
                                                 sticky="w", padx=4, pady=8)
    ttk.Label(tab_set, textvariable=status_var, foreground="green").pack(anchor="w", padx=10)
    ttk.Label(tab_set, textvariable=web_var, foreground="blue").pack(anchor="w", padx=10)

    def do_in_thread(fn):
        threading.Thread(target=fn, daemon=True).start()

    def on_apply():
        def work():
            data = {
                "tg_port": tg_port_var.get().strip(),
                "dpi_port": dpi_port_var.get().strip(),
                "web_port": web_port_var.get().strip(),
                "delay": delay_var.get().strip(),
                "autostart": bool(autostart_var.get()),
            }
            res = apply_settings(data)
            if res.get("ok"):
                status_var.set(
                    f"Применено: TG :{CUR['tg_port']}, "
                    f"DPI :{CUR['dpi_port']}, WEB :{CUR['web_port']}.")
            else:
                try:
                    messagebox.showerror("Ошибка", res.get("error", "ошибка"))
                except Exception:
                    status_var.set("Ошибка: " + str(res.get("error", "")))
        do_in_thread(work)

    btns = ttk.Frame(tab_set)
    btns.pack(fill="x", padx=10, pady=6)
    ttk.Button(btns, text="Сохранить и перезапустить", command=on_apply).pack(side="left", padx=2)
    ttk.Button(btns, text="Перезапустить всё",
               command=lambda: do_in_thread(start_all)).pack(side="left", padx=2)
    ttk.Button(btns, text="Остановить всё",
               command=lambda: do_in_thread(stop_all)).pack(side="left", padx=2)
    ttk.Button(btns, text="Настройки телефона",
               command=lambda: do_in_thread(show_phone_info)).pack(side="left", padx=2)
    ttk.Button(btns, text="Закрыть панель (всё продолжит работать)",
               command=root.destroy).pack(side="left", padx=2)

    def refresh():
        try:
            if not pause_var.get():
                set_text(txt_tg, read_tail(TG_LOG))
                set_text(txt_dpi, read_tail(DPI_LOG))
            tg = "работает" if is_alive("tg") else "ОСТАНОВЛЕН"
            dpi = "работает" if is_alive("dpi") else "ОСТАНОВЛЕН"
            status_var.set(f"Статус: TG :{CUR['tg_port']} — {tg} | DPI :{CUR['dpi_port']} — {dpi}")
            try:
                web_var.set(f"Сайт для телефона: {site_url()}" if CUR.get("web_port") else "Сайт выключен (WEB порт = 0)")
            except Exception:
                pass
        except Exception:
            return  # окно уже закрыто
        try:
            root.after(1000, refresh)
        except Exception:
            pass

    def poll():
        try:
            if _panel_close_req.is_set():
                try:
                    root.destroy()
                except Exception:
                    pass
                return
            if _panel_raise.is_set():
                _panel_raise.clear()
                try:
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                except Exception:
                    pass
            root.after(200, poll)
        except Exception:
            pass

    # Закрытие крестиком = просто скрыть, прокси продолжают работать
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    refresh()
    poll()
    root.mainloop()


# ---------- трей ----------

def ensure_tray_deps() -> bool:
    need = []
    try:
        import pystray  # noqa: F401
    except ImportError:
        need.append("pystray")
    try:
        import PIL  # noqa: F401
    except ImportError:
        need.append("Pillow")
    if not need:
        return True
    log(f"[*] Ставлю зависимости трея: {' '.join(need)}")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *need])
        return True
    except Exception as e:
        log(f"[!] pip install не удался: {e}")
        return False


def make_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), (25, 25, 25))
    d = ImageDraw.Draw(img)
    d.ellipse([6, 6, 58, 58], fill=(46, 204, 113))   # TG: зелёный
    d.ellipse([20, 20, 44, 44], fill=(52, 152, 219))  # DPI: синий
    return img


def run_tray() -> None:
    import pystray

    def status_text(item):
        tg = "работает" if is_alive("tg") else "ОСТАНОВЛЕН"
        dpi = "работает" if is_alive("dpi") else "ОСТАНОВЛЕН"
        return f"TG :{CUR['tg_port']} — {tg} | DPI :{CUR['dpi_port']} — {dpi}"

    def tg_text(item):
        return f"Перезапустить Telegram :{CUR['tg_port']}"

    def dpi_text(item):
        return f"Перезапустить DPI :{CUR['dpi_port']}"

    def autostart_text(item):
        return ("Автозапуск Windows: ВКЛ (выключить)"
                if autostart_enabled() else "Автозапуск Windows: выкл (включить)")

    def threaded(fn):
        def wrap(icon, item):
            threading.Thread(target=fn, daemon=True).start()
        return wrap

    def on_autostart():
        if autostart_enabled():
            remove_autostart()
        else:
            install_autostart()

    def on_exit(icon, item):
        log("[*] Останавливаю всё и выхожу...")
        _panel_close_req.set()
        time.sleep(0.3)
        stop_all()
        icon.stop()

    def noop(icon, item):
        pass

    def on_open_site(icon, item):
        def work():
            url = site_url()
            if not url:
                log("[!] сайт выключен (web_port = 0)")
                return
            try:
                import webbrowser
                webbrowser.open(f"http://127.0.0.1:{CUR['web_port']}")
            except Exception as e:
                log(f"[X] не смог открыть браузер: {e}")
        threading.Thread(target=work, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть панель (консоли и настройки)",
                         threaded(show_panel), default=True),
        pystray.MenuItem(status_text, noop, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(tg_text, threaded(lambda: start_one("tg"))),
        pystray.MenuItem(dpi_text, threaded(lambda: start_one("dpi"))),
        pystray.MenuItem("Перезапустить всё", threaded(start_all)),
        pystray.MenuItem("Остановить всё", threaded(stop_all)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Настройки для телефона...", threaded(show_phone_info)),
        pystray.MenuItem("Открыть сайт (этот ПК)", on_open_site),
        pystray.MenuItem("Скопировать TG-ссылку (телефон)", threaded(copy_tg_link)),
        pystray.MenuItem("Скопировать адрес DPI (телефон)", threaded(copy_dpi_addr)),
        pystray.MenuItem("Открыть папку логов", threaded(open_logs_dir)),
        pystray.MenuItem(autostart_text, threaded(on_autostart)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход (остановить всё)", on_exit),
    )
    global TRAY_ICON
    TRAY_ICON = pystray.Icon("home-proxies", make_icon_image(),
                             f"Дом: TG :{CUR['tg_port']} + DPI :{CUR['dpi_port']}", menu)
    TRAY_ICON.run()


def copy_tg_link() -> None:
    s = read_secret()
    if not s:
        log("[!] secret.txt пока пуст — TG-прокси ещё стартует")
        return
    lan = get_lan_ip()
    if copy_text(f"tg://proxy?server={lan}&port={CUR['tg_port']}&secret=dd{s}"):
        log("[*] TG-ссылка для телефона скопирована")


def copy_dpi_addr() -> None:
    if copy_text(f"{get_lan_ip()}:{CUR['dpi_port']}"):
        log("[*] адрес DPI-прокси для телефона скопирован")


# ---------- режимы запуска ----------

def run_supervisor(with_tray: bool) -> None:
    if supervisor_running():
        log("[!] супервизор уже запущен — выхожу (это окно можно закрыть)")
        return
    write_lock()
    try:
        start_all()
        start_web()
        lan = get_lan_ip()
        log("")
        log("=" * 60)
        log("  ОБА ЗАПУЩЕНЫ (в фоне, окон у них нет):")
        log(f"  [1/3] Telegram  127.0.0.1:{CUR['tg_port']}  (телефон: {lan}:{CUR['tg_port']})")
        log(f"  [2/3] DPI proxy 127.0.0.1:{CUR['dpi_port']}  (телефон: {lan}:{CUR['dpi_port']})")
        log("  Двойной клик по иконке в трее — панель: консоли + настройки.")
        if CUR.get("web_port"):
            log(f"  Сайт для телефона: {site_url()}  (тот же Wi-Fi)")
        log("=" * 60)
        if with_tray and ensure_tray_deps():
            run_tray()
        else:
            if with_tray:
                log("[!] Трей недоступен — слежу за прокси в этой консоли. Стоп: Ctrl+C")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        stop_web()
        stop_all()
        remove_lock()


def run_viewer() -> None:
    # Видимое окно-наблюдатель: показывает живые логи, закрыть можно всегда.
    lan = get_lan_ip()
    log("")
    log("=" * 60)
    log("  ЭТО ОКНО — ТОЛЬКО ПРОСМОТР, его можно закрыть в любой момент:")
    log("  прокси живут в фоне под иконкой в трее и продолжат работать.")
    log(f"  [1/3] Telegram  127.0.0.1:{CUR['tg_port']}  (телефон: {lan}:{CUR['tg_port']})")
    log(f"  [2/3] DPI proxy 127.0.0.1:{CUR['dpi_port']}  (телефон: {lan}:{CUR['dpi_port']})")
    if CUR.get("web_port"):
        log(f"  Сайт для телефона: {site_url()}  (тот же Wi-Fi)")
    log("  Двойной клик по иконке в трее — панель: консоли + настройки.")
    log("  Стоп всего: правый клик по трею -> Выход. Стоп этого окна: Ctrl+C")
    log("=" * 60)
    offs = {}
    for key, path in (("TG", TG_LOG), ("DPI", DPI_LOG)):
        try:
            offs[key] = os.path.getsize(path)
        except Exception:
            offs[key] = 0
        tail = read_tail(path, max_lines=15).splitlines()
        for line in tail[-15:]:
            log(f"[{key}] {line}")
    try:
        while True:
            time.sleep(0.5)
            for key, path in (("TG", TG_LOG), ("DPI", DPI_LOG)):
                try:
                    if not os.path.isfile(path):
                        continue
                    sz = os.path.getsize(path)
                    if sz < offs[key]:
                        offs[key] = 0
                    if sz > offs[key]:
                        with open(path, "rb") as f:
                            f.seek(offs[key])
                            data = f.read()
                        offs[key] = sz
                        for line in data.decode("utf-8", "replace").splitlines():
                            log(f"[{key}] {line}")
                except Exception:
                    pass
    except KeyboardInterrupt:
        log("Окно просмотра закрыто. Прокси продолжают работать в фоне.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Дом-прокси: TG + DPI, трей и панель")
    ap.add_argument("--tg-port", type=int, default=None)
    ap.add_argument("--dpi-port", type=int, default=None)
    ap.add_argument("--delay", type=float, default=None)
    ap.add_argument("--web-port", type=int, default=None,
                    help="порт сайта для телефона (8899, 0 = выкл)")
    ap.add_argument("--no-tray", action="store_true", help="супервизор в этой консоли, без трея")
    ap.add_argument("--background", action="store_true",
                    help="тихо: только трей, без окна просмотра")
    ap.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--run-worker", choices=["tg", "dpi"], default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--install-autostart", action="store_true",
                    help="прописать в автозапуск Windows и выйти")
    ap.add_argument("--remove-autostart", action="store_true",
                    help="убрать из автозапуска Windows и выйти")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    load_settings()
    if args.tg_port:
        CUR["tg_port"] = args.tg_port
    if args.dpi_port:
        CUR["dpi_port"] = args.dpi_port
    if args.delay is not None:
        CUR["delay"] = args.delay
    if args.web_port is not None:
        CUR["web_port"] = args.web_port

    if args.run_worker:
        run_bundled_worker(args.run_worker)
        return

    if args.install_autostart:
        sys.exit(0 if install_autostart() else 1)
    if args.remove_autostart:
        sys.exit(0 if remove_autostart() else 1)

    frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        # В exe воркеры упакованы внутрь, файлы рядом не нужны
        missing = [p for p in (TG_SCRIPT, DPI_SCRIPT) if not os.path.isfile(p)]
        if missing:
            log("[X] Рядом должны лежать:")
            log("    1-telegram-mtproxy.py")
            log("    2-dpi-proxy.py")
            log("  Положи все три файла в одну папку.")
            sys.exit(1)

    # Супервизор: detached-процесс, pythonw, явный --no-tray
    # или упакованный exe (у windowed-exe консоли нет, вьювер ему не нужен).
    if args.detached or args.no_tray or is_pythonw() or frozen:
        run_supervisor(with_tray=not args.no_tray)
        return

    # Обычный запуск: супервизор в фон, а это окно — безопасный просмотр
    if not supervisor_running():
        try:
            spawn_supervisor()
            time.sleep(2)
        except Exception as e:
            log(f"[!] не смог уйти в фон ({e}) — работаю супервизором в этом окне")
            run_supervisor(with_tray=True)
            return
    if args.background:
        return  # тихо: только трей
    run_viewer()


if __name__ == "__main__":
    main()
