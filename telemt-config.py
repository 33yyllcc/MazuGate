#!/usr/bin/env python3
"""Applies MazuGate's high-load settings to telemt.toml (idempotent).

Never touches secrets, users, port, tls_domain or ad_tag.
Prints one word: UNCHANGED, RELOAD (hot-reloadable keys changed) or
RESTART (a key that telemt only reads at start changed).
"""
# MazuGate • Programmed by t.me/ozozz
import re
import sys

# (section, key, value, mode) — mode: None = enforce, "missing" = only add if absent
SETTINGS = [
    ("server", "metrics_listen", '"127.0.0.1:9090"', "no_metrics"),
    ("server", "max_connections", "0", None),          # default 10000 cuts users
    ("server", "listen_backlog", "65535", None),       # default 1024
    # Pressure mode: one full per-client queue switches the whole server into
    # short idle caps (30s/90s) that keep cutting idle Telegram connections.
    ("server.conntrack_control", "inline_conntrack_control", "false", None),
    ("server.conntrack_control", "mode", '"tracked"', None),
    ("general.telemetry", "user_enabled", "true", "missing"),
    # more parallel connections to Telegram's middle proxies (ad-tag path)
    ("general", "me_adaptive_floor_min_writers_single_endpoint", "2", None),
    ("general", "me_adaptive_floor_min_writers_multi_endpoint", "4", None),
    ("general", "me_adaptive_floor_max_extra_writers_single_per_core", "2", None),
    ("general", "me_adaptive_floor_max_extra_writers_multi_per_core", "4", None),
]
RESTART_KEYS = {"metrics_listen", "max_connections", "listen_backlog",
                "inline_conntrack_control", "mode"}


def main(path):
    with open(path, encoding="utf-8") as f:
        original = f.read()
    lines = original.splitlines()
    restart = False

    def header(sec):
        return re.compile(r'^\s*\[\s*' + re.escape(sec) + r'\s*\]\s*(#.*)?$')

    def toml_set(sec, key, val, only_if_missing):
        h = header(sec)
        start = next((i for i, l in enumerate(lines) if h.match(l)), None)
        if start is None:
            lines.extend(["", f"[{sec}]", f"{key} = {val}"])
            return True
        end = next((j for j in range(start + 1, len(lines)) if re.match(r'^\s*\[', lines[j])), len(lines))
        kre = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
        for j in range(start + 1, end):
            if kre.match(lines[j]):
                if only_if_missing or lines[j].strip() == f"{key} = {val}":
                    return False
                lines[j] = f"{key} = {val}"
                return True
        lines.insert(start + 1, f"{key} = {val}")
        return True

    has_metrics = any(re.match(r'\s*metrics_(listen|port)\s*=', l) for l in lines)
    for sec, key, val, mode in SETTINGS:
        if mode == "no_metrics" and has_metrics:
            continue
        if toml_set(sec, key, val, mode == "missing") and key in RESTART_KEYS:
            restart = True

    new = "\n".join(lines) + "\n"
    if new == original:
        print("UNCHANGED")
        return
    with open(path + ".telemtg.tmp", "w", encoding="utf-8") as f:
        f.write(new)
    import os
    st = os.stat(path)
    os.chown(path + ".telemtg.tmp", st.st_uid, st.st_gid)
    os.chmod(path + ".telemtg.tmp", st.st_mode & 0o777)
    os.replace(path + ".telemtg.tmp", path)
    print("RESTART" if restart else "RELOAD")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/etc/telemt/telemt.toml")
