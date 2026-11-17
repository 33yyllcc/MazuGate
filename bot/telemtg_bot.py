#!/usr/bin/env python3
"""MazuGate: Telegram bot that manages telemt MTProxy servers.
By Mazu. Programmed by t.me/ozozz

The same file runs in two modes:
  * bot (default): on the main server; Telegram UI, stats, reports, cluster.
  * agent (--agent): on extra servers; a small TLS API used by the main bot.

Stdlib only. Users/secrets/ad tag are edited directly in telemt.toml (as root):
telemt's own installer makes that file root:telemt 0640, so telemt's REST API
cannot write it (EPERM), while its file watcher hot-reloads these keys.
"""
import concurrent.futures
import datetime as dt
import hashlib
import hmac
import html
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None

BRAND = "MazuGate"
CREDIT = "Programmed by t.me/ozozz"
REPO_RAW = "https://raw.githubusercontent.com/33yyllcc/MazuGate/main"
AGENT_VERSION = 2
AGENT_MODE = "--agent" in sys.argv
CONFIG_PATH = os.environ.get(
    "TELEMTG_CONFIG", "/etc/telemtg/agent.json" if AGENT_MODE else "/etc/telemtg/bot.json")

DEFAULTS = {
    "bot_token": "",
    "admins": [],
    "langs": {},
    "default_lang": "ar",
    "api_url": "http://127.0.0.1:9091",
    "api_auth": "",
    "metrics_url": "http://127.0.0.1:9090/metrics",
    "telemt_config": "/etc/telemt/telemt.toml",
    "telemt_service": "telemt",
    "public_ip": "",
    "port": 443,
    "timezone_offset_hours": 3,
    "report_hour": 0,
    "state_file": "/var/lib/telemtg/state.json",
    "sample_seconds": 30,
    "periodic_hours": 6,
    "nic_speed_mbps": 0,
    "link_host": "",
    "nodes": [],
    "cf_token": "", "cf_zone": "",
    # agent-only keys
    "token": "", "master_ips": [], "agent_port": 9443, "cert": "", "key": "",
}

with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = {**DEFAULTS, **json.load(_f)}
CFG["repo_raw"] = os.environ.get("MAZUGATE_REPO_RAW", REPO_RAW)

for _i, _n in enumerate(CFG.get("nodes") or []):  # servers added by older versions
    _n.setdefault("num", _i + 2)

TOKEN = CFG["bot_token"]
CFG_LOCK = threading.Lock()
OWNER = int(CFG.get("owner") or (CFG["admins"][0] if CFG["admins"] else 0))
ADMINS = {int(a) for a in CFG["admins"]} - {OWNER}  # same full permissions as the owner
ADMIN_NAMES = {str(k): v for k, v in (CFG.get("admin_names") or {}).items()}
TZ = dt.timezone(dt.timedelta(hours=float(CFG["timezone_offset_hours"])))
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def now_local():
    return dt.datetime.now(TZ)


def is_admin(uid):
    return uid == OWNER or uid in ADMINS


def save_cfg():
    with CFG_LOCK:
        if not AGENT_MODE:
            CFG["owner"] = OWNER
            CFG["admins"] = sorted(ADMINS)
            CFG["admin_names"] = dict(ADMIN_NAMES)
        data = json.dumps({k: v for k, v in CFG.items() if k != "repo_raw"}, ensure_ascii=False, indent=2)
        tmp = CONFIG_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, CONFIG_PATH)


# ------------------------------------------------------------- language ----
# Every user-facing text is written as L(arabic, english); the language of the
# user being served is kept per thread.

_CTX = threading.local()


def cur_lang():
    return getattr(_CTX, "lang", None) or CFG.get("default_lang") or "ar"


def set_lang(lang):
    _CTX.lang = lang


def L(ar, en):
    return en if cur_lang() == "en" else ar


def E(ar, en):
    """A bilingual value stored now and rendered later with tr()."""
    return {"ar": ar, "en": en}


def tr(x):
    if isinstance(x, dict) and "ar" in x:
        return x["en"] if cur_lang() == "en" else x["ar"]
    return "" if x is None else str(x)


def user_lang(uid):
    return (CFG.get("langs") or {}).get(str(uid))


def in_lang(lang, fn):
    prev = getattr(_CTX, "lang", None)
    set_lang(lang)
    try:
        return fn()
    finally:
        set_lang(prev)


def both(fn):
    """Renders fn() in both languages now (e.g. before counters are reset)."""
    return {"ar": in_lang("ar", fn), "en": in_lang("en", fn)}


def footer():
    return f"\n\n💻 <i>{BRAND} • {CREDIT}</i>"


# ---------------------------------------------------------------- state ----

STATE_LOCK = threading.Lock()
DAILY_KEYS = {"today_down": 0, "today_up": 0, "peak_conns": 0, "peak_ips": 0}


def _load_state():
    try:
        with open(CFG["state_file"], encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


STATE = {
    "day": now_local().strftime("%Y-%m-%d"),
    **DAILY_KEYS,
    "total_down": 0, "total_up": 0,
    "period_down": 0, "period_up": 0, "period_slot": "",
    "users_today": {}, "servers_today": {}, "srv_peaks": {}, "last_users": {},
    "history": [],
    "welcomed": False,
}
STATE.update(_load_state())
for _k in ("last_down", "last_up", "last_report_day", "nic_today_rx", "nic_today_tx",
           "nic_today_drops", "last_nic", "peak_rx", "peak_tx", "peak_cpu", "peak_ram"):
    STATE.pop(_k, None)


def save_state():
    path = CFG["state_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with STATE_LOCK:
        data = json.dumps(STATE, ensure_ascii=False, indent=1)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def update_peaks(src, v):
    with STATE_LOCK:
        p = STATE["srv_peaks"].setdefault(src, {"rx": 0.0, "tx": 0.0, "cpu": 0.0, "ram": 0.0})
        p["rx"] = round(max(p["rx"], v.get("rx", 0.0)), 2)
        p["tx"] = round(max(p["tx"], v.get("tx", 0.0)), 2)
        p["cpu"] = round(max(p["cpu"], v.get("cpu", 0.0)), 1)
        p["ram"] = round(max(p["ram"], v.get("ram_pct", 0.0)), 1)


# --------------------------------------------------------- telemt.toml ----

TOML_LOCK = threading.RLock()
HEADER_RE = re.compile(r"^\s*\[")
TABLE_RE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
KV_RE = re.compile(r'''^\s*("(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+)\s*=\s*(.*)$''')
STR_RE = re.compile(r'''^\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')''')
USER_MAPS = ("user_enabled", "user_ad_tags", "user_max_tcp_conns", "user_expirations",
             "user_data_quota", "user_quota_periods", "user_max_unique_ips",
             "user_source_deny", "user_rate_limits")


class ConfigError(Exception):
    pass


def _unquote(k):
    k = k.strip()
    if len(k) >= 2 and k[0] == k[-1] == '"':
        try:
            return json.loads(k)
        except ValueError:
            return k[1:-1]
    if len(k) >= 2 and k[0] == k[-1] == "'":
        return k[1:-1]
    return k


def _key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def _str_val(raw):
    m = STR_RE.match(raw)
    if not m:
        return None
    if m.group(1) is not None:
        try:
            return json.loads('"' + m.group(1) + '"')
        except ValueError:
            return m.group(1)
    return m.group(2)


def _scalar(raw):
    return raw.split("#", 1)[0].strip()


class TomlDoc:
    """Minimal line-based TOML editor: keeps comments/formatting, edits
    `key = value` lines inside `[table]` sections."""

    def __init__(self, path=None):
        self.path = path or CFG["telemt_config"]
        with open(self.path, encoding="utf-8") as f:
            self.text = f.read()
        self.lines = self.text.splitlines()

    def _range(self, table):
        for i, line in enumerate(self.lines):
            m = TABLE_RE.match(line)
            if m and not line.lstrip().startswith("[["):
                if ".".join(_unquote(p) for p in m.group(1).split(".")) == table:
                    end = next((j for j in range(i + 1, len(self.lines))
                                if HEADER_RE.match(self.lines[j])), len(self.lines))
                    return i, end
        return None, None

    def items(self, table):
        start, end = self._range(table)
        if start is None:
            return []
        out = []
        for i in range(start + 1, end):
            m = KV_RE.match(self.lines[i])
            if m and not self.lines[i].lstrip().startswith("#"):
                out.append((i, _unquote(m.group(1)), m.group(2)))
        return out

    def strings(self, table):
        return {k: v for _, k, raw in self.items(table) if (v := _str_val(raw)) is not None}

    def raw(self, table):
        return {k: _scalar(raw) for _, k, raw in self.items(table)}

    def get_str(self, table, key):
        return self.strings(table).get(key)

    def set(self, table, key, literal):
        line = f"{_key(key)} = {literal}"
        start, end = self._range(table)
        if start is None:
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines += [f"[{table}]", line]
            return True
        for i, k, _ in self.items(table):
            if k == key:
                if self.lines[i].strip() == line:
                    return False
                self.lines[i] = line
                return True
        ins = end
        while ins - 1 > start and not self.lines[ins - 1].strip():
            ins -= 1
        self.lines.insert(ins, line)
        return True

    def delete(self, table, key):
        for i, k, _ in self.items(table):
            if k == key:
                del self.lines[i]
                return True
        return False

    def save(self):
        """Validates and atomically replaces the file, keeping owner and mode."""
        new = "\n".join(self.lines) + "\n"
        if new == self.text:
            return False
        if tomllib:
            try:
                data = tomllib.loads(new)
            except Exception as e:
                raise ConfigError(f"invalid TOML after edit: {e}")
            users = (data.get("access") or {}).get("users") or {}
            if not users:
                raise ConfigError("access.users would be empty")
            for k, v in users.items():
                if not (isinstance(v, str) and HEX32_RE.match(v.lower())):
                    raise ConfigError(f"bad secret for user {k}")
        st = os.stat(self.path)
        tmp = os.path.join(os.path.dirname(self.path) or ".",
                           f".{os.path.basename(self.path)}.mazugate-{os.getpid()}-{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, st.st_mode & 0o7777)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.text = new
        return True


def reload_telemt():
    """telemt's watcher reloads by itself; SIGHUP makes it immediate."""
    try:
        subprocess.run(["systemctl", "kill", "-s", "HUP", CFG["telemt_service"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


def toml_mutate(fn):
    """Runs fn(doc) under the lock; saves and reloads telemt if anything changed."""
    with TOML_LOCK:
        doc = TomlDoc()
        result = fn(doc)
        changed = doc.save()
    if changed:
        reload_telemt()
    return result, changed


def config_users(doc=None):
    """{name: secret} of enabled users, straight from telemt.toml."""
    doc = doc or TomlDoc()
    enabled = doc.raw("access.user_enabled")
    return {k: v.lower() for k, v in doc.strings("access.users").items()
            if enabled.get(k, "true") != "false" and HEX32_RE.match(v.lower())}


def read_ad_tag(doc=None):
    try:
        tag = ((doc or TomlDoc()).get_str("general", "ad_tag") or "").lower()
    except OSError:
        return None
    return tag if HEX32_RE.match(tag) and tag != "0" * 32 else None


def cfg_hash(users, tag):
    h = hashlib.sha256()
    for n in sorted(users):
        h.update(f"{n}={users[n]}\n".encode())
    h.update(f"ad_tag={tag or ''}".encode())
    return h.hexdigest()[:16]


def delete_user_lines(doc, name):
    doc.delete("access.users", name)
    for m in USER_MAPS:
        doc.delete(f"access.{m}", name)


def set_ad_tag(tag):
    def fn(doc):
        if tag:
            doc.set("general", "ad_tag", json.dumps(tag))
        else:
            doc.delete("general", "ad_tag")
    toml_mutate(fn)


def set_public_host(host):
    def fn(doc):
        if host:
            doc.set("general.links", "public_host", json.dumps(host))
        else:
            doc.delete("general.links", "public_host")
    toml_mutate(fn)


def create_user(name):
    secret = secrets.token_hex(16)

    def fn(doc):
        if name in doc.strings("access.users"):
            raise ConfigError("exists")
        doc.set("access.users", name, json.dumps(secret))
        doc.delete("access.user_enabled", name)
    toml_mutate(fn)
    return secret


def delete_user(name):
    def fn(doc):
        users = doc.strings("access.users")
        if name not in users:
            raise ConfigError("missing")
        if len(users) <= 1:
            raise ConfigError("last")
        delete_user_lines(doc, name)
    toml_mutate(fn)


def wait_runtime(names, timeout=15):
    """True once telemt's runtime contains every user in `names`."""
    deadline = time.time() + timeout
    while True:
        try:
            live = {u["username"] for u in get_users() if u.get("in_runtime", True)}
            if set(names) <= live:
                return True
        except ApiError:
            pass
        if time.time() > deadline:
            return False
        time.sleep(1)


# ------------------------------------------------------ telemt API (read) --

class ApiError(Exception):
    pass


def api(method, path, body=None, timeout=10):
    url = CFG["api_url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if CFG["api_auth"]:
        headers["Authorization"] = CFG["api_auth"]
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with LOCAL.open(req, timeout=timeout) as r:
            payload = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            raise ApiError(f"HTTP {e.code}")
    except Exception as e:
        raise ApiError(str(e))
    if not payload.get("ok", False):
        err = payload.get("error") or {}
        raise ApiError(err.get("message") or err.get("code") or "unknown error")
    return payload.get("data")


def api_optional(path):
    try:
        return api("GET", path, timeout=5) or {}
    except ApiError:
        return {}


def get_users():
    return api("GET", "/v1/users") or []


METRIC_LINE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eE+-]+)\s*$')
USER_LABEL_RE = re.compile(r'user="((?:[^"\\]|\\.)*)"')


def metrics_snapshot():
    with LOCAL.open(urllib.request.Request(CFG["metrics_url"]), timeout=10) as r:
        text = r.read().decode("utf-8", "replace")
    out = {}
    for line in text.splitlines():
        m = METRIC_LINE_RE.match(line)
        if m:
            try:
                out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
            except ValueError:
                pass
    return out


def msum(snap, name):
    return sum(v for k, v in snap.items() if k == name or k.startswith(name + "{"))


def get_traffic():
    """Per-user {name: [down_to_clients, up_from_clients]} bytes since telemt start."""
    per = {}
    for k, v in metrics_snapshot().items():
        if k.startswith("telemt_user_octets_to_client_total{") or \
                k.startswith("telemt_user_octets_from_client_total{"):
            lm = USER_LABEL_RE.search(k)
            rec = per.setdefault(lm.group(1) if lm else "?", [0, 0])
            rec[0 if "_to_client_" in k else 1] += int(v)
    return per


# --------------------------------------------------------------- system ----

def _cpu_fields():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    return (vals + [0] * 8)[:8]  # user nice system idle iowait irq softirq steal


def _meminfo():
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    return mem


def _default_iface():
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if p[1] == "00000000":
                    return p[0]
    except Exception:
        pass
    return None


def _iface_bytes(iface):
    with open("/proc/net/dev") as f:
        for line in f:
            if ":" in line:
                name, rest = line.split(":", 1)
                if name.strip() == iface:
                    p = rest.split()
                    return int(p[0]), int(p[8])
    return None


def nic_speed(iface):
    """Port speed in Mbit/s and whether it is a guess (VPS NICs often hide it)."""
    manual = int(CFG.get("nic_speed_mbps") or 0)
    if manual > 0:
        return manual, False
    try:
        with open(f"/sys/class/net/{iface}/speed") as f:
            v = int(f.read().strip())
        if v > 0:
            return v, False
    except Exception:
        pass
    return 1000, True


SYS_CACHE = {"t": 0.0, "val": None}


def _measure(window):
    iface = _default_iface()
    c1, n1 = _cpu_fields(), (_iface_bytes(iface) if iface else None)
    t0 = time.time()
    time.sleep(window)
    c2, n2 = _cpu_fields(), (_iface_bytes(iface) if iface else None)
    el = max(time.time() - t0, 0.001)
    d = [b - a for a, b in zip(c1, c2)]
    tot = max(sum(d), 1)
    mem = _meminfo()
    total = mem.get("MemTotal", 1)
    used = total - mem.get("MemAvailable", mem.get("MemFree", 0))
    r = {"cpu": max(0.0, 100.0 * (tot - d[3] - d[4]) / tot),
         "ram_used": used, "ram_pct": 100.0 * used / total,
         "iface": iface or "?", "rx": 0.0, "tx": 0.0}
    if n1 and n2:
        r["rx"] = max(0, n2[0] - n1[0]) * 8 / el / 1e6
        r["tx"] = max(0, n2[1] - n1[1]) * 8 / el / 1e6
    r["speed"], r["speed_guess"] = nic_speed(iface) if iface else (1000, True)
    r["nic_pct"] = 100.0 * max(r["rx"], r["tx"]) / max(r["speed"], 1)
    return r


def sys_monitor():
    """Background sampler: stats answer instantly and daily peaks are tracked."""
    while True:
        try:
            v = _measure(2.0)
            SYS_CACHE["val"], SYS_CACHE["t"] = v, time.time()
            if not AGENT_MODE:
                update_peaks("local", v)
        except Exception:
            traceback.print_exc()
            time.sleep(5)


def system_usage():
    if SYS_CACHE["val"] is None or time.time() - SYS_CACHE["t"] > 10:
        return _measure(0.5)
    return SYS_CACHE["val"]


_TELEMT_PID = [None]


def telemt_pid():
    pid = _TELEMT_PID[0]
    if pid and os.path.exists(f"/proc/{pid}"):
        return pid
    _TELEMT_PID[0] = None
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/comm") as f:
                    if f.read().strip() == "telemt":
                        _TELEMT_PID[0] = int(d)
                        break
            except OSError:
                pass
    return _TELEMT_PID[0]


def telemt_fds():
    pid = telemt_pid()
    if not pid:
        return None, None
    try:
        used = len(os.listdir(f"/proc/{pid}/fd"))
        with open(f"/proc/{pid}/limits") as f:
            for line in f:
                if line.startswith("Max open files"):
                    return used, int(line.split()[3])
    except (OSError, ValueError, IndexError):
        pass
    return None, None


def _sysctl(name):
    try:
        with open("/proc/sys/" + name.replace(".", "/")) as f:
            return f.read().strip()
    except OSError:
        return None


TUNING_NAMES = {
    "somaxconn": E("طابور قبول الاتصالات (somaxconn)", "accept queue (somaxconn)"),
    "syn_backlog": E("طابور SYN", "SYN backlog"),
    "nr_open": E("حد الملفات بالنظام", "system file limit"),
    "bbr": E("BBR", "BBR"),
    "max_connections": E("حد الاتصالات بـ telemt (كان 10000)", "telemt connection cap (was 10000)"),
    "listen_backlog": E("طابور telemt", "telemt listen backlog"),
    "pressure_mode": E("وضع الضغط مفعّل (يگطع الاتصالات)", "pressure mode on (drops connections)"),
    "nofile": E("حد الملفات المفتوحة لـ telemt", "telemt open-file limit"),
}


def tuning_checks():
    """Which high-load settings are missing on this server."""
    missing = []

    def num(v):
        try:
            return int(str(v).split()[0])
        except (TypeError, ValueError, IndexError):
            return 0

    if num(_sysctl("net.core.somaxconn")) < 65535:
        missing.append("somaxconn")
    if num(_sysctl("net.ipv4.tcp_max_syn_backlog")) < 65535:
        missing.append("syn_backlog")
    if num(_sysctl("fs.nr_open")) < 1048576:
        missing.append("nr_open")
    if "bbr" in (_sysctl("net.ipv4.tcp_available_congestion_control") or "").split() \
            and _sysctl("net.ipv4.tcp_congestion_control") != "bbr":
        missing.append("bbr")
    try:
        doc = TomlDoc()
        server = doc.raw("server")
        if server.get("max_connections") != "0":
            missing.append("max_connections")
        if num(server.get("listen_backlog")) < 65535:
            missing.append("listen_backlog")
        if doc.raw("server.conntrack_control").get("inline_conntrack_control") != "false":
            missing.append("pressure_mode")
    except OSError:
        pass
    _, limit = telemt_fds()
    if limit is not None and limit < 1048576:
        missing.append("nofile")
    return {"ok": not missing, "missing": missing}


def tuning_names(missing):
    return L("، ", ", ").join(tr(TUNING_NAMES.get(x, x)) for x in missing)


# -------------------------------------------------------------- helpers ----

def gb(n):
    n = float(n or 0)
    if n >= 1024 ** 4:
        return f"{n / 1024 ** 4:.2f} TB"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def fmt_rate(mbps):
    mbps = float(mbps or 0)
    if mbps >= 1000:
        return f"{mbps / 1000:.2f} Gbps"
    if mbps >= 1:
        return f"{mbps:.0f} Mbps"
    return f"{mbps * 1000:.0f} Kbps"


def fmt_num(n):
    return f"{int(n or 0):,}"


def bar(pct, n=10):
    f = int(round(min(max(pct or 0, 0), 100) / 100 * n))
    return "▰" * f + "▱" * (n - f)


def esc(s):
    return html.escape(str(s), quote=False)


def tme(link):
    return link.replace("tg://proxy?", "https://t.me/proxy?", 1)


def with_link_host(link):
    """Replaces server=<ip> with the owner's domain (if one is set)."""
    host = (CFG.get("link_host") or "").strip()
    if not host or "?" not in link:
        return link
    base, q = link.split("?", 1)
    params = [(k, host if k == "server" else v) for k, v in urllib.parse.parse_qsl(q, keep_blank_values=True)]
    return base + "?" + urllib.parse.urlencode(params)


def api_links(u):
    links = u.get("links") or {}
    out = []
    for kind in ("tls", "secure", "classic"):
        out += links.get(kind) or []
    out += [i["link"] for i in links.get("tls_domains") or [] if i.get("link")]
    seen, uniq = set(), []
    for link in out:
        link = tme(with_link_host(link))
        if link not in seen:
            seen.add(link)
            uniq.append(link)
    return uniq


def build_link(secret, doc=None):
    """Link built from telemt.toml (used if telemt's API has no link yet)."""
    doc = doc or TomlDoc()
    modes = doc.raw("general.modes")
    domain = doc.get_str("censorship", "tls_domain") or ""
    if modes.get("tls", "true" if domain else "false") == "true" and domain:
        sec = "ee" + secret + domain.encode().hex()
    elif modes.get("secure") == "true":
        sec = "dd" + secret
    else:
        sec = secret
    host = CFG.get("link_host") or CFG["public_ip"]
    return f"https://t.me/proxy?server={host}&port={CFG['port']}&secret={sec}"


# ------------------------------------------------------------- telegram ----

TG_HOST = "api.telegram.org"
TG_PORT = 443
_SSL = ssl.create_default_context()
_TLS = threading.local()  # one keep-alive connection per thread


class TGConnection(http.client.HTTPSConnection):
    """HTTPS connection that tries IPv4 first: a broken IPv6 route on the VPS
    otherwise stalls every request until timeout."""

    def connect(self):
        sock, last = None, None
        for family in (socket.AF_INET, socket.AF_UNSPEC):
            try:
                infos = socket.getaddrinfo(self.host, self.port, family, socket.SOCK_STREAM)
            except OSError as e:
                last = e
                continue
            for af, st, proto, _, sa in infos:
                s = socket.socket(af, st, proto)
                s.settimeout(8)
                try:
                    s.connect(sa)
                    sock = s
                    break
                except OSError as e:
                    last = e
                    s.close()
            if sock:
                break
        if sock is None:
            raise last or OSError("connect failed")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout)
        self.sock = _SSL.wrap_socket(sock, server_hostname=self.host)


def tg(method, **params):
    body = json.dumps(params).encode()
    timeout = params.get("timeout", 0) + 25
    path = f"/bot{TOKEN}/{method}"
    for attempt in range(3):
        conn = getattr(_TLS, "conn", None)
        if conn is None:
            conn = _TLS.conn = TGConnection(TG_HOST, TG_PORT, timeout=timeout)
        conn.timeout = timeout
        if conn.sock:
            conn.sock.settimeout(timeout)
        try:
            conn.request("POST", path, body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            _TLS.conn = None
            if attempt == 2:
                raise RuntimeError(f"{method}: {e}")
            continue
        try:
            res = json.loads(raw)
        except ValueError:
            raise RuntimeError(f"{method}: HTTP {resp.status}")
        if res.get("ok"):
            return res.get("result")
        retry = (res.get("parameters") or {}).get("retry_after")
        if retry and attempt < 2:  # 429 flood control
            time.sleep(min(int(retry), 30))
            continue
        raise RuntimeError(f"{method}: {res.get('description')}")


def send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    try:
        return tg("sendMessage", **p)
    except Exception as e:
        log("send failed:", e)


def edit(chat_id, msg_id, text, kb=None):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    try:
        tg("editMessageText", **p)
    except Exception as e:
        if "not modified" not in str(e):
            send(chat_id, text, kb)


def show(chat_id, msg_id, text, kb=None):
    if msg_id:
        edit(chat_id, msg_id, text, kb)
    else:
        send(chat_id, text, kb)


def broadcast(msg, kb=None, owner_only=False):
    """msg / kb may be a string, an E() dict, or a callable rendered in each
    recipient's own language."""
    for a in ([OWNER] if owner_only else [OWNER] + sorted(ADMINS)):
        def render(x):
            return x() if callable(x) else tr(x) if isinstance(x, dict) else x
        text, keyboard = in_lang(user_lang(a) or CFG.get("default_lang") or "ar",
                                 lambda: (render(msg), render(kb)))
        send(a, text, keyboard)


def btn(text, data):
    return {"text": text, "callback_data": data}


def main_kb():
    return [
        [btn(L("📊 الإحصائيات", "📊 Stats"), "stats"), btn(L("🔗 الروابط", "🔗 Links"), "links")],
        [btn(L("🖧 السيرفرات", "🖧 Servers"), "servers"), btn(L("🩺 تشخيص", "🩺 Diagnostics"), "diag")],
        [btn(L("📢 القناة الإعلانية", "📢 Sponsored channel"), "ad"), btn(L("📅 آخر 7 أيام", "📅 Last 7 days"), "history")],
        [btn(L("➕ رابط جديد", "➕ New link"), "new"), btn(L("🗑 حذف رابط", "🗑 Delete link"), "del")],
        [btn(L("👮 الأدمنية", "👮 Admins"), "admins"), btn(L("⚙️ الإعدادات", "⚙️ Settings"), "settings")],
        [btn("🌐 Language / اللغة", "lang")],
    ]


def back_kb():
    return [[btn(L("🔙 القائمة", "🔙 Menu"), "menu")]]


def cancel_kb():
    return [[btn(L("إلغاء", "Cancel"), "cancel")]]


LANG_KB = [[btn("🇮🇶 العربية", "lang:ar"), btn("🇬🇧 English", "lang:en")]]
LANG_TEXT = (f"👋 <b>{BRAND}</b>\n\n🌐 اختار اللغة\n🌐 Choose your language"
             f"\n\n💻 <i>{CREDIT}</i>")


# ------------------------------------------------- domain + cloudflare ----

CF_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22")]
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def resolve_a(domain):
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(domain, None, socket.AF_INET)})
    except OSError:
        return []


def check_link_domain(domain):
    """None if the domain points straight at this server, else an error text."""
    if not DOMAIN_RE.match(domain):
        return L("❌ هذا مو دومين صحيح. مثال: <code>proxy.example.com</code>",
                 "❌ That is not a valid domain. Example: <code>proxy.example.com</code>")
    ips = resolve_a(domain)
    ip = esc(CFG['public_ip'])
    if not ips:
        return L(f"❌ الدومين ما عنده سجل A بعد.\nسوي سجل <b>A</b> يأشر على <code>{ip}</code> وانتظر دقيقة وجرب.",
                 f"❌ The domain has no A record yet.\nCreate an <b>A</b> record pointing to <code>{ip}</code>, wait a minute and retry.")
    if any(any(ipaddress.ip_address(x) in n for n in CF_NETS) for x in ips):
        return L("❌ الدومين مفعّل عليه بروكسي كلاودفلير (الغيمة البرتقالية 🟠).\n"
                 "كلاودفلير المجاني يمرر بس مواقع (HTTP)، وبروكسي تلكرام ما يشتغل من خلاله.\n\n"
                 "روح لكلاودفلير ← DNS ← خلي الغيمة <b>رمادية ☁️ (DNS only)</b>، انتظر دقيقتين وارسل الدومين مرة ثانية.",
                 "❌ Cloudflare proxy is enabled on this domain (orange cloud 🟠).\n"
                 "Free Cloudflare only passes websites (HTTP); a Telegram proxy cannot work through it.\n\n"
                 "In Cloudflare → DNS, set the cloud to <b>grey ☁️ (DNS only)</b>, wait two minutes and send the domain again.")
    if CFG["public_ip"] not in ips:
        return L(f"❌ الدومين يأشر على <code>{esc(', '.join(ips))}</code> بس IP السيرفر <code>{ip}</code>.\n"
                 "صلّح سجل A وانتظر دقيقتين وجرب مرة ثانية.",
                 f"❌ The domain points to <code>{esc(', '.join(ips))}</code> but this server is <code>{ip}</code>.\n"
                 "Fix the A record, wait two minutes and retry.")
    return None


def cf_enabled():
    return bool(CFG.get("cf_token") and CFG.get("link_host"))


def cf(method, path, body=None):
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + CFG["cf_token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read())
        except Exception:
            raise RuntimeError(f"Cloudflare HTTP {e.code}")
    if not res.get("success"):
        errs = res.get("errors") or [{}]
        raise RuntimeError("Cloudflare: " + str(errs[0].get("message", "error")))
    return res.get("result")


def cf_zone_id(domain):
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        r = cf("GET", "/zones?name=" + urllib.parse.quote(".".join(parts[i:])))
        if r:
            return r[0]["id"]
    raise RuntimeError("domain not found in this Cloudflare account (or the token has no access)")


def dns_wanted_ips():
    want = {CFG["public_ip"]}
    for n in joined_nodes():
        if (NODE_CACHE.get(n["ip"]) or {}).get("ready"):
            want.add(n["ip"])
    return want


def dns_reconcile():
    """Keeps exactly one grey A record per ready server. Returns (added, removed)."""
    if not cf_enabled():
        return None
    domain = CFG["link_host"]
    zone = CFG.get("cf_zone") or cf_zone_id(domain)
    if CFG.get("cf_zone") != zone:
        CFG["cf_zone"] = zone
        save_cfg()
    recs = cf("GET", f"/zones/{zone}/dns_records?type=A&per_page=100&name={urllib.parse.quote(domain)}") or []
    want = dns_wanted_ips()
    have = {}
    for r in recs:
        have.setdefault(r["content"], []).append(r)
    added, removed = [], []
    for ip in sorted(want - set(have)):
        cf("POST", f"/zones/{zone}/dns_records",
           {"type": "A", "name": domain, "content": ip, "ttl": 60, "proxied": False})
        added.append(ip)
    for ip, rs in have.items():
        keep = rs[0] if ip in want else None
        for r in rs:
            if r is keep:
                if r.get("proxied") or r.get("ttl", 60) > 120:
                    cf("PATCH", f"/zones/{zone}/dns_records/{r['id']}", {"proxied": False, "ttl": 60})
                continue
            cf("DELETE", f"/zones/{zone}/dns_records/{r['id']}")
            removed.append(ip)
    return added, removed


_DNS_ERR = {"t": 0}


def dns_reconcile_safe():
    try:
        return dns_reconcile()
    except Exception as e:
        log("dns:", e)
        if time.time() - _DNS_ERR["t"] > 3600:
            _DNS_ERR["t"] = time.time()
            broadcast(lambda err=e: L("⚠️ ما كدرت أحدث سجلات الدومين بكلاودفلير:", "⚠️ Could not update the Cloudflare DNS records:")
                      + f"\n<code>{esc(err)}</code>")


# ------------------------------------------------------ cluster (main) ----
# Every server runs the same secrets + ad tag; the link domain has one A record
# per *ready* server (reachable, healthy, and verified in sync).

NODE_CACHE = {}  # ip -> health/sync state
IPV4_RE = re.compile(r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$")
_SYNC_LOCK = threading.Lock()
_LAST_DNS = {"t": 0}


class NodeError(Exception):
    pass


def nodes():
    return list(CFG.get("nodes") or [])


def joined_nodes():
    return [n for n in nodes() if n.get("joined")]


def find_node(ip):
    return next((n for n in CFG.get("nodes") or [] if n["ip"] == ip), None)


def node_name(src):
    if src == "local":
        return L("السيرفر 1", "Server 1")
    n = find_node(src)
    num = (n or {}).get("num")
    if num:
        return L(f"السيرفر {num}", f"Server {num}")
    return (n or {}).get("name") or src


def node_cache(ip):
    return NODE_CACHE.setdefault(ip, {"down": False, "fails": 0, "succ": 0, "data": None, "t": 0,
                                      "err": "", "in_sync": False, "ready": False, "unsynced": 0,
                                      "sync_err": "", "last_sync_try": 0})


def node_fresh(ip):
    c = NODE_CACHE.get(ip) or {}
    return c.get("data") if c.get("data") and time.time() - c.get("t", 0) < 120 else None


def node_call(node, method, path, body=None, timeout=10):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed cert, pinned by fingerprint below
    conn = http.client.HTTPSConnection(node["ip"], int(node.get("agent_port") or 9443),
                                       timeout=timeout, context=ctx)
    try:
        conn.connect()
        fp = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        if node.get("fp") and not hmac.compare_digest(fp, node["fp"]):
            raise NodeError(E("بصمة شهادة السيرفر تغيرت! إذا عدت تنصيبه احذفه وضيفه من جديد",
                              "The server certificate changed! If you reinstalled it, remove and add it again"))
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, data, {"Authorization": "Bearer " + node["token"],
                                          "Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
    except NodeError:
        raise
    except (OSError, http.client.HTTPException) as e:
        raise NodeError(str(e) or e.__class__.__name__)
    finally:
        conn.close()
    if resp.status == 404:
        raise NodeError(OLD_VERSION)
    if resp.status != 200:
        try:
            msg = json.loads(raw).get("error") or raw[:200].decode("utf-8", "replace")
        except ValueError:
            msg = raw[:200].decode("utf-8", "replace")
        raise NodeError(f"HTTP {resp.status}: {msg}")
    if not node.get("fp"):  # trust on first use; the owner just installed it
        node["fp"] = fp
        save_cfg()
    return json.loads(raw)


def err_value(e):
    """Keeps bilingual NodeError payloads, stringifies anything else."""
    if isinstance(e, NodeError) and e.args and isinstance(e.args[0], dict):
        return e.args[0]
    return str(e)


OLD_VERSION = E("نسخة السيرفر قديمة، حدّثه بـ update.sh", "Server runs an old version, update it with update.sh")


def tcp_check(ip, port):
    try:
        socket.create_connection((ip, int(port)), 4).close()
        return True
    except OSError:
        return False


def desired_state():
    doc = TomlDoc()
    users = config_users(doc)
    tag = read_ad_tag(doc)
    return {"users": users, "ad_tag": tag, "prune": bool(users),
            "nic_speed_mbps": int(CFG.get("nic_speed_mbps") or 0), "hash": cfg_hash(users, tag)}


def sync_node(node, state=None):
    state = state or desired_state()
    res = node_call(node, "POST", "/sync", state, timeout=40)
    c = node_cache(node["ip"])
    c["in_sync"] = res.get("cfg_hash") == state["hash"] and res.get("applied", False)
    c["sync_err"] = "" if c["in_sync"] else E("البروكسي بعده ما طبق الإعدادات", "the proxy has not applied the settings yet")
    return res


def sync_all():
    """Pushes links + ad tag to every joined node. Returns {ip: result|error}."""
    out = {}
    with _SYNC_LOCK:
        try:
            state = desired_state()
        except Exception as e:
            return {"local": f"telemt.toml: {e}"}
        for n in joined_nodes():
            try:
                out[n["ip"]] = sync_node(n, state)
            except Exception as e:
                node_cache(n["ip"]).update(in_sync=False, sync_err=err_value(e))
                out[n["ip"]] = err_value(e)
    return out


def trigger_sync():
    if joined_nodes():
        threading.Thread(target=sync_all, daemon=True).start()


def update_cmd():
    return f"curl -fsSL {CFG['repo_raw']}/update.sh -o update.sh && sudo bash update.sh"


def _dns_note(ip, joining):
    domain = esc(CFG.get("link_host") or L("الدومين", "the domain"))
    if cf_enabled():
        return L("☁️ ضفته للدومين تلقائياً.", "☁️ Added to the domain automatically.") if joining else \
            L("☁️ شلته من الدومين تلقائياً لحد ما يرجع.", "☁️ Removed from the domain until it recovers.")
    if joining:
        return L(f"⚠️ ضيف سجل A لـ <code>{domain}</code> يأشر على <code>{ip}</code> (غيمة رمادية)، أو اربط كلاودفلير من 🖧 السيرفرات.",
                 f"⚠️ Add an A record for <code>{domain}</code> pointing to <code>{ip}</code> (grey cloud), or link Cloudflare in 🖧 Servers.")
    return L(f"⚠️ شيل سجل <code>{ip}</code> من <code>{domain}</code> لحد ما يرجع، حتى ما يفشل اتصال قسم من المشتركين.",
             f"⚠️ Remove the <code>{ip}</code> record from <code>{domain}</code> until it recovers, so users are not sent to it.")


def poll_nodes():
    """Health + sync check of every node; keeps DNS to ready nodes only."""
    try:
        state = desired_state()
    except Exception as e:
        log("desired_state:", e)
        state = None
    dns_changed = False
    for node in nodes():
        ip = node["ip"]
        c = node_cache(ip)
        data, ok = None, False
        try:
            data = node_call(node, "GET", "/status", timeout=8)
            if int(data.get("version") or 1) < AGENT_VERSION:
                c["err"] = OLD_VERSION
            elif not data.get("telemt_ok"):
                c["err"] = E("البروكسي (telemt) ما يشتغل عليه", "telemt is not running on it")
            elif not tcp_check(ip, CFG["port"]):
                c["err"] = E(f"البورت {CFG['port']} مسكر من برا", f"port {CFG['port']} is closed from outside")
            else:
                ok, c["err"] = True, ""
        except Exception as e:
            c["err"] = err_value(e)
        if data is not None:
            c["data"], c["t"] = data, time.time()
            update_peaks(ip, data.get("sys") or {})
        c["succ"], c["fails"] = (c["succ"] + 1, 0) if ok else (0, c["fails"] + 1)

        if data is not None and not node.get("joined"):
            node["joined"] = True
            node["joined_at"] = int(time.time())
            save_cfg()
            broadcast(lambda: L(f"🔗 <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) اتصل بالنظام، دا أنقله الروابط والتاك...",
                                f"🔗 <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) connected, copying links and tag..."))
        if not node.get("joined"):
            continue

        # sync check: same users/secrets/tag as here, and loaded by telemt
        if data is not None and state and int(data.get("version") or 1) >= AGENT_VERSION:
            c["in_sync"] = data.get("cfg_hash") == state["hash"] and bool(data.get("runtime_ok"))
            if not c["in_sync"] and time.time() - c["last_sync_try"] > 45:
                c["last_sync_try"] = time.time()
                try:
                    with _SYNC_LOCK:
                        sync_node(node, state)
                except Exception as e:
                    c["in_sync"], c["sync_err"] = False, err_value(e)
        elif data is not None:
            c["in_sync"], c["sync_err"] = False, c["err"]

        if c["down"] and c["succ"] >= 2:
            c["down"] = False
        elif not c["down"] and c["fails"] >= 3:
            c["down"] = True

        ready_now = not c["down"] and ok and c["in_sync"]
        why = c["err"] or c["sync_err"] or E("غير معروف", "unknown")
        if not ready_now and not c["ready"] and not c.get("stuck_alerted") \
                and time.time() - node.get("joined_at", 0) > 180:
            c["stuck_alerted"] = True  # joined but never became ready: say why, once
            broadcast(lambda: L(f"⚠️ <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) مو جاهز وما داخل بالتوزيع:",
                                f"⚠️ <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) is not ready and gets no users:")
                      + f"\n<code>{esc(tr(why))}</code>\n"
                      + ("" if cf_enabled() else _dns_note(ip, False) + "\n")
                      + L("حدّثه بهذا الأمر عليه:", "Update it with this command on that server:")
                      + f"\n<code>{esc(update_cmd())}</code>")
        if ready_now == c["ready"]:
            c["unsynced"] = 0
            continue
        if not ready_now and c["ready"] and not c["down"] and c["unsynced"] < 2:
            c["unsynced"] += 1  # short grace while a fresh change is being synced
            continue
        was_ready_before = c["ready"]
        c["ready"], c["unsynced"] = ready_now, 0
        dns_changed = True
        if ready_now:
            c["stuck_alerted"] = False
            if not node.get("announced"):  # first time ever (persisted across restarts)
                node["announced"] = True
                save_cfg()
                broadcast(lambda: L(f"✅ <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) جاهز ويستقبل مشتركين.",
                                    f"✅ <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) is ready and serving users.")
                          + "\n" + _dns_note(ip, True))
            elif c.get("was_bad"):
                c["was_bad"] = False
                broadcast(lambda: L(f"✅ <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) رجع يشتغل.",
                                    f"✅ <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) is back.")
                          + "\n" + _dns_note(ip, True))
        elif was_ready_before:
            c["was_bad"] = True
            down = c["down"] or not ok
            broadcast(lambda: (L(f"🚨 <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) واگف!",
                                 f"🚨 <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) is down!") if down else
                               L(f"⚠️ <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) مو متزامن ويا السيرفر الرئيسي!",
                                 f"⚠️ <b>{esc(node_name(ip))}</b> (<code>{ip}</code>) is out of sync with the main server!"))
                      + f"\n<code>{esc(tr(why))}</code>\n" + _dns_note(ip, False))
    # reconcile on every change, right after a restart, and every 5 minutes
    if cf_enabled() and (dns_changed or time.time() - _LAST_DNS["t"] > 300):
        _LAST_DNS["t"] = time.time()
        dns_reconcile_safe()


# ---------------------------------------------------------- diagnostics ----

def _kernel_counters():
    c = {}
    for path in ("/proc/net/netstat", "/proc/net/snmp"):
        try:
            with open(path) as f:
                rows = f.read().splitlines()
            for i in range(0, len(rows) - 1, 2):
                keys, vals = rows[i].split(), rows[i + 1].split()
                if keys and keys[0] == vals[0]:
                    for k, v in zip(keys[1:], vals[1:]):
                        try:
                            c[keys[0] + k] = int(v)
                        except ValueError:
                            pass
        except OSError:
            pass
    return c


def diag_collect(window=4.0):
    """Measures this server for `window` seconds (plain data, JSON-safe)."""
    m1, s1, k1, t1 = metrics_snapshot(), api_optional("/v1/stats/summary"), _kernel_counters(), time.time()
    time.sleep(window)
    m2, s2, k2 = metrics_snapshot(), api_optional("/v1/stats/summary"), _kernel_counters()
    el = max(time.time() - t1, 0.1)

    def rate(name):
        return max(0.0, msum(m2, name) - msum(m1, name)) / el

    try:
        conns = sum(int(u.get("current_connections") or 0) for u in get_users())
    except ApiError:
        conns = None
    out_s = k2.get("TcpOutSegs", 0) - k1.get("TcpOutSegs", 0)
    ret = k2.get("TcpRetransSegs", 0) - k1.get("TcpRetransSegs", 0)
    fd_used, fd_limit = telemt_fds()
    classes = sorted(s2.get("connections_bad_by_class") or [], key=lambda c: -(c.get("total") or 0))[:3]
    return {
        "window": round(el, 1), "conns": conns,
        "new_rate": max(0, (s2.get("connections_total") or 0) - (s1.get("connections_total") or 0)) / el,
        "idle_close_rate": rate("telemt_relay_idle_hard_close_total"),
        "me_full_rate": rate('telemt_me_c2me_enqueue_events_total{event="full"}'),
        "me_timeout_rate": rate('telemt_me_c2me_enqueue_events_total{event="timeout"}'),
        "accept_timeout_rate": rate("telemt_accept_permit_timeout_total"),
        "pressure": m2.get('telemt_conntrack_control_state{flag="pressure_active"}'),
        "listen_ovf_rate": max(0, k2.get("TcpExtListenOverflows", 0) - k1.get("TcpExtListenOverflows", 0)) / el,
        "retrans_pct": (100.0 * ret / out_s) if out_s > 1000 else None,
        "fd_used": fd_used, "fd_limit": fd_limit,
        "gates": api_optional("/v1/runtime/gates"),
        "tuning": tuning_checks(),
        "bad_classes": [{"class": c.get("class"), "total": c.get("total")} for c in classes],
        "version": api_optional("/v1/system/info").get("version"),
    }


def diag_render(title, d):
    probs, oks = [], []
    fix = "<code>sudo bash update.sh</code>"
    if d.get("pressure"):
        probs.append(L(f"🔴 <b>وضع الضغط شغال</b>: البروكسي يگطع الاتصالات الخاملة بعد 30-90 ثانية. الحل: {fix}",
                       f"🔴 <b>Pressure mode is on</b>: idle connections are cut after 30-90s. Fix: {fix}"))
    if d.get("me_timeout_rate", 0) > 0:
        probs.append(L(f"🔴 <b>رسائل دا تنرمي</b>: الطريق لسيرفرات تلكرام ممتلي ({d['me_timeout_rate']:.1f}/ث)",
                       f"🔴 <b>Messages are dropped</b>: the path to Telegram is full ({d['me_timeout_rate']:.1f}/s)"))
    elif d.get("me_full_rate", 0) > 0:
        probs.append(L(f"⚠️ طوابير سيرفرات تلكرام دا تمتلي ({d['me_full_rate']:.1f}/ث)",
                       f"⚠️ Queues to Telegram are filling up ({d['me_full_rate']:.1f}/s)"))
    else:
        oks.append(L("طريق تلكرام سالك", "Telegram path clear"))
    if d.get("accept_timeout_rate", 0) > 0:
        probs.append(L(f"🔴 <b>اتصالات جديدة تنرفض</b> ({d['accept_timeout_rate']:.1f}/ث)",
                       f"🔴 <b>New connections are refused</b> ({d['accept_timeout_rate']:.1f}/s)"))
    if d.get("listen_ovf_rate", 0) > 0:
        probs.append(L(f"🔴 <b>طابور القبول ممتلي</b> ({d['listen_ovf_rate']:.0f}/ث) — الحل: {fix}",
                       f"🔴 <b>Accept queue overflowing</b> ({d['listen_ovf_rate']:.0f}/s) — fix: {fix}"))
    else:
        oks.append(L("طابور القبول سليم", "accept queue OK"))
    g = d.get("gates") or {}
    if g.get("accepting_new_connections") is False:
        probs.append(L("🔴 <b>البروكسي مسكر استقبال اتصالات جديدة</b>", "🔴 <b>The proxy is not accepting new connections</b>"))
    if g.get("use_middle_proxy") and g.get("me_runtime_ready") is False:
        probs.append(L("⚠️ سيرفرات تلكرام الوسيطة (مال الإعلان) مو جاهزة", "⚠️ Telegram middle proxies (sponsored channel) not ready"))
    if g.get("reroute_active"):
        probs.append(L("⚠️ اتصالات جديدة تروح مباشر بدون إعلان", "⚠️ New connections go direct, without the sponsored channel"))
    rp = d.get("retrans_pct")
    if rp is not None:
        if rp >= 5:
            probs.append(L(f"🔴 <b>ضياع بالشبكة {rp:.1f}%</b>: الطريق بين السيرفر والمشتركين سيء (من المزود أو شبكات الانترنت)",
                           f"🔴 <b>{rp:.1f}% packet loss</b>: the path between server and users is bad (provider or ISPs)"))
        elif rp >= 2:
            probs.append(L(f"⚠️ ضياع بالشبكة {rp:.1f}%", f"⚠️ {rp:.1f}% packet loss"))
        else:
            oks.append(L(f"ضياع الشبكة {rp:.1f}%", f"packet loss {rp:.1f}%"))
    if d.get("fd_used") and d.get("fd_limit"):
        pct = 100.0 * d["fd_used"] / d["fd_limit"]
        if pct > 70:
            probs.append(L(f"🔴 الملفات المفتوحة {pct:.0f}% من الحد", f"🔴 Open files at {pct:.0f}% of the limit"))
    t = d.get("tuning") or {}
    if t.get("missing"):
        probs.append(L("⚠️ <b>التهيئة ناقصة</b>: ", "⚠️ <b>Tuning incomplete</b>: ") + tuning_names(t["missing"])
                     + "\n   " + L("الحل:", "Fix:") + f" {fix}")
    elif t:
        oks.append(L("التهيئة للضغط العالي كاملة", "high-load tuning complete"))
    if d.get("pressure") is not None and not d.get("pressure"):
        oks.append(L("وضع الضغط مطفي", "pressure mode off"))

    out = [f"🖥 <b>{esc(title)}</b>" + (f" — telemt {esc(d['version'])}" if d.get("version") else "")]
    out += probs or [L("✅ ماكو مشاكل", "✅ No problems")]
    if oks:
        out.append("✅ " + " • ".join(oks))
    info = []
    if d.get("conns") is not None:
        info.append(L(f"{fmt_num(d['conns'])} متصل", f"{fmt_num(d['conns'])} connected"))
    info.append(L(f"{d.get('new_rate', 0):.0f} اتصال جديد/ث", f"{d.get('new_rate', 0):.0f} new conn/s"))
    if d.get("idle_close_rate", 0) >= 0.5:
        info.append(L(f"{d['idle_close_rate']:.0f}/ث يتسكر لأنه خامل 6 دقايق",
                      f"{d['idle_close_rate']:.0f}/s closed after 6 min idle"))
    out.append("ℹ️ " + " • ".join(info))
    if d.get("bad_classes"):
        out.append(L("ℹ️ أسباب الفشل: ", "ℹ️ Failure reasons: ")
                   + ", ".join(f"{esc(c['class'])} ({fmt_num(c['total'])})" for c in d["bad_classes"]))
    return out


def diag_collect_all():
    jobs = {"local": lambda: diag_collect(4.0)}
    for n in joined_nodes():
        jobs[n["ip"]] = (lambda node: lambda: node_call(node, "GET", "/diag", timeout=25))(n)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(f): k for k, f in jobs.items()}
        for fut in concurrent.futures.as_completed(futs):
            try:
                results[futs[fut]] = fut.result()
            except Exception as e:
                results[futs[fut]] = err_value(e)
    return [(k, results.get(k)) for k in jobs]


def diag_text(results):
    out = [L("🩺 <b>تشخيص</b>", "🩺 <b>Diagnostics</b>") + f"  •  {now_local():%Y-%m-%d %H:%M}", ""]
    for k, r in results:
        title = node_name(k) + (L(" (الرئيسي)", " (main)") if k == "local" else f" — {k}")
        if isinstance(r, dict) and "window" in r:
            out += diag_render(title, r)
        else:
            out.append(f"🖥 <b>{esc(title)}</b>\n🔴 " + L("ما كدرت أفحصه:", "Could not check it:")
                       + f" <code>{esc(tr(r))}</code>")
        out.append("")
    out.append(L("ℹ️ الاتصالات الجديدة الكثيرة طبيعية: تلكرام يفتح اتصالات قصيرة لتحميل الصور والفيديو.",
                 "ℹ️ Many new connections are normal: Telegram opens short connections to download media."))
    return "\n".join(out) + footer()


# --------------------------------------------------------- agent (node) ----

def agent_status():
    ok = True
    try:
        users = get_users()
    except ApiError:
        users, ok = [], False
    try:
        doc = TomlDoc()
        cu, tag = config_users(doc), read_ad_tag(doc)
    except OSError:
        cu, tag = {}, None
    live = {u.get("username") for u in users if u.get("in_runtime", True)}
    try:
        traffic = get_traffic()
    except Exception:
        traffic = {}
    return {
        "version": AGENT_VERSION, "telemt_ok": ok,
        "cfg_hash": cfg_hash(cu, tag), "runtime_ok": ok and set(cu) <= live,
        "users": [{"username": u.get("username"),
                   "current_connections": int(u.get("current_connections") or 0),
                   "active_unique_ips": int(u.get("active_unique_ips") or 0)} for u in users],
        "traffic": traffic, "sys": system_usage(), "tuning": tuning_checks(),
        "ad_tag": bool(tag), "info": api_optional("/v1/system/info") if ok else {},
    }


def agent_apply(p):
    want = {}
    for k, v in (p.get("users") or {}).items():
        v = str(v).lower()
        if NAME_RE.fullmatch(str(k)) and HEX32_RE.match(v):
            want[str(k)] = v
    tag = str(p.get("ad_tag") or "").lower() or None
    if tag and not HEX32_RE.match(tag):
        tag = None
    res = {"created": [], "updated": [], "deleted": [], "ad_tag_changed": False}

    def fn(doc):
        have = doc.strings("access.users")
        enabled = doc.raw("access.user_enabled")
        for name, sec in want.items():
            if name not in have:
                doc.set("access.users", name, json.dumps(sec))
                res["created"].append(name)
            elif have[name].lower() != sec:
                doc.set("access.users", name, json.dumps(sec))
                res["updated"].append(name)
            if enabled.get(name) == "false":
                doc.delete("access.user_enabled", name)
                res["updated"].append(name)
        if p.get("prune") and want:
            for name in have:
                if name not in want:
                    delete_user_lines(doc, name)
                    res["deleted"].append(name)
        if (read_ad_tag(doc) or None) != tag:
            if tag:
                doc.set("general", "ad_tag", json.dumps(tag))
            else:
                doc.delete("general", "ad_tag")
            res["ad_tag_changed"] = True

    if want:
        toml_mutate(fn)
    speed = p.get("nic_speed_mbps")
    if isinstance(speed, int) and speed >= 0 and speed != int(CFG.get("nic_speed_mbps") or 0):
        CFG["nic_speed_mbps"] = speed
        save_cfg()
    res["applied"] = wait_runtime(set(want)) if want else True
    doc = TomlDoc()
    res["cfg_hash"] = cfg_hash(config_users(doc), read_ad_tag(doc))
    return res


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "mazugate-agent"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def setup(self):
        self.request.settimeout(30)
        self.request.do_handshake()
        super().setup()

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authorized(self):
        return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + CFG["token"])

    def do_GET(self):
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        try:
            if self.path == "/status":
                return self._reply(200, agent_status())
            if self.path == "/diag":
                return self._reply(200, diag_collect(3.0))
        except Exception as e:
            return self._reply(500, {"error": str(e)})
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            return self._reply(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._reply(400, {"error": "bad json"})
        if self.path == "/sync":
            try:
                return self._reply(200, agent_apply(body))
            except Exception as e:
                log("sync failed:", e)
                return self._reply(500, {"error": str(e)})
        self._reply(404, {"error": "not found"})


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def verify_request(self, request, client_address):
        # only the main server may talk to the agent
        return client_address[0] in {str(x) for x in CFG.get("master_ips") or []}

    def handle_error(self, request, client_address):
        pass


def agent_main():
    if not CFG.get("token") or not CFG.get("master_ips"):
        log("agent: token/master_ips missing in", CONFIG_PATH)
        sys.exit(1)
    threading.Thread(target=sys_monitor, daemon=True).start()
    srv = AgentServer(("0.0.0.0", int(CFG.get("agent_port") or 9443)), AgentHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CFG["cert"], CFG["key"])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    log(f"{BRAND} agent v{AGENT_VERSION} listening on {srv.server_address}, masters: {CFG.get('master_ips')}")
    srv.serve_forever()


# -------------------------------------------------------------- screens ----

def ad_status():
    return L("مربوطة ✅", "linked ✅") if read_ad_tag() else L("غير مربوطة ❌", "not linked ❌")


def server_rows():
    """One row per server: live data or why it is missing."""
    with STATE_LOCK:
        today = {k: list(v) for k, v in (STATE.get("servers_today") or {}).items()}
    rows = []
    main_title = node_name("local") + (L(" (الرئيسي)", " (main)") if nodes() else "")
    try:
        users = get_users()
        rows.append({"src": "local", "title": main_title, "dot": "🟢", "live": True, "users": users,
                     "sys": system_usage(), "today": today.get("local", [0, 0]), "note": ""})
    except ApiError as e:
        rows.append({"src": "local", "title": main_title, "dot": "🔴", "live": False, "users": [],
                     "note": L("البروكسي ما يستجيب: ", "proxy not responding: ") + str(e),
                     "today": today.get("local", [0, 0])})
    for n in nodes():
        ip, c = n["ip"], NODE_CACHE.get(n["ip"]) or {}
        row = {"src": ip, "title": node_name(ip), "today": today.get(ip, [0, 0]), "users": []}
        d = node_fresh(ip)
        if not n.get("joined"):
            row.update(dot="⏳", live=False, note=L("بانتظار التنصيب", "waiting for installation"))
        elif c.get("down") or not d:
            row.update(dot="🔴", live=False, note=tr(c.get("err")) or L("ما يرد", "not responding"))
        else:
            row.update(dot="🟢" if c.get("ready") else "🟡", live=True, users=d.get("users") or [],
                       sys=d.get("sys") or {},
                       note="" if c.get("ready") else
                       L("مو داخل بالتوزيع: ", "not serving users: ")
                       + (tr(c.get("sync_err")) or tr(c.get("err")) or L("دا يتزامن", "syncing")))
        rows.append(row)
    for r in rows:
        r["conns"] = sum(int(u.get("current_connections") or 0) for u in r["users"])
        r["ips"] = sum(int(u.get("active_unique_ips") or 0) for u in r["users"])
    return rows


def server_block(r):
    out = [f"🖥 <b>{esc(r['title'])}</b> {r['dot']}"]
    if not r["live"]:
        out.append(f"• {esc(r['note'])}")
        return out
    v = r.get("sys") or {}
    out += [L("• المتصلين: ", "• Connected: ") + f"<b>{fmt_num(r['conns'])}</b>" + L(" اتصال | ", " conns | ")
            + f"<b>{fmt_num(r['ips'])}</b> IP",
            L("• الكارت: ", "• Network: ") + f"⬇️ {fmt_rate(v.get('rx'))} | ⬆️ {fmt_rate(v.get('tx'))}",
            f"   {bar(v.get('nic_pct'))} <b>{v.get('nic_pct', 0):.0f}%</b> " + L("من ", "of ")
            + fmt_rate(v.get('speed', 1000)) + (L(" (تقديري)", " (estimated)") if v.get("speed_guess") else ""),
            L("• المعالج: ", "• CPU: ") + f"{bar(v.get('cpu'))} <b>{v.get('cpu', 0):.0f}%</b>",
            L("• الرام: ", "• RAM: ") + f"{bar(v.get('ram_pct'))} <b>{v.get('ram_pct', 0):.0f}%</b> ({gb(v.get('ram_used'))})",
            L("• اليوم: ", "• Today: ") + f"⬇️ {gb(r['today'][0])} | ⬆️ {gb(r['today'][1])}"]
    if r.get("note"):
        out.append(f"⚠️ {esc(r['note'])}")
    return out


def stats_text():
    rows = server_rows()
    conns = sum(r["conns"] for r in rows)
    ips = sum(r["ips"] for r in rows)
    with STATE_LOCK:
        s = {k: STATE[k] for k in ("today_down", "today_up", "total_down", "total_up", "peak_conns", "peak_ips")}
        ut = {k: list(v) for k, v in (STATE.get("users_today") or {}).items()}
    out = [L("📊 <b>الإحصائيات</b>", "📊 <b>Statistics</b>") + f"  •  {now_local():%Y-%m-%d %H:%M}", "",
           L("👥 المتصلين الآن: ", "👥 Connected now: ") + f"<b>{fmt_num(conns)}</b>" + L(" اتصال | ", " conns | ")
           + f"<b>{fmt_num(ips)}</b> IP",
           L("🔝 أعلى عدد اليوم: ", "🔝 Today's peak: ") + f"<b>{fmt_num(max(s['peak_conns'], conns))}</b>"
           + L(" اتصال | ", " conns | ") + f"<b>{fmt_num(max(s['peak_ips'], ips))}</b> IP",
           L("⬇️ تحميل اليوم: ", "⬇️ Downloaded today: ") + f"<b>{gb(s['today_down'])}</b>",
           L("⬆️ رفع اليوم: ", "⬆️ Uploaded today: ") + f"<b>{gb(s['today_up'])}</b>",
           L("📦 الكلي: ", "📦 All time: ") + f"⬇️ {gb(s['total_down'])} | ⬆️ {gb(s['total_up'])}"]
    for r in rows:
        out += [""] + server_block(r)
    per_link = {}
    for r in rows:
        for u in r["users"]:
            pl = per_link.setdefault(u.get("username"), [0, 0])
            pl[0] += int(u.get("current_connections") or 0)
            pl[1] += int(u.get("active_unique_ips") or 0)
    if len(per_link) > 1:
        out += ["", L("🔗 <b>الروابط</b>", "🔗 <b>Links</b>") + f" ({len(per_link)})"]
        for name, (c_, i_) in sorted(per_link.items(), key=lambda kv: -kv[1][0])[:5]:
            d, u = ut.get(name, [0, 0])
            out.append(f"• <b>{esc(name)}</b>: {fmt_num(c_)}" + L(" اتصال | ", " conns | ")
                       + f"{fmt_num(i_)} IP | " + L("اليوم ", "today ") + gb(d + u))
    out += ["", L("📢 القناة الإعلانية: ", "📢 Sponsored channel: ") + ad_status()]
    return "\n".join(out) + footer()


def links_text():
    try:
        doc = TomlDoc()
        cu = config_users(doc)
    except OSError as e:
        return L("⚠️ ما كدرت أقرا إعدادات البروكسي: ", "⚠️ Could not read the proxy config: ") + esc(e)
    try:
        api_users = {u["username"]: u for u in get_users()}
    except ApiError:
        api_users = {}
    if not cu:
        return L("ماكو روابط. اضغط ➕ رابط جديد.", "No links yet. Press ➕ New link.")
    conns = {}
    for r in server_rows():
        for u in r["users"]:
            conns[u.get("username")] = conns.get(u.get("username"), 0) + int(u.get("current_connections") or 0)
    parts = [L("🔗 <b>روابط البروكسي</b>", "🔗 <b>Proxy links</b>")]
    if CFG.get("link_host"):
        parts.append(L("🌍 على الدومين ", "🌍 On domain ") + f"<code>{esc(CFG['link_host'])}</code>")
    for name, secret in cu.items():
        links = api_links(api_users.get(name) or {}) or [build_link(secret, doc)]
        parts.append(f"\n👤 <b>{esc(name)}</b> — " + L("متصلين: ", "connected: ") + fmt_num(conns.get(name, 0)))
        for link in links:
            parts.append(f'<a href="{esc(link)}">' + L("📲 اضغط للاتصال", "📲 Tap to connect")
                         + f'</a>\n<code>{esc(link)}</code>')
    return "\n".join(parts) + footer()


def ad_text():
    try:
        doc = TomlDoc()
        cu, tag = config_users(doc), read_ad_tag(doc)
    except OSError:
        cu, tag = {}, None
    secret = esc(cu.get("hello") or next(iter(cu.values()), ""))
    host = esc(f"{CFG['public_ip']}:{CFG['port']}")
    status = (L("مربوطة ✅", "linked ✅") + f"\n{L('التاك', 'Tag')}: <code>{tag}</code>") if tag else L("غير مربوطة ❌", "not linked ❌")
    return L("📢 <b>القناة الإعلانية</b>\n"
             f"الحالة: {status}\n\n"
             "<b>طريقة الربط:</b>\n"
             "1️⃣ افتح @MTProxybot واكتب /newproxy\n"
             f"2️⃣ يطلب العنوان، ارسله هذا:\n<code>{host}</code>\n"
             f"3️⃣ يطلب السر (secret)، ارسله هذا:\n<code>{secret}</code>\n"
             "4️⃣ ياخذ منك <b>proxy tag</b> (32 حرف). انسخه\n"
             "5️⃣ اضغط «✏️ أرسل التاك» تحت وارسله إلي\n"
             "6️⃣ رجع لـ @MTProxybot ← /myproxies ← البروكسي ← <b>Set promotion</b> ← معرف قناتك\n\n"
             "ℹ️ التاك ينطبق على كل الروابط وكل السيرفرات تلقائياً.",
             "📢 <b>Sponsored channel</b>\n"
             f"Status: {status}\n\n"
             "<b>How to link it:</b>\n"
             "1️⃣ Open @MTProxybot and send /newproxy\n"
             f"2️⃣ When asked for the address, send:\n<code>{host}</code>\n"
             f"3️⃣ When asked for the secret, send:\n<code>{secret}</code>\n"
             "4️⃣ It gives you a <b>proxy tag</b> (32 chars). Copy it\n"
             "5️⃣ Press «✏️ Send tag» below and send it to me\n"
             "6️⃣ Back in @MTProxybot → /myproxies → your proxy → <b>Set promotion</b> → your channel\n\n"
             "ℹ️ The tag applies to every link and every server automatically.") + footer()


def ad_kb():
    kb = [[btn(L("✏️ أرسل التاك", "✏️ Send tag"), "ad_set")]]
    if read_ad_tag():
        kb.append([btn(L("❌ إلغاء ربط القناة", "❌ Unlink channel"), "ad_off")])
    return kb + back_kb()


def history_text():
    with STATE_LOCK:
        hist = list(STATE["history"])[-7:]
    if not hist:
        return L("📅 بعد ماكو سجل أيام. أول تقرير يومي يوصل بعد نص الليل.",
                 "📅 No history yet. The first daily report arrives after midnight.")
    lines = [L("📅 <b>آخر 7 أيام</b>", "📅 <b>Last 7 days</b>")]
    for h in reversed(hist):
        lines.append(f"\n<b>{h['day']}</b>\n• ⬇️ {gb(h['down'])} | ⬆️ {gb(h['up'])}\n"
                     + L("• أعلى عدد: ", "• Peak: ") + f"{fmt_num(h['peak_conns'])}"
                     + L(" اتصال | ", " conns | ") + f"{fmt_num(h.get('peak_ips', 0))} IP")
        for src, sv in (h.get("servers") or {}).items():
            lines.append(f"• {esc(node_name(src))}: {gb(sum(sv.get('today', [0, 0])))} | "
                         + L("أعلى كارت ", "peak net ") + fmt_rate(max(sv.get('rx', 0), sv.get('tx', 0))))
    return "\n".join(lines)


SEP = "━━━━━━━━━━━━━━"


def daily_report_text(rec):
    out = [L("🗓 <b>التقرير اليومي — ", "🗓 <b>Daily report — ") + f"{rec['day']}</b>", "",
           L("⬇️ التحميل: ", "⬇️ Downloaded: ") + f"<b>{gb(rec['down'])}</b>",
           L("⬆️ الرفع: ", "⬆️ Uploaded: ") + f"<b>{gb(rec['up'])}</b>",
           L("📦 المجموع: ", "📦 Total: ") + f"<b>{gb(rec['down'] + rec['up'])}</b>",
           L("🔝 أعلى عدد متصلين: ", "🔝 Peak connected: ") + f"<b>{fmt_num(rec['peak_conns'])}</b>"
           + L(" اتصال | ", " conns | ") + f"<b>{fmt_num(rec['peak_ips'])}</b> IP"]
    for src, sv in (rec.get("servers") or {}).items():
        out.append(f"🖥 {esc(node_name(src))}: {gb(sum(sv.get('today', [0, 0])))} | "
                   + L("أعلى كارت ", "peak net ") + fmt_rate(max(sv.get('rx', 0), sv.get('tx', 0))) + " | "
                   + L("معالج ", "CPU ") + f"{sv.get('cpu', 0):.0f}% | " + L("رام ", "RAM ") + f"{sv.get('ram', 0):.0f}%")
    if rec.get("top_user") and rec.get("links", 1) > 1:
        out.append(L("🏆 أكثر رابط: ", "🏆 Top link: ") + f"<b>{esc(rec['top_user'])}</b> ({gb(rec['top_bytes'])})")
    return "\n".join(out) + f"\n\n{SEP}\n" + stats_text()


def periodic_text(hours, down, up):
    return (L(f"⏱ <b>تقرير كل {hours} ساعات</b>\n📦 آخر {hours} ساعات: ",
              f"⏱ <b>Report every {hours} hours</b>\n📦 Last {hours} hours: ")
            + f"⬇️ <b>{gb(down)}</b> | ⬆️ <b>{gb(up)}</b>\n{SEP}\n" + stats_text())


def settings_text():
    ph = int(CFG.get("periodic_hours") or 0)
    sp = int(CFG.get("nic_speed_mbps") or 0)
    speed, guess = nic_speed(_default_iface() or "?")
    period = (L(f"كل {ph} ساعات", f"every {ph} hours") if ph else L("متوقف", "off"))
    how = L(" (يدوي)", " (manual)") if sp else (L(" (تقديري)", " (estimated)") if guess else L(" (من الكارت)", " (from NIC)"))
    domain = esc(CFG.get("link_host") or L("ماكو (الروابط بالـ IP)", "none (links use the IP)"))
    return (L("⚙️ <b>الإعدادات</b>\n\n", "⚙️ <b>Settings</b>\n\n")
            + L("⏱ التقرير الدوري: ", "⏱ Periodic report: ") + f"<b>{period}</b>\n"
            + L("🗓 التقرير اليومي: الساعة ", "🗓 Daily report at ") + f"<b>{int(CFG['report_hour']):02d}:00</b>\n"
            + L("🌍 دومين الروابط: ", "🌍 Link domain: ") + f"<b>{domain}</b>\n"
            + L("🌐 سرعة البورت: ", "🌐 Port speed: ") + f"<b>{fmt_rate(speed)}</b>{how}\n\n"
            + L("ℹ️ سرعة البورت تنطبق على كل السيرفرات، وتستعمل لحساب نسبة استهلاك الكارت.",
                "ℹ️ The port speed applies to every server and is used for the network usage %.")) + footer()


def settings_kb():
    return [[btn(L("⏱ مدة التقرير الدوري", "⏱ Report interval"), "set_period")],
            [btn(L("🌐 سرعة البورت", "🌐 Port speed"), "set_speed"),
             btn(L("🌍 دومين الروابط", "🌍 Link domain"), "set_domain")],
            [btn("🌐 Language / اللغة", "lang")]] + back_kb()


def servers_text():
    out = [L("🖧 <b>السيرفرات</b>", "🖧 <b>Servers</b>"),
           L("كل السيرفرات تشتغل بنفس الروابط ونفس التاك، والدومين يوزع المشتركين على السيرفرات الجاهزة.",
             "All servers share the same links and tag; the domain spreads users over the ready servers."), ""]
    for r in server_rows():
        ip = CFG['public_ip'] if r['src'] == 'local' else r['src']
        line = f"{r['dot']} <b>{esc(r['title'])}</b> <code>{ip}</code>"
        if r["live"]:
            line += f": {fmt_num(r['conns'])}" + L(" اتصال", " conns")
        out.append(line)
        t = tuning_checks() if r["src"] == "local" else (node_fresh(r["src"]) or {}).get("tuning") or {}
        if t.get("missing"):
            out.append("   ⚠️ " + L("التهيئة ناقصة: ", "tuning incomplete: ") + tuning_names(t["missing"]))
        elif t:
            out.append("   ⚡ " + L("التهيئة للضغط العالي: كاملة ✅", "high-load tuning: complete ✅"))
        if r.get("note"):
            out.append(f"   ⚠️ {esc(r['note'])}")
    domain = CFG.get("link_host")
    out.append("")
    if not domain:
        out.append(L("⚠️ <b>لازم تربط دومين أول</b> من ⚙️ الإعدادات ← 🌍 دومين الروابط، لأن التوزيع يصير عن طريق الدومين.",
                     "⚠️ <b>Link a domain first</b> in ⚙️ Settings → 🌍 Link domain; users are spread through the domain."))
    elif cf_enabled():
        out.append(L(f"☁️ كلاودفلير مربوط ✅ — سجلات <code>{esc(domain)}</code> يديرها البوت: السيرفر ينضاف لما يجهز وينشال إذا وگف.",
                     f"☁️ Cloudflare linked ✅ — the bot manages <code>{esc(domain)}</code>: servers are added when ready and removed when down."))
    else:
        have, want = set(resolve_a(domain)), dns_wanted_ips()
        out.append(L(f"🌍 <code>{esc(domain)}</code> يأشر على: ", f"🌍 <code>{esc(domain)}</code> points to: ")
                   + f"<code>{esc(', '.join(sorted(have)) or '—')}</code>")
        missing = sorted(want - have)
        bad = sorted(ip for ip in have - want if ip != CFG["public_ip"])
        if missing:
            out.append(L("⚠️ ضيف سجل <b>A</b> (غيمة رمادية) لكل IP: ", "⚠️ Add an <b>A</b> record (grey cloud) for: ")
                       + ", ".join(f"<code>{ip}</code>" for ip in missing))
        if bad:
            out.append(L("🔴 <b>شيل هذني من الدومين</b> لأنهم مو جاهزين: ", "🔴 <b>Remove these from the domain</b>, they are not ready: ")
                       + ", ".join(f"<code>{ip}</code>" for ip in bad))
        out.append(L("💡 اربط كلاودفلير حتى البوت يدير السجلات وحده.", "💡 Link Cloudflare and the bot manages the records for you."))
    out += ["", L("🔄 <b>التحديث</b>: شغل هذا على كل سيرفر:", "🔄 <b>Update</b>: run this on every server:"),
            f"<code>{esc(update_cmd())}</code>"]
    return "\n".join(out) + footer()


def servers_kb():
    kb = [[btn(L("➕ إضافة سيرفر", "➕ Add server"), "node_add")]]
    if joined_nodes():
        kb.append([btn(L("🔄 مزامنة الآن", "🔄 Sync now"), "sync_now")])
    kb.append([btn(L("❌ فك ربط كلاودفلير", "❌ Unlink Cloudflare"), "cf_off")] if cf_enabled()
              else [btn(L("☁️ ربط كلاودفلير", "☁️ Link Cloudflare"), "cf_set")])
    for n in nodes():
        kb.append([btn(L("🗑 حذف ", "🗑 Remove ") + node_name(n["ip"]), "node_rm:" + n["ip"])])
    return kb + back_kb()


def cf_help():
    return L("☁️ <b>ربط كلاودفلير</b>\n\n"
             "1️⃣ ادخل كلاودفلير ← فوگ يمين على حسابك ← <b>My Profile</b> ← <b>API Tokens</b>\n"
             "2️⃣ <b>Create Token</b> ← اختار قالب <b>Edit zone DNS</b>\n"
             "3️⃣ بـ <b>Zone Resources</b> اختار الدومين مالتك ← <b>Continue</b> ← <b>Create Token</b>\n"
             "4️⃣ انسخ التوكن وارسله هنا\n\n"
             "🔒 التوكن يتحفظ بالسيرفر بس، وصلاحيته على DNS الدومين هذا بس.",
             "☁️ <b>Link Cloudflare</b>\n\n"
             "1️⃣ In Cloudflare: top right, your account → <b>My Profile</b> → <b>API Tokens</b>\n"
             "2️⃣ <b>Create Token</b> → template <b>Edit zone DNS</b>\n"
             "3️⃣ Under <b>Zone Resources</b> pick your domain → <b>Continue</b> → <b>Create Token</b>\n"
             "4️⃣ Copy the token and send it here\n\n"
             "🔒 The token stays on your server and only has access to this domain's DNS.")


# ------------------------------------------------------------- handlers ----

PENDING = {}  # chat_id -> mode waiting for text input
ACCESS_REQ = {}  # uid -> last request time (anti-spam)


def menu_text():
    return (f"🛡 <b>{BRAND}</b>\n" + L("لوحة تحكم البروكسي. اختار من الأزرار:", "Proxy control panel. Pick an option:")
            + footer())


def display_name(u):
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or str(u.get("id"))
    if u.get("username"):
        name += f" (@{u['username']})"
    return name


def admins_text():
    lines = [L("👮 <b>الأدمنية</b>", "👮 <b>Admins</b>"),
             L("كل الأدمنية عدهم نفس صلاحيات المالك بالكامل: يشوفون كلشي ويتحكمون بكلشي، ويوصلهم كل تقرير وتنبيه.\n",
               "Every admin has the owner's full permissions: sees and controls everything and gets every report and alert.\n"),
             L("👑 المالك: ", "👑 Owner: ") + f"<code>{OWNER}</code>"]
    if not ADMINS:
        lines.append(L("\nماكو أدمنية بعد.", "\nNo admins yet."))
    for a in sorted(ADMINS):
        lines.append(f"• {esc(ADMIN_NAMES.get(str(a), ''))} <code>{a}</code>")
    lines.append(L("\n<b>طرق الرفع:</b> «➕ رفع أدمن» وارسل الـ ID أو حوّل رسالة منه، أو خلي الشخص يدوس /start ويوصلكم طلب.",
                   "\n<b>How to add:</b> «➕ Add admin» and send the ID or forward a message, or let the person press /start and approve the request."))
    return "\n".join(lines)


def admins_kb():
    kb = [[btn(L("➕ رفع أدمن", "➕ Add admin"), "adm_add")]]
    for a in sorted(ADMINS):
        kb.append([btn(L("⬇️ تنزيل ", "⬇️ Remove ") + (ADMIN_NAMES.get(str(a)) or str(a))[:30], f"adm_rm:{a}")])
    return kb + back_kb()


def promote(uid, name=None):
    if uid == OWNER:
        return False
    if not name:
        try:
            name = display_name(tg("getChat", chat_id=uid))
        except Exception:
            name = str(uid)
    ADMINS.add(uid)
    ADMIN_NAMES[str(uid)] = name
    save_cfg()
    send(uid, f"✅ <b>{BRAND}</b>\nانرفعت أدمن، عندك صلاحيات كاملة.\nYou are now an admin with full permissions.\n\n"
              "👇 اختار اللغة / Choose your language", LANG_KB)
    return True


def demote(uid):
    ADMINS.discard(uid)
    ADMIN_NAMES.pop(str(uid), None)
    save_cfg()
    PENDING.pop(uid, None)
    send(uid, "ℹ️ انشالت صلاحية الأدمن مالتك.\nYour admin access was removed.")


def handle_action(chat_id, action, msg_id=None, uid=None):
    uid = chat_id if uid is None else uid
    if not is_admin(uid):
        return
    if action == "menu":
        show(chat_id, msg_id, menu_text(), main_kb())
    elif action == "lang":
        show(chat_id, msg_id, LANG_TEXT, LANG_KB)
    elif action.startswith("lang:"):
        lang = "en" if action.endswith("en") else "ar"
        CFG.setdefault("langs", {})[str(uid)] = lang
        save_cfg()
        set_lang(lang)
        show(chat_id, msg_id, menu_text(), main_kb())
    elif action == "stats":
        show(chat_id, msg_id, stats_text(), [[btn(L("🔄 تحديث", "🔄 Refresh"), "stats")]] + back_kb())
    elif action == "links":
        show(chat_id, msg_id, links_text(), [[btn(L("🔄 تحديث", "🔄 Refresh"), "links")]] + back_kb())
    elif action == "history":
        show(chat_id, msg_id, history_text(), back_kb())
    elif action == "diag":
        show(chat_id, msg_id, L("🩺 دا أفحص كل السيرفرات (5 ثواني)...", "🩺 Checking every server (5 seconds)..."))
        show(chat_id, msg_id, diag_text(diag_collect_all()),
             [[btn(L("🔄 إعادة الفحص", "🔄 Check again"), "diag")]] + back_kb())
    elif action == "ad":
        show(chat_id, msg_id, ad_text(), ad_kb())
    elif action == "ad_set":
        PENDING[chat_id] = "ad_tag"
        send(chat_id, L("✏️ ارسل الـ proxy tag اللي عطاك اياه @MTProxybot (32 حرف):",
                        "✏️ Send the proxy tag @MTProxybot gave you (32 characters):"), cancel_kb())
    elif action == "ad_off":
        try:
            set_ad_tag(None)
        except Exception as e:
            send(chat_id, L("⚠️ فشل: ", "⚠️ Failed: ") + esc(e), back_kb())
            return
        trigger_sync()
        show(chat_id, msg_id, L("✅ تم إلغاء ربط القناة الإعلانية من كل السيرفرات.",
                                "✅ The sponsored channel was unlinked on every server."), back_kb())
    elif action == "new":
        PENDING[chat_id] = "new_user"
        send(chat_id, L("➕ ارسل اسم للرابط الجديد (حروف إنكليزية وأرقام، مثال: <code>vip1</code>):",
                        "➕ Send a name for the new link (letters and digits, e.g. <code>vip1</code>):"), cancel_kb())
    elif action == "del":
        try:
            names = list(config_users())
        except OSError as e:
            send(chat_id, f"⚠️ {esc(e)}", back_kb())
            return
        if len(names) <= 1:
            show(chat_id, msg_id, L("ℹ️ عندك رابط واحد بس، وما ينحذف آخر رابط.",
                                    "ℹ️ You have only one link and the last link cannot be deleted."), back_kb())
            return
        show(chat_id, msg_id, L("اختار الرابط اللي تريد تحذفه:", "Pick the link to delete:"),
             [[btn(f"🗑 {n}", "rm:" + n)] for n in names] + back_kb())
    elif action.startswith("rm:"):
        name = esc(action[3:])
        show(chat_id, msg_id, L(f"متأكد تحذف <b>{name}</b>؟ اللي يستعملونه راح ينگطعون.",
                                f"Delete <b>{name}</b>? Everyone using it will be disconnected."),
             [[btn(L("✅ إي احذف", "✅ Yes, delete"), "rmy:" + action[3:]), btn(L("❌ لا", "❌ No"), "menu")]])
    elif action.startswith("rmy:"):
        name = action[4:]
        try:
            delete_user(name)
        except ConfigError as e:
            msg = {"last": L("ما ينحذف آخر رابط.", "The last link cannot be deleted."),
                   "missing": L("هذا الرابط مو موجود.", "That link does not exist.")}.get(str(e), str(e))
            show(chat_id, msg_id, f"⚠️ {esc(msg)}", back_kb())
            return
        except Exception as e:
            show(chat_id, msg_id, L("⚠️ فشل الحذف: ", "⚠️ Delete failed: ") + esc(e), back_kb())
            return
        trigger_sync()
        show(chat_id, msg_id, L(f"✅ انحذف <b>{esc(name)}</b>", f"✅ Deleted <b>{esc(name)}</b>")
             + (L(" من كل السيرفرات.", " on every server.") if joined_nodes() else "."), back_kb())
    elif action == "servers":
        show(chat_id, msg_id, servers_text(), servers_kb())
    elif action == "node_add":
        if not CFG.get("link_host"):
            show(chat_id, msg_id, L("⚠️ اربط دومين أول من ⚙️ الإعدادات ← 🌍 دومين الروابط.\nالتوزيع على السيرفرات يصير عن طريق الدومين.",
                                    "⚠️ Link a domain first in ⚙️ Settings → 🌍 Link domain.\nUsers are spread over servers through the domain."),
                 [[btn(L("⚙️ الإعدادات", "⚙️ Settings"), "settings")]] + back_kb())
            return
        PENDING[chat_id] = "add_node"
        send(chat_id, L("➕ ارسل <b>IP السيرفر الجديد</b> (Ubuntu 24، فارغ):",
                        "➕ Send the <b>IP of the new server</b> (fresh Ubuntu 24):"), cancel_kb())
    elif action.startswith("node_rm:"):
        ip = action[8:]
        show(chat_id, msg_id, L(f"متأكد تحذف السيرفر <code>{esc(ip)}</code>؟\nالمشتركين اللي عليه يتحولون للسيرفرات الباقية لما يعيدون الاتصال.",
                                f"Remove server <code>{esc(ip)}</code>?\nIts users move to the other servers when they reconnect."),
             [[btn(L("✅ إي احذف", "✅ Yes, remove"), "node_rmy:" + ip), btn(L("❌ لا", "❌ No"), "servers")]])
    elif action.startswith("node_rmy:"):
        ip = action[9:]
        CFG["nodes"] = [n for n in CFG.get("nodes") or [] if n["ip"] != ip]
        save_cfg()
        NODE_CACHE.pop(ip, None)
        dns_reconcile_safe()
        show(chat_id, msg_id, L(f"✅ انحذف <code>{esc(ip)}</code> من النظام", f"✅ Removed <code>{esc(ip)}</code>")
             + (L(" ومن الدومين.", " and from the domain.") if cf_enabled()
                else L(".\n⚠️ شيل سجله من الدومين.", ".\n⚠️ Remove its record from the domain."))
             + L("\n\nحتى تمسح البروكسي من السيرفر نفسه شغل عليه:", "\n\nTo wipe the proxy from that server run on it:")
             + "\n<code>sudo bash uninstall.sh all</code>", servers_kb())
    elif action == "sync_now":
        show(chat_id, msg_id, L("🔄 دا أزامن...", "🔄 Syncing..."))
        res = sync_all()
        want = desired_state()["hash"]
        lines = [L("🔄 <b>نتيجة المزامنة</b>", "🔄 <b>Sync result</b>")]
        for ip, r in res.items():
            if isinstance(r, dict) and "created" in r:
                ok = r.get("applied") and r.get("cfg_hash") == want
                lines.append(f"{'✅' if ok else '⚠️'} <code>{ip}</code>: "
                             + L(f"جديد {len(r['created'])} | تحديث {len(r['updated'])} | حذف {len(r['deleted'])}",
                                 f"new {len(r['created'])} | updated {len(r['updated'])} | deleted {len(r['deleted'])}")
                             + (L(" | التاك تحدث", " | tag updated") if r.get("ad_tag_changed") else "")
                             + ("" if ok else L(" (البروكسي بعده ما طبقها)", " (not applied yet)")))
            else:
                lines.append(f"❌ <code>{ip}</code>: {esc(tr(r))}")
        show(chat_id, msg_id, "\n".join(lines), servers_kb())
    elif action == "cf_set":
        if not CFG.get("link_host"):
            send(chat_id, L("⚠️ اربط دومين أول من ⚙️ الإعدادات ← 🌍 دومين الروابط.",
                            "⚠️ Link a domain first in ⚙️ Settings → 🌍 Link domain."), back_kb())
            return
        PENDING[chat_id] = "set_cf"
        send(chat_id, cf_help(), cancel_kb())
    elif action == "cf_off":
        CFG["cf_token"] = CFG["cf_zone"] = ""
        save_cfg()
        show(chat_id, msg_id, L("✅ فكيت ربط كلاودفلير. السجلات الحالية تبقى مثل ما هي.",
                                "✅ Cloudflare unlinked. Existing records stay as they are."), servers_kb())
    elif action == "settings":
        show(chat_id, msg_id, settings_text(), settings_kb())
    elif action == "set_period":
        PENDING[chat_id] = "set_period"
        send(chat_id, L("⏱ كل كم ساعة توصل الإحصائيات للأدمنية؟ ارسل رقم من 1 لـ 24 (أو 0 حتى توقفها):",
                        "⏱ How often (hours) should stats be sent to admins? Send 1-24 (or 0 to stop):"), cancel_kb())
    elif action == "set_speed":
        PENDING[chat_id] = "set_speed"
        send(chat_id, L("🌐 ارسل سرعة البورت بالـ Mbps (مثال: 2500 = 2.5Gbps)، أو 0 حتى يقراها من الكارت:",
                        "🌐 Send the port speed in Mbps (e.g. 2500 = 2.5Gbps), or 0 to read it from the NIC:"), cancel_kb())
    elif action == "set_domain":
        PENDING[chat_id] = "set_domain"
        ip = esc(CFG['public_ip'])
        send(chat_id, L("🌍 <b>دومين الروابط</b>\n\n"
                        f"1️⃣ سوي سجل <b>A</b> للدومين يأشر على <code>{ip}</code>\n"
                        "2️⃣ إذا بكلاودفلير: الغيمة لازم <b>رمادية ☁️ (DNS only)</b>\n"
                        "3️⃣ ارسل الدومين هنا (مثال: <code>proxy.example.com</code>)\n\n"
                        "حتى ترجع الروابط على الـ IP ارسل <code>0</code>",
                        "🌍 <b>Link domain</b>\n\n"
                        f"1️⃣ Create an <b>A</b> record pointing to <code>{ip}</code>\n"
                        "2️⃣ On Cloudflare the cloud must be <b>grey ☁️ (DNS only)</b>\n"
                        "3️⃣ Send the domain here (e.g. <code>proxy.example.com</code>)\n\n"
                        "Send <code>0</code> to go back to IP links"), cancel_kb())
    elif action == "admins":
        show(chat_id, msg_id, admins_text(), admins_kb())
    elif action == "adm_add":
        PENDING[chat_id] = "add_admin"
        send(chat_id, L("👮 ارسل الـ ID مال الشخص (أرقام)، أو حوّلي رسالة منه، أو ارسل جهة الاتصال مالته:",
                        "👮 Send the person's ID (digits), forward one of their messages, or share their contact:"), cancel_kb())
    elif action.startswith("adm_rm:"):
        demote(int(action[7:]))
        show(chat_id, msg_id, admins_text(), admins_kb())
    elif action.startswith("mk:"):
        target = int(action[3:])
        if target in ADMINS or target == OWNER:
            show(chat_id, msg_id, L("ℹ️ هذا الشخص أدمن من قبل.", "ℹ️ Already an admin."), back_kb())
        elif promote(target):
            show(chat_id, msg_id, L(f"✅ انرفع <code>{target}</code> أدمن.", f"✅ <code>{target}</code> is now an admin."), admins_kb())
    elif action == "ign":
        show(chat_id, msg_id, L("تم التجاهل.", "Ignored."), back_kb())
    elif action == "cancel":
        PENDING.pop(chat_id, None)
        show(chat_id, msg_id, menu_text(), main_kb())


def admin_target_from_message(m):
    """(user_id, name) from a typed ID, a forwarded message or a contact."""
    origin = m.get("forward_origin") or {}
    if origin.get("type") == "user" and origin.get("sender_user"):
        u = origin["sender_user"]
        return u["id"], display_name(u)
    if origin.get("type") == "hidden_user":
        return "hidden", None
    if m.get("forward_from"):
        return m["forward_from"]["id"], display_name(m["forward_from"])
    c = m.get("contact")
    if c:
        if c.get("user_id"):
            return c["user_id"], display_name({"first_name": c.get("first_name"),
                                               "last_name": c.get("last_name"), "id": c["user_id"]})
        return "hidden", None
    t = (m.get("text") or "").strip()
    if re.fullmatch(r"\d{4,15}", t):
        return int(t), None
    return None, None


def _input_add_node(chat_id, text):
    ip = text.strip()
    if not IPV4_RE.fullmatch(ip):
        PENDING[chat_id] = "add_node"
        send(chat_id, L("❌ ارسل IP صحيح (مثال: <code>1.2.3.4</code>):", "❌ Send a valid IP (e.g. <code>1.2.3.4</code>):"), cancel_kb())
        return
    if ip == CFG["public_ip"] or find_node(ip):
        send(chat_id, L("ℹ️ هذا السيرفر موجود بالنظام من قبل.", "ℹ️ This server is already in the system."), servers_kb())
        return
    num = max([1] + [int(n.get("num") or 0) for n in nodes()]) + 1
    node = {"ip": ip, "token": secrets.token_hex(24), "num": num,
            "agent_port": 9443, "joined": False, "added": int(time.time())}
    CFG.setdefault("nodes", []).append(node)
    save_cfg()
    tls_domain = (TomlDoc().get_str("censorship", "tls_domain") or "petrovich.ru")
    cmd = (f"curl -fsSL {CFG['repo_raw']}/install.sh -o install.sh && sudo bash install.sh "
           f"--node {CFG['public_ip']} {node['token']} {CFG['port']} {tls_domain}")
    send(chat_id, L(f"✅ ضفت <b>{esc(node_name(ip))}</b> (<code>{ip}</code>)\n\nادخل على السيرفر الجديد وشغل هذا الأمر (انسخه كامل):\n\n",
                    f"✅ Added <b>{esc(node_name(ip))}</b> (<code>{ip}</code>)\n\nLog into the new server and run this command (copy all of it):\n\n")
         + f"<code>{esc(cmd)}</code>\n\n"
         + L("⏳ بعد التنصيب البوت يحس بيه خلال دقيقة، ينقله الروابط والتاك ويتأكد إنها مطابقة. ما يدخل بالتوزيع إلا لما يجهز تماماً.",
             "⏳ After installing, the bot detects it within a minute, copies links and tag and verifies them. It only gets users once fully ready."),
         servers_kb())


def _input_set_cf(chat_id, text):
    old = (CFG.get("cf_token"), CFG.get("cf_zone"))
    CFG["cf_token"], CFG["cf_zone"] = text.strip(), ""
    try:
        cf("GET", "/user/tokens/verify")
        dns = dns_reconcile()
    except Exception as e:
        CFG["cf_token"], CFG["cf_zone"] = old
        PENDING[chat_id] = "set_cf"
        send(chat_id, L("❌ التوكن ما اشتغل:", "❌ The token did not work:") + f"\n<code>{esc(e)}</code>", cancel_kb())
        return
    save_cfg()
    send(chat_id, L("✅ <b>كلاودفلير مربوط!</b>\nسجلات ", "✅ <b>Cloudflare linked!</b>\nRecords of ")
         + f"<code>{esc(CFG['link_host'])}</code>: <code>{esc(', '.join(sorted(dns_wanted_ips())))}</code>"
         + (L(f"\n(أضفت {len(dns[0])} وشلت {len(dns[1])})", f"\n(added {len(dns[0])}, removed {len(dns[1])})") if dns else ""),
         servers_kb())


def _input_set_domain(chat_id, text):
    domain = text.lower().strip().strip("/").replace("https://", "").replace("http://", "")
    if domain in ("0", "off", "ip"):
        CFG["link_host"] = ""
        save_cfg()
        try:
            set_public_host(None)
        except Exception as e:
            log("public_host:", e)
        send(chat_id, L("✅ رجعت الروابط على الـ IP.\n\n", "✅ Links use the IP again.\n\n") + links_text(), settings_kb())
        return
    err = check_link_domain(domain)
    if err:
        PENDING[chat_id] = "set_domain"
        send(chat_id, err, cancel_kb())
        return
    CFG["link_host"] = domain
    save_cfg()
    try:
        set_public_host(domain)
    except Exception as e:
        log("public_host:", e)
    send(chat_id, L(f"✅ <b>الروابط صارت على {esc(domain)}</b>\nℹ️ الروابط القديمة (بالـ IP) تبقى شغالة.\n\n",
                    f"✅ <b>Links now use {esc(domain)}</b>\nℹ️ Old IP links keep working.\n\n") + links_text(), back_kb())


def _input_number(chat_id, mode, text):
    limit = 24 if mode == "set_period" else 400000
    if not text.isdigit() or int(text) > limit:
        PENDING[chat_id] = mode
        send(chat_id, L(f"❌ ارسل رقم بس (0 لـ {limit}):", f"❌ Send a number (0 to {limit}):"), cancel_kb())
        return
    CFG["periodic_hours" if mode == "set_period" else "nic_speed_mbps"] = int(text)
    save_cfg()
    if mode == "set_period":
        with STATE_LOCK:
            STATE["period_slot"] = ""  # restart the cycle from now
    else:
        trigger_sync()  # nodes use the same port speed
    send(chat_id, L("✅ انحفظ.\n\n", "✅ Saved.\n\n") + settings_text(), settings_kb())


def _input_ad_tag(chat_id, text):
    tag = text.lower().replace(" ", "")
    if not HEX32_RE.match(tag):
        PENDING[chat_id] = "ad_tag"
        send(chat_id, L("❌ التاك لازم يكون 32 حرف (0-9 و a-f). ارسله مرة ثانية:",
                        "❌ The tag must be 32 characters (0-9, a-f). Send it again:"), cancel_kb())
        return
    try:
        set_ad_tag(tag)
    except Exception as e:
        send(chat_id, L("⚠️ فشل حفظ التاك: ", "⚠️ Could not save the tag: ") + esc(e), back_kb())
        return
    trigger_sync()
    send(chat_id, L("✅ <b>تم ربط التاك!</b>", "✅ <b>Tag linked!</b>")
         + (L(" (ونقلته لكل السيرفرات)", " (copied to every server)") if joined_nodes() else "") + "\n\n"
         + L("هسه رجع لـ @MTProxybot ← /myproxies ← البروكسي ← <b>Set promotion</b> وارسل معرف قناتك.\n\n"
             "ℹ️ الإعلان يطلع خلال دقائق، وممكن المشتركين يحتاجون يعيدون الاتصال.",
             "Now in @MTProxybot → /myproxies → your proxy → <b>Set promotion</b> → send your channel.\n\n"
             "ℹ️ The channel shows up within minutes; users may need to reconnect."), back_kb())


def _input_new_user(chat_id, text):
    if not NAME_RE.fullmatch(text):
        PENDING[chat_id] = "new_user"
        send(chat_id, L("❌ الاسم لازم يكون حروف إنكليزية/أرقام/_ فقط. جرب مرة ثانية:",
                        "❌ Use English letters, digits or _ only. Try again:"), cancel_kb())
        return
    try:
        secret = create_user(text)
    except ConfigError as e:
        if str(e) == "exists":
            PENDING[chat_id] = "new_user"
            send(chat_id, L("❌ هذا الاسم موجود. ارسل اسم ثاني:", "❌ That name exists. Send another:"), cancel_kb())
        else:
            send(chat_id, L("⚠️ فشل الإنشاء: ", "⚠️ Create failed: ") + esc(e), back_kb())
        return
    except Exception as e:
        send(chat_id, L("⚠️ فشل الإنشاء: ", "⚠️ Create failed: ") + esc(e), back_kb())
        return
    trigger_sync()
    live = wait_runtime({text})
    link = None
    try:
        link = next(iter(api_links(api("GET", "/v1/users/" + urllib.parse.quote(text, safe="")) or {})), None)
    except ApiError:
        pass
    link = link or build_link(secret)
    send(chat_id, L(f"✅ انشأ الرابط <b>{esc(text)}</b>", f"✅ Link <b>{esc(text)}</b> created")
         + (L(" (على كل السيرفرات)", " (on every server)") if joined_nodes() else "") + ":\n\n"
         + f'<a href="{esc(link)}">' + L("📲 اضغط للاتصال", "📲 Tap to connect") + f'</a>\n<code>{esc(link)}</code>'
         + ("" if live else L("\n\n⚠️ البروكسي بعده ما حمّل الرابط، جربه بعد دقيقة.",
                              "\n\n⚠️ The proxy has not loaded it yet, try it in a minute.")), back_kb())


def handle_text(chat_id, text, uid=None, msg=None):
    uid = chat_id if uid is None else uid
    text = (text or "").strip()
    if text.startswith("/"):
        PENDING.pop(chat_id, None)
        cmd = text.split()[0].split("@")[0].lower()
        mapping = {"/start": "menu", "/menu": "menu", "/stats": "stats", "/links": "links",
                   "/diag": "diag", "/ad": "ad", "/history": "history", "/admins": "admins",
                   "/servers": "servers", "/settings": "settings", "/lang": "lang", "/language": "lang"}
        if cmd == "/report":
            send(chat_id, daily_report_text(current_day_record()), back_kb())
        else:
            handle_action(chat_id, mapping.get(cmd, "menu"), uid=uid)
        return
    mode = PENDING.pop(chat_id, None)
    if mode == "add_admin":
        target, name = admin_target_from_message(msg or {"text": text})
        if target == "hidden":
            PENDING[chat_id] = "add_admin"
            send(chat_id, L("❌ هذا الشخص مخفي حسابه بالتحويل. خليه يدوس /start بالبوت أو ارسل الـ ID مالته.",
                            "❌ This person hides their account in forwards. Let them press /start or send their ID."), cancel_kb())
        elif target is None:
            PENDING[chat_id] = "add_admin"
            send(chat_id, L("❌ ارسل ID (أرقام بس) أو حوّل رسالة منه:", "❌ Send an ID (digits) or forward a message:"), cancel_kb())
        elif target == OWNER or target in ADMINS:
            send(chat_id, L("ℹ️ هذا الشخص أدمن من قبل.", "ℹ️ Already an admin."), back_kb())
        else:
            promote(int(target), name)
            send(chat_id, L(f"✅ انرفع <b>{esc(ADMIN_NAMES.get(str(target)))}</b> <code>{target}</code> أدمن.\n"
                            "ℹ️ إذا ما داس /start بالبوت قبل، خليه يدوس حتى توصله الرسائل.",
                            f"✅ <b>{esc(ADMIN_NAMES.get(str(target)))}</b> <code>{target}</code> is now an admin.\n"
                            "ℹ️ If they never pressed /start in the bot, ask them to, so messages reach them."), admins_kb())
    elif mode == "add_node":
        _input_add_node(chat_id, text)
    elif mode == "set_cf":
        _input_set_cf(chat_id, text)
    elif mode == "set_domain":
        _input_set_domain(chat_id, text)
    elif mode in ("set_period", "set_speed"):
        _input_number(chat_id, mode, text)
    elif mode == "ad_tag":
        _input_ad_tag(chat_id, text)
    elif mode == "new_user":
        _input_new_user(chat_id, text)
    else:
        handle_action(chat_id, "menu", uid=uid)


def access_request(m):
    """A stranger pressed /start: tell them their ID and ping the admins once per 10 min."""
    u = m.get("from", {})
    uid = u.get("id")
    send(m["chat"]["id"], f"⛔ <b>{BRAND}</b>\n"
                          f"هذا البوت خاص. الـ ID مالتك: <code>{uid}</code>\nانرسل طلبك للإدارة.\n\n"
                          f"This bot is private. Your ID: <code>{uid}</code>\nYour request was sent to the admins."
                          f"\n\n💻 <i>{CREDIT}</i>")
    now = time.time()
    if now - ACCESS_REQ.get(uid, 0) < 600:
        return
    ACCESS_REQ[uid] = now
    broadcast(lambda: L("🔔 <b>طلب دخول للبوت</b>", "🔔 <b>Access request</b>")
              + f"\n{esc(display_name(u))}\nID: <code>{uid}</code>",
              lambda: [[btn(L("✅ رفعه أدمن", "✅ Make admin"), f"mk:{uid}"), btn(L("❌ تجاهل", "❌ Ignore"), "ign")]])


def handle_update(upd):
    if "callback_query" in upd:
        cq = upd["callback_query"]
        uid = cq["from"]["id"]
        try:
            tg("answerCallbackQuery", callback_query_id=cq["id"])
        except Exception:
            pass
        if not is_admin(uid):
            return
        set_lang(user_lang(uid))
        msg = cq.get("message") or {}
        handle_action(msg.get("chat", {}).get("id", uid), cq.get("data", ""), msg.get("message_id"), uid=uid)
    elif "message" in upd:
        m = upd["message"]
        uid = m.get("from", {}).get("id")
        if m.get("chat", {}).get("type") != "private":
            return
        if not is_admin(uid):
            access_request(m)
            return
        if not user_lang(uid):  # first visit: choose the language
            send(m["chat"]["id"], LANG_TEXT, LANG_KB)
            return
        set_lang(user_lang(uid))
        handle_text(m["chat"]["id"], m.get("text", ""), uid=uid, msg=m)


# -------------------------------------------------------------- sampler ----

def current_day_record():
    with STATE_LOCK:
        ut = STATE.get("users_today") or {}
        top = max(ut.items(), key=lambda kv: kv[1][0] + kv[1][1], default=None)
        servers = {}
        known = set(STATE.get("servers_today") or {}) | set(STATE.get("srv_peaks") or {})
        order = ["local"] + [n["ip"] for n in nodes()]
        for src in [x for x in order if x in known] + sorted(known - set(order)):
            pk = (STATE.get("srv_peaks") or {}).get(src) or {}
            servers[src] = {"today": list((STATE.get("servers_today") or {}).get(src, [0, 0])),
                            "rx": pk.get("rx", 0), "tx": pk.get("tx", 0),
                            "cpu": pk.get("cpu", 0), "ram": pk.get("ram", 0)}
        return {"day": STATE["day"], "down": STATE["today_down"], "up": STATE["today_up"],
                "peak_conns": STATE["peak_conns"], "peak_ips": STATE["peak_ips"],
                "servers": servers, "links": len(ut), "top_user": top[0] if top else None,
                "top_bytes": sum(top[1]) if top else 0}


def _delta(cur, last):
    """Counter delta that survives counter resets (telemt/server restart)."""
    if last is None:
        return 0
    return cur - last if cur >= last else cur


def sample_once():
    per = get_traffic()
    users = get_users()
    conns = sum(int(u.get("current_connections") or 0) for u in users)
    ips = sum(int(u.get("active_unique_ips") or 0) for u in users)
    sources = [("local", per)]
    for n in joined_nodes():
        d = node_fresh(n["ip"])
        if d:
            sources.append((n["ip"], d.get("traffic") or {}))
            conns += sum(int(u.get("current_connections") or 0) for u in d.get("users", []))
            ips += sum(int(u.get("active_unique_ips") or 0) for u in d.get("users", []))
    with STATE_LOCK:
        last, ut, st = STATE["last_users"], STATE["users_today"], STATE["servers_today"]
        dd_t = du_t = 0
        seen = set()
        for src, src_per in sources:
            for name, (d, u) in src_per.items():
                key = f"{src}|{name}"
                seen.add(key)
                ld, lu = last.get(key, (None, None))
                dd, du = _delta(d, ld), _delta(u, lu)
                last[key] = [d, u]
                if dd or du:
                    for rec in (ut.setdefault(name, [0, 0]), st.setdefault(src, [0, 0])):
                        rec[0] += dd
                        rec[1] += du
                dd_t += dd
                du_t += du
        # drop baselines of deleted links / removed servers; keep those of a
        # server that just didn't answer this round (its counters continue later)
        fresh = {src for src, _ in sources}
        valid = {"local"} | {n["ip"] for n in nodes()}
        for key in list(last):
            src = key.split("|", 1)[0] if "|" in key else None
            if src not in valid or (src in fresh and key not in seen):
                del last[key]
        for k, inc in (("today_down", dd_t), ("total_down", dd_t), ("period_down", dd_t),
                       ("today_up", du_t), ("total_up", du_t), ("period_up", du_t)):
            STATE[k] += inc
        STATE["peak_conns"] = max(STATE["peak_conns"], conns)
        STATE["peak_ips"] = max(STATE["peak_ips"], ips)


def roll_day_if_needed():
    now = now_local()
    today = now.strftime("%Y-%m-%d")
    with STATE_LOCK:
        day = STATE["day"]
    if day == today:
        return False
    if int(CFG["report_hour"]) and now.hour < int(CFG["report_hour"]):
        return False
    rec = current_day_record()
    text = both(lambda: daily_report_text(rec))  # rendered before the counters reset
    with STATE_LOCK:
        STATE["history"] = (STATE["history"] + [rec])[-60:]
        STATE["day"] = today
        STATE.update(DAILY_KEYS)
        STATE["users_today"], STATE["servers_today"], STATE["srv_peaks"] = {}, {}, {}
    save_state()
    broadcast(text, back_kb)
    return True


def periodic_if_due(send_report=True):
    hours = int(CFG.get("periodic_hours") or 0)
    if hours <= 0:
        return
    now = now_local()
    slot = f"{now:%Y-%m-%d}/{hours}/{now.hour // hours}"
    with STATE_LOCK:
        prev = STATE.get("period_slot")
        if prev == slot:
            return
        STATE["period_slot"] = slot
        down, up = STATE["period_down"], STATE["period_up"]
        STATE["period_down"] = STATE["period_up"] = 0
    if prev and send_report:
        broadcast(lambda: periodic_text(hours, down, up), back_kb)


def sampler():
    api_down_since, alerted, last_save = None, False, 0
    while True:
        if nodes():
            try:
                poll_nodes()
            except Exception:
                traceback.print_exc()
        try:
            sample_once()
            if alerted:
                broadcast(lambda: L("✅ البروكسي رجع يشتغل طبيعي.", "✅ The proxy is running normally again."))
            api_down_since, alerted = None, False
        except Exception as e:
            if api_down_since is None:
                api_down_since = time.time()
            elif not alerted and time.time() - api_down_since > 120:
                broadcast(lambda err=e: L("🚨 <b>تنبيه:</b> البروكسي بالسيرفر الرئيسي ما يستجيب من دقيقتين!",
                                          "🚨 <b>Alert:</b> the proxy on the main server has not responded for 2 minutes!")
                          + f"\n<code>{esc(err)}</code>")
                alerted = True
        rolled = False
        try:
            rolled = roll_day_if_needed()
        except Exception:
            traceback.print_exc()
        try:
            periodic_if_due(send_report=not rolled)  # the daily report already covers that slot
        except Exception:
            traceback.print_exc()
        if time.time() - last_save > 120:
            try:
                save_state()
                last_save = time.time()
            except Exception:
                traceback.print_exc()
        time.sleep(int(CFG["sample_seconds"]))


# ----------------------------------------------------------------- main ----

def welcome():
    with STATE_LOCK:
        if STATE.get("welcomed"):
            return
    for _ in range(30):
        try:
            if wait_runtime(set(config_users()), timeout=2):
                break
        except Exception:
            pass
        time.sleep(2)
    broadcast(lambda: L(f"🎉 <b>تم تنصيب {BRAND} بنجاح!</b>\n\n", f"🎉 <b>{BRAND} installed successfully!</b>\n\n")
              + links_text(), main_kb, owner_only=True)
    broadcast(ad_text, ad_kb, owner_only=True)
    with STATE_LOCK:
        STATE["welcomed"] = True
    save_state()


def setup_bot_profile():
    cmds = {
        "ar": [("start", "القائمة الرئيسية"), ("stats", "الإحصائيات"), ("links", "روابط البروكسي"),
               ("diag", "تشخيص كل السيرفرات"), ("servers", "السيرفرات"), ("ad", "القناة الإعلانية"),
               ("report", "تقرير اليوم لحد الآن"), ("lang", "Language / اللغة")],
        "en": [("start", "Main menu"), ("stats", "Statistics"), ("links", "Proxy links"),
               ("diag", "Diagnose every server"), ("servers", "Servers"), ("ad", "Sponsored channel"),
               ("report", "Today's report so far"), ("lang", "Language / اللغة")],
    }
    desc = {"ar": f"{BRAND} من Mazu: إدارة بروكسيات تلكرام MTProto، إحصائيات، قناة إعلانية وتوزيع على أكثر من سيرفر.\n{CREDIT}",
            "en": f"{BRAND} by Mazu: manage Telegram MTProto proxies, stats, sponsored channel and multi-server.\n{CREDIT}"}
    for lang, items in cmds.items():
        code = {} if lang == "ar" else {"language_code": "en"}
        try:
            tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in items], **code)
            tg("setMyDescription", description=desc[lang], **code)
            tg("setMyShortDescription", short_description=f"{BRAND} • {CREDIT}", **code)
        except Exception as e:
            log("bot profile:", e)


def main():
    if not TOKEN or not OWNER:
        log("bot_token/owner missing in", CONFIG_PATH)
        sys.exit(1)
    setup_bot_profile()
    threading.Thread(target=sys_monitor, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=welcome, daemon=True).start()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    chat_locks, guard = {}, threading.Lock()

    def run(upd):
        # parallel across chats, strictly ordered inside one chat
        if "callback_query" in upd:
            cid = upd["callback_query"]["from"]["id"]
        else:
            cid = (upd.get("message") or {}).get("chat", {}).get("id", 0)
        with guard:
            lock = chat_locks.setdefault(cid, threading.Lock())
        with lock:
            try:
                handle_update(upd)
            except Exception:
                traceback.print_exc()

    offset = None
    log(f"{BRAND} bot started — {CREDIT}")
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            for upd in tg("getUpdates", **params) or []:
                offset = upd["update_id"] + 1
                pool.submit(run, upd)
        except Exception as e:
            log("poll error:", e)
            if "Conflict" in str(e):
                log("another instance is polling with the same bot token!")
            time.sleep(3)


if __name__ == "__main__":
    agent_main() if AGENT_MODE else main()
