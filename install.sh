#!/usr/bin/env bash
# MazuGate installer — telemt MTProxy + Telegram management bot (Ubuntu 22/24)
# By Mazu • Programmed by t.me/ozozz
#
#   main server :  sudo bash install.sh
#   extra server:  sudo bash install.sh --node MASTER_IP TOKEN PORT TLS_DOMAIN   (command comes from the bot)
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/33yyllcc/MazuGate/main}"
TELEMT_INSTALLER="https://raw.githubusercontent.com/telemt/telemt/main/install.sh"
BOT_DIR=/opt/telemtg
BOT_CONF_DIR=/etc/telemtg
BOT_CONF=$BOT_CONF_DIR/bot.json
TELEMT_CONF=/etc/telemt/telemt.toml
CREDIT="Programmed by t.me/ozozz"

# ------------------------------------------------------------- look & feel --
C='\033[1;36m'; G='\033[1;32m'; Y='\033[1;33m'; R='\033[1;31m'; M='\033[1;35m'; D='\033[2m'; B='\033[1m'; N='\033[0m'
MG_LANG="${MG_LANG:-}"
t()    { if [ "$MG_LANG" = "en" ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
ok()   { printf "  ${G}✔${N} %b\n" "$*"; }
warn() { printf "  ${Y}⚠${N} %b\n" "$*"; }
die()  { printf "\n  ${R}✘ %b${N}\n\n" "$*"; exit 1; }
STEP=0; STEPS=6
step() { STEP=$((STEP + 1)); printf "\n${M}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${N}\n${C}${B} [%d/%d]${N} ${B}%b${N}\n" "$STEP" "$STEPS" "$*"; }
ask() { # ask VAR "prompt" "default"
    local __v="$1" __p="$2" __d="${3:-}" __in=""
    if [ -n "${!__v:-}" ]; then return; fi
    if [ -n "$__d" ]; then printf "  ${C}➜${N} %b ${D}[%s]${N}: " "$__p" "$__d" > /dev/tty
    else printf "  ${C}➜${N} %b: " "$__p" > /dev/tty; fi
    read -r __in < /dev/tty || true
    printf -v "$__v" '%s' "${__in:-$__d}"
}
banner() {
    clear 2>/dev/null || true
    printf "${C}"
    cat <<'ART'

   __  __                  ____       _
  |  \/  | __ _ _____   _ / ___| __ _| |_ ___
  | |\/| |/ _` |_  / | | | |  _ / _` | __/ _ \
  | |  | | (_| |/ /| |_| | |_| | (_| | ||  __/
  |_|  |_|\__,_/___|\__,_|\____|\__,_|\__\___|

ART
    printf "${N}  ${B}Telegram Proxy Manager${N} ${D}•${N} ${M}by Mazu${N}\n  ${D}%s${N}\n" "$CREDIT"
}

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash install.sh"; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /tmp)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fetch() { # fetch REPO_FILE DEST
    if [ -f "$SCRIPT_DIR/$1" ]; then cp "$SCRIPT_DIR/$1" "$2"; else curl -fsSL "$REPO_RAW/$1" -o "$2"; fi
}

NODE_MODE=0
if [ "${1:-}" = "--node" ]; then
    NODE_MODE=1
    MASTER_IP="${2:-}"; NODE_TOKEN="${3:-}"; PORT="${4:-}"; TLS_DOMAIN="${5:-}"; AD_TAG=""
fi

# ---------------------------------------------------------------- language --
banner
if [ -z "$MG_LANG" ]; then
    printf "\n  ${B}🌐 اختار اللغة / Choose language${N}\n\n     ${C}1)${N} العربية\n     ${C}2)${N} English\n\n"
    LSEL=""; ask LSEL "1 / 2" "1"
    [ "$LSEL" = "2" ] && MG_LANG=en || MG_LANG=ar
fi
export MG_LANG

if [ "$NODE_MODE" -eq 1 ]; then
    [[ "$MASTER_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] && [[ "$NODE_TOKEN" =~ ^[0-9a-f]{32,64}$ ]] \
        && [[ "$PORT" =~ ^[0-9]+$ ]] && [ -n "$TLS_DOMAIN" ] \
        || die "$(t "أمر الإضافة ناقص. انسخه كامل من البوت (🖧 السيرفرات)" "Incomplete command. Copy it fully from the bot (🖧 Servers)")"
fi
. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = "ubuntu" ] || warn "$(t "مجرب على Ubuntu 24، النظام عندك:" "Tested on Ubuntu 24, you have:") ${PRETTY_NAME:-unknown}"

# ---------------------------------------------------------------- packages --
step "$(t "تجهيز الحزم الأساسية" "Installing base packages")"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq curl ca-certificates python3 openssl iproute2 iptables >/dev/null
ok "$(t "الحزم جاهزة" "Packages ready")"

# existing install: NEVER change the secret / port / domain (would break every link)
EXIST_SECRET=""; EXIST_PORT=""; EXIST_DOMAIN=""
if [ -f "$TELEMT_CONF" ]; then
    EXIST_SECRET="$(awk -F'"' '/^[ \t]*hello[ \t]*=/ {print $2; exit}' "$TELEMT_CONF" 2>/dev/null || true)"
    EXIST_PORT="$(awk -F'=' '/^[ \t]*port[ \t]*=/ {gsub(/[^0-9]/, "", $2); print $2; exit}' "$TELEMT_CONF" 2>/dev/null || true)"
    EXIST_DOMAIN="$(awk -F'"' '/^[ \t]*tls_domain[ \t]*=/ {print $2; exit}' "$TELEMT_CONF" 2>/dev/null || true)"
fi

# --------------------------------------------------------------- questions --
if [ "$NODE_MODE" -eq 0 ]; then
    step "$(t "الإعدادات" "Settings")"
    tg_api() { curl -fsS --max-time 60 "https://api.telegram.org/bot${BOT_TOKEN}/$1" "${@:2}"; }

    if [ -f "$BOT_CONF" ] && [ -z "${BOT_TOKEN:-}" ]; then
        KEEP=""; ask KEEP "$(t "لكيت إعدادات بوت قديمة. تبقيها؟ (y/n)" "Found an existing bot config. Keep it? (y/n)")" "y"
        if [ "$KEEP" = "y" ] || [ "$KEEP" = "Y" ]; then
            BOT_TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["bot_token"])' "$BOT_CONF")"
            ADMIN_ID="$(python3 -c 'import json,sys;c=json.load(open(sys.argv[1]));print(c.get("owner") or c["admins"][0])' "$BOT_CONF")"
        fi
    fi

    while :; do
        ask BOT_TOKEN "$(t "توكن البوت (من @BotFather)" "Bot token (from @BotFather)")"
        if BOT_INFO="$(tg_api getMe 2>/dev/null)"; then
            BOT_USER="$(printf '%s' "$BOT_INFO" | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["username"])')"
            ok "$(t "البوت:" "Bot:") @$BOT_USER"; break
        fi
        warn "$(t "التوكن غلط أو السيرفر ما يوصل لـ api.telegram.org" "Wrong token, or the server cannot reach api.telegram.org")"; BOT_TOKEN=""
    done

    if [ -z "${ADMIN_ID:-}" ]; then
        printf "\n  ${Y}👉${N} %s ${G}@%s${N} %s ${G}/start${N} %s\n" \
            "$(t "افتح البوت" "Open")" "$BOT_USER" "$(t "بالتلكرام ودوس" "in Telegram and press")" \
            "$(t "(أنتظر دقيقتين)..." "(waiting 2 minutes)...")" > /dev/tty
        tg_api deleteWebhook >/dev/null 2>&1 || true
        OFFSET=0
        for _ in $(seq 1 24); do
            RESP="$(tg_api getUpdates -d "offset=$OFFSET" -d timeout=5 2>/dev/null || echo '{}')"
            read -r ADMIN_ID OFFSET < <(printf '%s' "$RESP" | python3 -c '
import json,sys
d=json.load(sys.stdin); uid=""; off=int(sys.argv[1])
for u in d.get("result",[]):
    off=u["update_id"]+1
    m=u.get("message") or {}
    if m.get("chat",{}).get("type")=="private" and not uid: uid=str(m["from"]["id"])
print(uid or "-", off)' "$OFFSET")
            [ "$ADMIN_ID" != "-" ] && break
            ADMIN_ID=""
        done
        tg_api getUpdates -d "offset=$OFFSET" -d timeout=0 >/dev/null 2>&1 || true
        if [ -n "$ADMIN_ID" ]; then ok "$(t "لكيت حسابك، الـ ID:" "Found your account, ID:") $ADMIN_ID"
        else ask ADMIN_ID "$(t "ما وصلني /start. اكتب الـ ID مالتك (من @userinfobot)" "No /start received. Type your ID (from @userinfobot)")"; fi
    fi
    [[ "$ADMIN_ID" =~ ^[0-9,]+$ ]] || die "$(t "ID غلط:" "Invalid ID:") $ADMIN_ID"

    if [ -n "$EXIST_PORT" ] && [ -n "$EXIST_DOMAIN" ]; then
        PORT="$EXIST_PORT"; TLS_DOMAIN="$EXIST_DOMAIN"
        ok "$(t "البروكسي منصب من قبل: أبقي البورت والروابط مثل ما هي" "Proxy already installed: keeping the port and links") ($PORT)"
    else
        ask PORT "$(t "بورت البروكسي" "Proxy port")" "443"
        ask TLS_DOMAIN "$(t "دومين الرابط (اتركه افتراضي إذا ما تعرف)" "Link TLS domain (keep the default if unsure)")" "petrovich.ru"
    fi
    [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "$(t "بورت غلط" "Invalid port")"
    ask AD_TAG "$(t "تاك القناة الإعلانية من @MTProxybot (اختياري، Enter للتخطي)" "Sponsored-channel tag from @MTProxybot (optional, Enter to skip)")" ""
    AD_TAG="$(printf '%s' "$AD_TAG" | tr -d ' ' | tr 'A-F' 'a-f')"
    if [ -n "$AD_TAG" ] && ! [[ "$AD_TAG" =~ ^[0-9a-f]{32}$ ]]; then
        warn "$(t "التاك غلط، راح أتخطاه (اربطه من البوت)" "Invalid tag, skipped (link it from the bot)")"; AD_TAG=""
    fi
    TZ_OFFSET="${TZ_OFFSET:-3}"
else
    STEPS=5
    step "$(t "سيرفر إضافي مربوط بالسيرفر الرئيسي" "Extra server linked to the main server") ${MASTER_IP}"
    ok "$(t "البورت:" "Port:") $PORT"
fi

if ss -Hltn "sport = :$PORT" | grep -q . && ! ss -Hltnp "sport = :$PORT" | grep -q telemt; then
    ss -ltnp "sport = :$PORT"
    die "$(t "البورت $PORT مشغول ببرنامج ثاني. وكفه أو اختار بورت ثاني." "Port $PORT is used by another program. Stop it or choose another port.")"
fi

PUBLIC_IP="${PUBLIC_IP:-}"
for u in https://api.ipify.org https://ipv4.icanhazip.com https://ifconfig.me/ip; do
    [ -n "$PUBLIC_IP" ] && break
    PUBLIC_IP="$(curl -4 -fsS --max-time 8 "$u" 2>/dev/null | tr -d '[:space:]' || true)"
done
[ -n "$PUBLIC_IP" ] || ask PUBLIC_IP "$(t "ما كدرت أعرف IP السيرفر، اكتبه" "Could not detect the server IP, type it")"
ok "$(t "IP السيرفر:" "Server IP:") $PUBLIC_IP"

# ------------------------------------------------------------------ tuning --
step "$(t "تهيئة السيرفر للضغط العالي" "Tuning the server for high load")"
NCPU="$(nproc)"; MEM_MB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))
IFACE="$(awk '$2=="00000000"{print $1; exit}' /proc/net/route)"
ok "$(t "السيرفر:" "Server:") ${NCPU} CPU • ${MEM_MB}MB RAM • NIC ${IFACE:-?}"
grep -qw aes /proc/cpuinfo || warn "$(t "المعالج ما يدعم AES-NI، التشفير راح يكون أبطأ" "CPU lacks AES-NI, encryption will be slower")"
if [ "$NCPU" -lt 4 ] || [ "$MEM_MB" -lt 7500 ]; then
    warn "$(t "لـ 50 ألف متصل ينصح بـ 4 كور + 8GB رام + بورت 1Gbps أو أكثر." "For 50k concurrent users 4 cores + 8GB RAM + 1Gbps or more is recommended.")"
fi
fetch server-tune.sh "$TMP/server-tune.sh"
bash "$TMP/server-tune.sh" all

# ------------------------------------------------------------------ telemt --
step "$(t "تنصيب محرك البروكسي (telemt • Rust)" "Installing the proxy engine (telemt • Rust)")"
SECRET="${EXIST_SECRET:-$(openssl rand -hex 16)}"   # keep existing links working
curl -fsSL "$TELEMT_INSTALLER" -o "$TMP/telemt-install.sh"
TARGS=(-l en -p "$PORT" -d "$TLS_DOMAIN" -s "$SECRET")
[ -n "$AD_TAG" ] && TARGS+=(-a "$AD_TAG")
sh "$TMP/telemt-install.sh" "${TARGS[@]}" < /dev/null > "$TMP/telemt.log" 2>&1 \
    || { tail -30 "$TMP/telemt.log"; die "$(t "تنصيب telemt فشل" "telemt installation failed")"; }
[ -f "$TELEMT_CONF" ] || die "$(t "ما انشأ ملف الإعدادات" "Config file was not created:") $TELEMT_CONF"

# high-load telemt settings + local-only metrics (never touches secrets/port/domain)
fetch telemt-config.py "$TMP/telemt-config.py"
python3 "$TMP/telemt-config.py" "$TELEMT_CONF" >/dev/null
install -m 755 "$TMP/telemt-config.py" /usr/local/sbin/telemtg-telemt-config

mkdir -p /etc/systemd/system/telemt.service.d
cat > /etc/systemd/system/telemt.service.d/10-telemtg.conf <<EOF
[Service]
LimitNOFILE=1048576
LimitNPROC=65535
TasksMax=infinity
Environment=TOKIO_WORKER_THREADS=$NCPU
OOMScoreAdjust=-900
Nice=-5
Restart=always
RestartSec=3
EOF
systemctl daemon-reload
systemctl enable -q telemt
systemctl restart telemt
for _ in $(seq 1 60); do
    curl -fsS --max-time 2 http://127.0.0.1:9091/v1/health >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS --max-time 2 http://127.0.0.1:9091/v1/health >/dev/null 2>&1 \
    || { journalctl -u telemt -n 30 --no-pager; die "$(t "البروكسي ما اشتغل" "The proxy did not start")"; }
ok "$(t "البروكسي شغال" "Proxy is running")"

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT"/tcp >/dev/null && ok "$(t "فتحت البورت بالجدار الناري:" "Opened firewall port:") $PORT"
    if [ "$NODE_MODE" -eq 1 ]; then
        ufw allow from "$MASTER_IP" to any port 9443 proto tcp >/dev/null \
            && ok "$(t "بورت الربط 9443 مفتوح للسيرفر الرئيسي بس" "Link port 9443 open to the main server only")"
    fi
fi
mkdir -p "$BOT_DIR" "$BOT_CONF_DIR" /var/lib/telemtg
echo "$MG_LANG" > "$BOT_CONF_DIR/lang"

# -------------------------------------------------------------- extra node --
if [ "$NODE_MODE" -eq 1 ]; then
    step "$(t "تشغيل وكيل الربط (يستقبل أوامر من السيرفر الرئيسي بس)" "Starting the link agent (accepts the main server only)")"
    fetch bot/telemtg_bot.py "$BOT_DIR/telemtg_bot.py"; chmod 755 "$BOT_DIR/telemtg_bot.py"
    if [ ! -s "$BOT_CONF_DIR/agent.crt" ]; then
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
            -subj "/CN=mazugate-node" -keyout "$BOT_CONF_DIR/agent.key" -out "$BOT_CONF_DIR/agent.crt" 2>/dev/null
    fi
    chmod 600 "$BOT_CONF_DIR/agent.key"
    python3 - "$BOT_CONF_DIR/agent.json" "$NODE_TOKEN" "$MASTER_IP" "$PUBLIC_IP" "$PORT" "$BOT_CONF_DIR" <<'PY'
import json, os, sys
path, token, master, ip, port, d = sys.argv[1:]
cfg = {"token": token, "master_ips": [master], "agent_port": 9443,
       "cert": d + "/agent.crt", "key": d + "/agent.key",
       "public_ip": ip, "port": int(port),
       "api_url": "http://127.0.0.1:9091", "metrics_url": "http://127.0.0.1:9090/metrics",
       "telemt_config": "/etc/telemt/telemt.toml", "state_file": "/var/lib/telemtg/agent-state.json"}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: json.dump(cfg, f, indent=2)
PY
    cat > /etc/systemd/system/telemtg-agent.service <<EOF
[Unit]
Description=MazuGate node agent (managed by the main server bot)
After=network-online.target telemt.service
Wants=network-online.target

[Service]
Type=simple
Environment=TELEMTG_CONFIG=$BOT_CONF_DIR/agent.json
ExecStart=/usr/bin/python3 -u $BOT_DIR/telemtg_bot.py --agent
Restart=always
RestartSec=5
Nice=10
MemoryMax=150M

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable -q telemtg-agent
    systemctl restart telemtg-agent
    sleep 2
    systemctl is-active --quiet telemtg-agent \
        || { journalctl -u telemtg-agent -n 30 --no-pager; die "$(t "وكيل الربط ما اشتغل" "The agent did not start")"; }
    ok "$(t "وكيل الربط شغال" "Agent is running")"
    printf "\n${G}╔═══════════════════════════════════════════════════════╗${N}\n"
    printf "${G}║${N}  ${B}%-53s${N}${G}║${N}\n" "$(t "✅ تم تنصيب السيرفر الإضافي" "✅ Extra server installed")"
    printf "${G}╚═══════════════════════════════════════════════════════╝${N}\n"
    printf "  %s\n" "$(t "ارجع للبوت: خلال دقيقة يحس بالسيرفر وينقله الروابط والتاك ويبلغك." "Back to the bot: within a minute it detects this server, copies links and tag, and tells you.")"
    printf "  %s %s\n" "$(t "إذا ما وصلك شي، تأكد إن البورتات مفتوحة: 9443 و" "If nothing arrives, make sure these ports are open: 9443 and")" "$PORT"
    printf "\n  ${M}MazuGate${N} ${D}•${N} ${D}%s${N}\n\n" "$CREDIT"
    exit 0
fi

# --------------------------------------------------------------------- bot --
step "$(t "تشغيل بوت الإدارة" "Starting the management bot")"
fetch bot/telemtg_bot.py "$BOT_DIR/telemtg_bot.py"; chmod 755 "$BOT_DIR/telemtg_bot.py"

python3 - "$BOT_CONF" "$BOT_TOKEN" "$ADMIN_ID" "$PUBLIC_IP" "$PORT" "$TZ_OFFSET" "$MG_LANG" <<'PY'
import json, os, sys
path, token, admins, ip, port, tz, lang = sys.argv[1:]
cfg = {}
if os.path.exists(path):
    try: cfg = json.load(open(path))
    except Exception: cfg = {}
ids = [int(a) for a in admins.split(",") if a]
owner = ids[0]
old_admins = {int(a) for a in cfg.get("admins", [])}
if cfg.get("owner") and int(cfg["owner"]) != owner:
    old_admins.add(int(cfg["owner"]))  # previous owner stays as admin
cfg.update({
    "bot_token": token,
    "owner": owner,
    "admins": sorted((old_admins | set(ids[1:])) - {owner}),
    "public_ip": ip, "port": int(port),
    "timezone_offset_hours": float(tz),
    "api_url": "http://127.0.0.1:9091",
    "metrics_url": "http://127.0.0.1:9090/metrics",
    "telemt_config": "/etc/telemt/telemt.toml",
    "default_lang": lang,
})
cfg.setdefault("langs", {}).setdefault(str(owner), lang)
cfg.setdefault("report_hour", 0)
cfg.pop("repo_raw", None)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: json.dump(cfg, f, indent=2, ensure_ascii=False)
PY

cat > /etc/systemd/system/telemtg-bot.service <<EOF
[Unit]
Description=MazuGate Telegram management bot
After=network-online.target telemt.service
Wants=network-online.target

[Service]
Type=simple
Environment=TELEMTG_CONFIG=$BOT_CONF
ExecStart=/usr/bin/python3 -u $BOT_DIR/telemtg_bot.py
Restart=always
RestartSec=5
Nice=10
MemoryMax=150M

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q telemtg-bot
systemctl restart telemtg-bot
sleep 2
systemctl is-active --quiet telemtg-bot \
    || { journalctl -u telemtg-bot -n 30 --no-pager; die "$(t "البوت ما اشتغل" "The bot did not start")"; }
ok "$(t "البوت شغال" "Bot is running")"

# ----------------------------------------------------------------- summary --
printf "\n${G}╔═══════════════════════════════════════════════════════╗${N}\n"
printf "${G}║${N}  ${B}%-53s${N}${G}║${N}\n" "$(t "✅ تم تنصيب MazuGate بنجاح" "✅ MazuGate installed successfully")"
printf "${G}╚═══════════════════════════════════════════════════════╝${N}\n\n"
printf "  ${B}🔗 %s${N}\n" "$(t "روابط البروكسي:" "Proxy links:")"
curl -fsS http://127.0.0.1:9091/v1/users | python3 -c '
import json,sys
for u in json.load(sys.stdin).get("data",[]):
    for k in ("tls","secure","classic"):
        for l in (u.get("links") or {}).get(k) or []:
            print("     \033[1;36m"+u["username"]+"\033[0m  "+l.replace("tg://proxy?","https://t.me/proxy?",1))
' || true
printf "\n  ${B}🤖 %s${N} ${G}@%s${N}\n" "$(t "البوت:" "Bot:")" "$BOT_USER"
printf "     %s\n" "$(t "دزلك الروابط وشرح ربط القناة الإعلانية هسه." "has just sent you the links and the sponsored-channel guide.")"
printf "\n  ${D}%s${N}\n" "$(t "أوامر مفيدة:" "Useful commands:")"
printf "     ${D}systemctl status telemt${N}        %s\n" "$(t "حالة البروكسي" "proxy status")"
printf "     ${D}journalctl -u telemtg-bot -f${N}   %s\n" "$(t "سجل البوت" "bot log")"
printf "     ${D}sudo bash update.sh${N}            %s\n" "$(t "تحديث (ما يغير الروابط)" "update (keeps links)")"
printf "\n  ${M}MazuGate${N} ${D}•${N} ${M}by Mazu${N} ${D}•${N} ${D}%s${N}\n\n" "$CREDIT"
