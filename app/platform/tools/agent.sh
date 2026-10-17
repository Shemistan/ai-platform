#!/bin/sh
# Автозапуск платформы через launchd: install | uninstall | status.
# Зовут цели make install-agent / uninstall-agent / agent-status (они же
# экспортируют AIP_* из local.mk). Ставит два LaunchAgent в ~/Library/LaunchAgents:
#   com.aiplatform.server       — сервис (make run), поднимается при входе и после падения;
#   com.aiplatform.digest-check — внешний чекер дайджеста раз в день (tools/digest_check.py).
# Переопределения (для тестов и нестандартных машин): AIP_AGENTS_DIR, AIP_LOG_DIR,
# AIP_AGENT_LABEL, AIP_CHECK_LABEL, AIP_CHECK_AT=ЧЧ:ММ, LAUNCHCTL.
set -eu

cd "$(dirname "$0")/.."
PLATFORM_DIR=$(pwd -P)
LABEL=${AIP_AGENT_LABEL:-com.aiplatform.server}
CHECK_LABEL=${AIP_CHECK_LABEL:-com.aiplatform.digest-check}
AGENTS=${AIP_AGENTS_DIR:-$HOME/Library/LaunchAgents}
LOG_DIR=${AIP_LOG_DIR:-$HOME/Library/Logs/aiplatform}
LAUNCHCTL=${LAUNCHCTL:-launchctl}
DOMAIN="gui/$(id -u)"
PORT=${AIP_PORT:-8780}
CHECK_URL="http://127.0.0.1:$PORT"
GRACE_MIN=30
PYTHON=$(command -v "${PYTHON:-python3}") || { echo "не найден ${PYTHON:-python3}" >&2; exit 1; }
# Настоящий интерпретатор, а не шим pyenv/asdf: без .zshrc шим может не найти версию.
PYTHON=$("$PYTHON" -c 'import sys; print(sys.executable)')

# launchd не читает .zshrc: собираем PATH из каталогов нужных команд и стандартных мест.
agent_path() {
	p="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"
	for cmd in ffmpeg uv claude "$PYTHON"; do
		found=$(command -v "$cmd" 2>/dev/null) || continue
		p="$p:$(dirname "$found")"
	done
	printf '%s' "$p:/usr/bin:/bin:/usr/sbin:/sbin" | awk -v RS=: -v ORS=: '!seen[$0]++' | sed 's/:$//'
}

# Время чекера: AIP_CHECK_AT или время дайджеста из настроек + 30 мин; не вышло — 09:00.
check_at() {
	if [ -n "${AIP_CHECK_AT:-}" ]; then
		echo "$AIP_CHECK_AT"
		return
	fi
	"$PYTHON" - "$GRACE_MIN" <<'EOF' 2>/dev/null || echo "09:00"
import sys
from datetime import datetime, timedelta
from aiplatform import config
plan = config.load().settings.load()["schedule"]["digest"]
at = datetime.strptime(plan["time"], "%H:%M") + timedelta(minutes=int(sys.argv[1]))
print(at.strftime("%H:%M"))
EOF
}

render() { # шаблон метка → plist
	sed -e "s|@LABEL@|$2|g" \
		-e "s|@PLATFORM_DIR@|$PLATFORM_DIR|g" \
		-e "s|@PYTHON@|$PYTHON|g" \
		-e "s|@PATH@|$AGENT_PATH|g" \
		-e "s|@LOG_DIR@|$LOG_DIR|g" \
		-e "s|@CHECK_URL@|$CHECK_URL|g" \
		-e "s|@HOUR@|$HOUR|g" \
		-e "s|@MINUTE@|$MINUTE|g" \
		"tools/$1" >"$AGENTS/$2.plist"
	if command -v plutil >/dev/null 2>&1; then
		plutil -lint -s "$AGENTS/$2.plist"
	fi
}

load() { # перезагрузить агент: старый — выгрузить, новый — загрузить
	"$LAUNCHCTL" bootout "$DOMAIN/$1" 2>/dev/null || true
	"$LAUNCHCTL" bootstrap "$DOMAIN" "$AGENTS/$1.plist"
}

unload() {
	"$LAUNCHCTL" bootout "$DOMAIN/$1" 2>/dev/null || true
	rm -f "$AGENTS/$1.plist"
}

case "${1:-}" in
install)
	mkdir -p "$AGENTS" "$LOG_DIR"
	AGENT_PATH=$(agent_path)
	at=$(check_at)
	HOUR=$(echo "$at" | cut -d: -f1 | sed 's/^0\(.\)/\1/')
	MINUTE=$(echo "$at" | cut -d: -f2 | sed 's/^0\(.\)/\1/')
	render aiplatform.plist.in "$LABEL"
	render digest-check.plist.in "$CHECK_LABEL"
	load "$LABEL"
	load "$CHECK_LABEL"
	echo "агент $LABEL запущен: $CHECK_URL, логи — $LOG_DIR/"
	echo "чекер $CHECK_LABEL — ежедневно в $at (токен и чат — в Keychain, см. README «Автозапуск»)"
	;;
uninstall)
	unload "$LABEL"
	unload "$CHECK_LABEL"
	echo "агенты $LABEL и $CHECK_LABEL удалены"
	;;
status)
	for label in "$LABEL" "$CHECK_LABEL"; do
		if [ -f "$AGENTS/$label.plist" ]; then
			echo "== $label"
			"$LAUNCHCTL" print "$DOMAIN/$label" 2>/dev/null | grep -E '^\s*(state|pid|last exit code|runs) =' || echo "  не загружен"
		else
			echo "== $label: не установлен (make install-agent)"
		fi
	done
	echo "== $CHECK_URL/api/health"
	curl -s -m 5 "$CHECK_URL/api/health" || echo "  не отвечает"
	echo
	;;
*)
	echo "использование: sh tools/agent.sh install|uninstall|status" >&2
	exit 2
	;;
esac
