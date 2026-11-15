"""Accesso locale alle VMC Aircare: il loro server HTTP in rete di casa, senza cloud.

Ogni macchina espone le proprieta' come percorsi: GET /speed legge, POST /speed
(corpo = valore) scrive. /esp_serial identifica la macchina, cosi' un cambio di
indirizzo IP non fa mai parlare con la VMC sbagliata.

Funzioni bloccanti (solo libreria standard): vanno eseguite in un executor.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

FAST_PIDS = ("speed", "mode")
SLOW_PIDS = ("temp_int", "temp_ext", "hr_int", "d_filter")
TIMEOUT = 4

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class LocalError(Exception):
    """La VMC non ha risposto (o ha risposto male) in rete locale."""


class WrongDevice(LocalError):
    """A quell'indirizzo ora risponde un'altra macchina."""


def _lock(host: str) -> threading.Lock:
    # il server della VMC regge una richiesta alla volta
    with _locks_guard:
        return _locks.setdefault(host, threading.Lock())


def _request(host: str, path: str, data: bytes | None = None,
             timeout: float = TIMEOUT, tries: int = 2) -> str:
    url = f"http://{host}/{path}"
    err: Exception | None = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
            with _opener.open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise LocalError(f"{host}/{path}: HTTP {resp.status}")
                return resp.read(200).decode("utf-8", "replace").strip()
        except LocalError:
            raise
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(0.3)
    raise LocalError(f"{host}/{path}: {type(err).__name__}: {err}")


def read_serial(host: str, timeout: float = TIMEOUT, tries: int = 2) -> str:
    raw = _request(host, "esp_serial", timeout=timeout, tries=tries)
    try:
        res = json.loads(raw).get("res")
    except (ValueError, AttributeError) as exc:
        raise LocalError(f"{host}: non e' una Aircare") from exc
    if not res:
        raise LocalError(f"{host}: non e' una Aircare")
    return str(res)


def poll(host: str, serial: str | None, pids) -> dict[str, str]:
    """Legge le proprieta'. Se serial e' dato, prima verifica che la macchina sia quella."""
    with _lock(host):
        if serial is not None:
            found = read_serial(host)
            if found != serial:
                raise WrongDevice(f"{host}: seriale {found}, atteso {serial}")
        out: dict[str, str] = {}
        last: Exception | None = None
        for pid in pids:
            try:
                out[pid] = _request(host, pid)
            except LocalError as exc:
                last = exc
        if not out:
            raise last or LocalError(f"{host}: nessuna lettura")
        return out


def write(host: str, pid: str, value) -> None:
    """Imposta una proprieta'. La VMC risponde ripetendo il valore accettato."""
    with _lock(host):
        got = _request(host, pid, data=str(value).encode())
    try:
        if float(got) != float(value):
            raise LocalError(f"{host}/{pid}: chiesto {value}, risposto {got}")
    except ValueError:
        pass


def local_prefix() -> str | None:
    """Primi tre numeri dell'indirizzo di questa macchina (rete /24)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0].rsplit(".", 1)[0]
    except OSError:
        return None
    finally:
        sock.close()


def scan(prefix: str | None = None) -> dict[str, str]:
    """Cerca le Aircare nella rete di casa. Ritorna {seriale: indirizzo}."""
    prefix = prefix or local_prefix()
    if not prefix:
        return {}

    def probe(i: int):
        host = f"{prefix}.{i}"
        try:
            return read_serial(host, timeout=2.5, tries=1), host
        except LocalError:
            return None

    with ThreadPoolExecutor(max_workers=48) as pool:
        return dict(r for r in pool.map(probe, range(1, 255)) if r)


def scan_described(prefix: str | None = None) -> dict[str, tuple[str, str]]:
    """Come scan(), con una riga leggibile per riconoscere ogni macchina."""
    out: dict[str, tuple[str, str]] = {}
    for serial, host in scan(prefix).items():
        text = host
        try:
            vals = poll(host, None, ("speed", "temp_int"))
            text = f"{host} · velocità {vals.get('speed', '?')} · {vals.get('temp_int', '?')} °C"
        except LocalError:
            pass
        out[serial] = (host, text)
    return out
