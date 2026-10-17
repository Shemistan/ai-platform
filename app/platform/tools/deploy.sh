#!/bin/sh
# Деплой живого сервиса на тег релиза (REL-014): deploy <тег> | current.
# Зовут цели make deploy TAG=… / deployed. Тег ставит человек: make release-tag (DX-009,
# tools/release.py) в main после слияния PR и git push origin <тег>; агент main не трогает.
#
#   deploy  — живой checkout (AIP_LIVE_DIR, по умолчанию ~/apps/aiplatform):
#             git fetch --tags → make backup → git checkout <тег> → launchctl kickstart -k.
#             Откат — тот же deploy с прошлым тегом (README «Обновление и откат»).
#   current — какой тег сейчас у живого checkout.
#
# Переопределения (тесты, нестандартные машины): AIP_LIVE_DIR, AIP_AGENT_LABEL,
# AIP_RELEASE_REMOTE (origin; пусто — без fetch), AIP_DEPLOY_BACKUP (команда бэкапа;
# по умолчанию make -C <live>/app/platform backup), AIP_DEPLOY_NO_BACKUP=1 (деплой без
# бэкапа — только осознанно), AIP_LOG_DIR, LAUNCHCTL.
set -eu

REMOTE=${AIP_RELEASE_REMOTE-origin}
LIVE=${AIP_LIVE_DIR:-$HOME/apps/aiplatform}
LABEL=${AIP_AGENT_LABEL:-com.aiplatform.server}
LAUNCHCTL=${LAUNCHCTL:-launchctl}
LOG_DIR=${AIP_LOG_DIR:-$HOME/Library/Logs/aiplatform}

# Под git-хуком (pre-commit) GIT_DIR и GIT_INDEX_FILE указывают на чужой репозиторий.
unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE 2>/dev/null || true

die() {
	echo "⛔ $*" >&2
	exit 1
}

has_remote() { # $1 — каталог репозитория
	[ -n "$REMOTE" ] && git -C "$1" remote get-url "$REMOTE" >/dev/null 2>&1
}

cmd_current() {
	[ -d "$LIVE/.git" ] || [ -f "$LIVE/.git" ] || die "нет живого checkout $LIVE (см. README «Обновление и откат»)"
	git -C "$LIVE" describe --tags --always
}

cmd_deploy() {
	tag=${1:-}
	[ -d "$LIVE/.git" ] || [ -f "$LIVE/.git" ] || die "нет живого checkout $LIVE: git clone <репозиторий> $LIVE (README «Обновление и откат»)"
	if [ -z "$tag" ]; then
		echo "последние теги:" >&2
		git -C "$LIVE" tag -l 'v[0-9]*' --sort=-creatordate | head -5 >&2
		die "укажи тег: make deploy TAG=<тег>"
	fi
	if [ -n "$(git -C "$LIVE" status --porcelain --untracked-files=no)" ]; then
		die "в $LIVE правки в отслеживаемых файлах — живой checkout руками не правят"
	fi
	before=$(git -C "$LIVE" describe --tags --always)
	if has_remote "$LIVE"; then
		git -C "$LIVE" fetch -q --tags "$REMOTE"
	fi
	git -C "$LIVE" rev-parse -q --verify "refs/tags/$tag" >/dev/null || die "нет тега $tag в $LIVE"
	if [ "${AIP_DEPLOY_NO_BACKUP:-}" = "1" ]; then
		echo "⚠ деплой без бэкапа (AIP_DEPLOY_NO_BACKUP=1)"
	else
		echo "== бэкап перед деплоем"
		sh -c "${AIP_DEPLOY_BACKUP:-make -C \"$LIVE/app/platform\" backup}" || die "бэкап не удался — деплой остановлен"
	fi
	git -C "$LIVE" -c advice.detachedHead=false checkout -q --detach "refs/tags/$tag"
	"$LAUNCHCTL" kickstart -k "gui/$(id -u)/$LABEL"
	mkdir -p "$LOG_DIR"
	echo "$(date '+%Y-%m-%d %H:%M:%S') $before -> $tag" >>"$LOG_DIR/deploy.log"
	echo "живой сервис: $before → $tag; откат: make deploy TAG=$before"
}

case "${1:-}" in
deploy) cmd_deploy "${2:-}" ;;
current) cmd_current ;;
*)
	echo "использование: sh tools/deploy.sh deploy <тег> | current" >&2
	exit 2
	;;
esac
