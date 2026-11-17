#!/usr/bin/env bash
# MazuGate update: bot/agent code + kernel tuning + telemt settings.
# Never touches links (secrets), port, domain or the ad tag.
# By Mazu • Programmed by t.me/ozozz
set -euo pipefail
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/33yyllcc/MazuGate/main}"
TELEMT_CONF=/etc/telemt/telemt.toml
CREDIT="Programmed by t.me/ozozz"
C='\033[1;36m'; G='\033[1;32m'; Y='\033[1;33m'; R='\033[1;31m'; M='\033[1;35m'; D='\033[2m'; B='\033[1m'; N='\033[0m'
MG_LANG="${MG_LANG:-$(cat /etc/telemtg/lang 2>/dev/null || python3 -c 'import json;print(json.load(open("/etc/telemtg/bot.json")).get("default_lang","ar"))' 2>/dev/null || echo ar)}"
export MG_LANG
t()    { if [ "$MG_LANG" = "en" ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
step() { printf "\n${C}${B}▶ %b${N}\n" "$*"; }
ok()   { printf "  ${G}✔${N} %b\n" "$*"; }
warn() { printf "  ${Y}⚠${N} %b\n" "$*"; }
die()  { printf "\n  ${R}✘ %b${N}\n\n" "$*"; exit 1; }

printf "\n${C}${B}  MazuGate${N} ${D}•${N} %s ${D}•${N} ${M}by Mazu${N}\n  ${D}%s${N}\n" "$(t "تحديث" "Update")" "$CREDIT"
[ "$(id -u)" -eq 0 ] || die "$(t "شغله كـ root:  sudo bash update.sh" "Run as root: sudo bash update.sh")"
[ -f "$TELEMT_CONF" ] || die "$(t "ما لكيت $TELEMT_CONF — نصب أول بـ install.sh" "$TELEMT_CONF not found — install first with install.sh")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
get() { curl -fsSL "$REPO_RAW/$1?$(date +%s)" -o "$2"; }

step "$(t "تنزيل آخر نسخة" "Downloading the latest version")"
get bot/telemtg_bot.py "$TMP/telemtg_bot.py"
get server-tune.sh "$TMP/server-tune.sh"
get telemt-config.py "$TMP/telemt-config.py"
python3 -m py_compile "$TMP/telemtg_bot.py" "$TMP/telemt-config.py" \
    || die "$(t "الملفات اللي نزلت خربانة، جرب بعد دقيقة" "Downloaded files are broken, retry in a minute")"
bash -n "$TMP/server-tune.sh" || die "$(t "server-tune.sh خربان، جرب بعد دقيقة" "server-tune.sh is broken, retry in a minute")"
ok "$(t "تم" "Done")"

step "$(t "تهيئة السيرفر (الكيرنل، كارت الشبكة، الحدود)" "Server tuning (kernel, network card, limits)")"
bash "$TMP/server-tune.sh" all

step "$(t "إعدادات البروكسي" "Proxy settings")"
cp -p "$TELEMT_CONF" "$TELEMT_CONF.bak.$(date +%Y%m%d%H%M%S)"
{ ls -1t "$TELEMT_CONF".bak.* 2>/dev/null | tail -n +4 | xargs -r rm -f; } || true   # keep the 3 newest
install -m 755 "$TMP/telemt-config.py" /usr/local/sbin/telemtg-telemt-config
RESULT="$(python3 "$TMP/telemt-config.py" "$TELEMT_CONF")"

# telemt service limits (servers installed by older versions may miss some)
DROPIN=/etc/systemd/system/telemt.service.d/10-telemtg.conf
mkdir -p "$(dirname "$DROPIN")"
cat > "$TMP/dropin" <<EOF
[Service]
LimitNOFILE=1048576
LimitNPROC=65535
TasksMax=infinity
Environment=TOKIO_WORKER_THREADS=$(nproc)
OOMScoreAdjust=-900
Nice=-5
Restart=always
RestartSec=3
EOF
if ! cmp -s "$TMP/dropin" "$DROPIN"; then
    install -m 644 "$TMP/dropin" "$DROPIN"
    systemctl daemon-reload
    RESULT=RESTART
fi

case "$RESULT" in
    RESTART)
        warn "$(t "تغيرت إعدادات يقراها البروكسي بس وكت التشغيل، فلازم أعيد تشغيله." "Settings changed that the proxy only reads at start, restarting it.")"
        warn "$(t "المتصلين ينقطعون ثواني ويرجعون وحدهم، والروابط ما تتغير." "Users drop for a few seconds and reconnect; links do not change.")"
        systemctl restart telemt
        for _ in $(seq 1 60); do
            curl -fsS --max-time 2 http://127.0.0.1:9091/v1/health >/dev/null 2>&1 && break
            sleep 1
        done
        curl -fsS --max-time 2 http://127.0.0.1:9091/v1/health >/dev/null 2>&1 \
            && ok "$(t "البروكسي رجع يشتغل" "Proxy is running again")" \
            || { journalctl -u telemt -n 30 --no-pager; die "$(t "البروكسي ما رجع! النسخة القديمة من الإعدادات بـ" "The proxy did not come back! Previous config saved as") $TELEMT_CONF.bak.*"; }
        ;;
    RELOAD) systemctl kill -s HUP telemt 2>/dev/null || true; ok "$(t "تطبقت بدون إعادة تشغيل" "Applied without a restart")" ;;
    *) ok "$(t "الإعدادات محدثة من قبل" "Settings already up to date")" ;;
esac

step "$(t "البوت / الوكيل" "Bot / agent")"
for svc in telemtg-bot telemtg-agent; do
    if [ -f "/etc/systemd/system/$svc.service" ]; then
        install -D -m 755 "$TMP/telemtg_bot.py" /opt/telemtg/telemtg_bot.py
        sed -i 's/^Description=Telemtg /Description=MazuGate /' "/etc/systemd/system/$svc.service"
        systemctl daemon-reload
        systemctl restart "$svc"
        ok "$(t "تحدث" "Updated") $svc"
    fi
done

printf "\n${G}${B}  ✅ %s${N}\n  %s\n\n  ${M}MazuGate${N} ${D}•${N} ${D}%s${N}\n\n" \
    "$(t "تم التحديث" "Update complete")" "$(t "شوف 🩺 تشخيص بالبوت بعد 5 دقايق." "Check 🩺 Diagnostics in the bot in 5 minutes.")" "$CREDIT"
