#!/usr/bin/env bash
# MazuGate uninstaller. Removes the bot/agent and the server tuning.
# Pass "all" to also remove the telemt proxy itself.
# By Mazu • Programmed by t.me/ozozz
set -eu
G='\033[1;32m'; M='\033[1;35m'; D='\033[2m'; N='\033[0m'
MG_LANG="${MG_LANG:-$(cat /etc/telemtg/lang 2>/dev/null || echo ar)}"
t() { if [ "$MG_LANG" = "en" ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash uninstall.sh"; exit 1; }

systemctl disable --now telemtg-bot telemtg-agent telemtg-tune 2>/dev/null || true
rm -f /etc/systemd/system/telemtg-bot.service /etc/systemd/system/telemtg-agent.service /etc/systemd/system/telemtg-tune.service
rm -rf /opt/telemtg /etc/telemtg /var/lib/telemtg
rm -f /usr/local/sbin/telemtg-tune /usr/local/sbin/telemtg-telemt-config \
      /etc/systemd/system/telemt.service.d/10-telemtg.conf \
      /etc/sysctl.d/99-telemtg.conf \
      /etc/security/limits.d/99-telemtg.conf \
      /etc/systemd/system.conf.d/99-telemtg.conf \
      /etc/systemd/journald.conf.d/99-telemtg.conf \
      /etc/modules-load.d/telemtg.conf \
      /etc/modprobe.d/telemtg-conntrack.conf
systemctl daemon-reload
if [ "${1:-}" = "all" ]; then
    curl -fsSL https://raw.githubusercontent.com/telemt/telemt/main/install.sh | sh -s -- -l en purge
fi
printf "\n  ${G}✔${N} %s\n  ${D}%s${N}\n\n  ${M}MazuGate${N} ${D}• Programmed by t.me/ozozz${N}\n\n" \
    "$(t "تم الحذف" "Removed")" "$(t "سوي ريستارت حتى ترجع إعدادات الكيرنل الافتراضية بالكامل." "Reboot to fully restore the default kernel settings.")"
