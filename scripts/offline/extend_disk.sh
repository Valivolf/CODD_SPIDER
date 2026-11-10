#!/usr/bin/env bash
# Расширение диска сервера после того, как в ESXi увеличили размер виртуального диска.
# Сам по себе ESXi диск внутри системы не растягивает: нужно увеличить раздел, затем том LVM (если он есть) и файловую систему.
#
#     sudo bash extend_disk.sh            # ПРОВЕРКА: только показывает, что найдено и что будет сделано (ничего не меняет)
#     sudo bash extend_disk.sh --apply    # выполнить
#
# Данные при этом не затрагиваются (раздел растёт «вправо» в свободное место, файловая система — онлайн, без остановки).
# Работает для: обычный раздел или LVM; файловая система ext4 или xfs. Интернет не нужен: используются стандартные утилиты
# (growpart из cloud-guest-utils, а если его нет — parted). На всякий случай перед правкой сохраняется таблица разделов.
set -uo pipefail
APPLY=""
TARGET="${EXTEND_TARGET:-/}"      # какую файловую систему расширять (по умолчанию корень; другое значение — только для проверки скрипта)
[ "${1:-}" = "--apply" ] && APPLY=1
[ "$(id -u)" = "0" ] || { echo "Запустите с правами администратора: sudo bash extend_disk.sh"; exit 1; }

say() { echo "$*"; }
die() { echo "СТОП: $*"; exit 1; }
run() { if [ -n "$APPLY" ]; then echo "+ $*"; "$@"; else echo "  (будет выполнено) $*"; fi; }

ROOT_SRC="$(findmnt -no SOURCE "$TARGET" 2>/dev/null)"
FSTYPE="$(findmnt -no FSTYPE "$TARGET" 2>/dev/null)"
[ -n "$ROOT_SRC" ] || die "не удалось определить, где лежит корневая файловая система"
say "== Было =="
df -h "$TARGET" | sed 's/^/   /'
say "   корень: $ROOT_SRC ($FSTYPE)"
case "$FSTYPE" in ext2|ext3|ext4|xfs) ;; *) die "файловая система $FSTYPE не поддерживается этим скриптом — расширьте вручную";; esac

# --- какой раздел и какой диск стоят под корнем
PART=""; LV=""
if lvs "$ROOT_SRC" >/dev/null 2>&1; then
  LV="$ROOT_SRC"
  VG="$(lvs --noheadings -o vg_name "$ROOT_SRC" 2>/dev/null | tr -d ' ')"
  PVS="$(pvs --noheadings -o pv_name -S "vg_name=$VG" 2>/dev/null | tr -d ' ')"
  [ -n "$PVS" ] || die "не найден физический том для группы $VG"
  if [ "$(echo "$PVS" | wc -l)" != "1" ]; then die "в группе $VG несколько физических томов ($(echo $PVS)) — расширьте вручную"; fi
  PART="$PVS"
  say "   LVM: том $LV, группа $VG, физический том $PART"
else
  PART="$(readlink -f "$ROOT_SRC")"
  say "   обычный раздел $PART"
fi
[ -b "$PART" ] || die "$PART — не раздел диска"
DISK_NAME="$(lsblk -dno pkname "$PART" 2>/dev/null | head -1 | tr -d ' ')"
[ -n "$DISK_NAME" ] || die "$PART — не раздел (возможно, диск без таблицы разделов): расширьте вручную"
DISK="/dev/$DISK_NAME"
PARTNUM="$(cat "/sys/class/block/$(basename "$PART")/partition" 2>/dev/null)"
[ -n "$PARTNUM" ] || die "не удалось определить номер раздела $PART"
say "   диск $DISK, раздел №$PARTNUM"

# --- ESXi увеличил диск, а система об этом ещё может не знать: просим перечитать размер
if [ -w "/sys/class/block/$DISK_NAME/device/rescan" ]; then
  run bash -c "echo 1 > /sys/class/block/$DISK_NAME/device/rescan"
  [ -n "$APPLY" ] && sleep 2
fi
say ""
say "== Диск и разделы =="
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT "$DISK" | sed 's/^/   /'
DISK_BYTES="$(blockdev --getsize64 "$DISK")"
END_BYTES="$(( $(cat "/sys/class/block/$(basename "$PART")/start") * 512 + $(cat "/sys/class/block/$(basename "$PART")/size") * 512 ))"
FREE_BYTES=$(( DISK_BYTES - END_BYTES ))
say "   размер диска: $(( DISK_BYTES / 1024 / 1024 / 1024 )) ГБ, свободно после раздела: $(( FREE_BYTES / 1024 / 1024 )) МБ"
# Свободное место бывает в двух местах: за концом раздела (ESXi увеличил диск) и внутри группы LVM (Ubuntu при установке
# отдаёт тому только часть группы — остальное лежит свободным). Нужно хотя бы одно из них.
VG_FREE=0
if [ -n "$LV" ]; then VG_FREE="$(vgs --noheadings --units b --nosuffix -o vg_free "$VG" 2>/dev/null | tr -d ' ' | cut -d. -f1)"; VG_FREE="${VG_FREE:-0}"; say "   свободно в группе LVM: $(( VG_FREE / 1024 / 1024 )) МБ"; fi
GROW_PART=""
[ "$FREE_BYTES" -ge $(( 200 * 1024 * 1024 )) ] && GROW_PART=1     # 200 МБ запаса: GPT хранит копию таблицы в самом конце диска
if [ -z "$GROW_PART" ] && [ "$VG_FREE" -lt $(( 100 * 1024 * 1024 )) ]; then
  say ""
  say "Свободного места нет ни за концом раздела, ни внутри группы LVM: система ещё не видит увеличенный диск (или он не увеличен)."
  say "Проверьте в ESXi, что размер диска у этой виртуальной машины действительно больше, и что нет снимков (snapshot):"
  say "диск со снимками расширить нельзя. Затем запустите скрипт снова."
  exit 2
fi

# --- резервная копия таблицы разделов
BK="/root/partition-table-$DISK_NAME-$(date +%Y%m%d-%H%M%S).sfdisk"
if [ -n "$APPLY" ]; then sfdisk -d "$DISK" > "$BK" 2>/dev/null && say "таблица разделов сохранена: $BK (восстановить: sfdisk $DISK < $BK)"; fi

say ""
say "== Что делаю =="
# 1) раздел (если за его концом есть место)
if [ -n "$GROW_PART" ]; then
  if command -v growpart >/dev/null 2>&1; then
    if [ -n "$APPLY" ]; then echo "+ growpart $DISK $PARTNUM"; growpart "$DISK" "$PARTNUM"; rc=$?; [ $rc -le 1 ] || die "growpart завершился с ошибкой ($rc)"
    else echo "  (будет выполнено) growpart $DISK $PARTNUM"; growpart --dry-run "$DISK" "$PARTNUM" 2>&1 | grep -E "CHANGE|FAILED|ERROR" | sed 's/^/     /'; fi
  elif command -v parted >/dev/null 2>&1; then
    if [ -n "$APPLY" ]; then echo "+ parted -s $DISK resizepart $PARTNUM 100%"; parted -s "$DISK" resizepart "$PARTNUM" 100% || die "parted не смог расширить раздел"
    else echo "  (будет выполнено) parted -s $DISK resizepart $PARTNUM 100%"; fi
  else
    die "нет ни growpart, ни parted — расширьте раздел вручную (fdisk/cfdisk)"
  fi
  [ -n "$APPLY" ] && { partprobe "$DISK" 2>/dev/null || true; udevadm settle 2>/dev/null || true; sleep 1; }
else
  say "  раздел уже занимает весь диск — расширять не нужно"
fi

# 2) том LVM и файловая система
if [ -n "$LV" ]; then
  [ -n "$GROW_PART" ] && run pvresize "$PART"
  if [ -n "$APPLY" ]; then echo "+ lvextend -r -l +100%FREE $LV"; lvextend -r -l +100%FREE "$LV"; rc=$?; [ $rc -eq 0 ] || say "lvextend: код $rc (если написано «matches existing size» — места уже нет, это нормально)"
  else echo "  (будет выполнено) lvextend -r -l +100%FREE $LV   (-r растягивает и файловую систему)"; fi
else
  case "$FSTYPE" in
    xfs) run xfs_growfs "$TARGET" ;;
    *) run resize2fs "$PART" ;;
  esac
fi

say ""
if [ -n "$APPLY" ]; then
  say "== Стало =="
  df -h "$TARGET" | sed 's/^/   /'
  say "Готово. Приложение перезапускать не нужно."
else
  say "Это была ПРОВЕРКА — ничего не изменено. Чтобы выполнить: sudo bash extend_disk.sh --apply"
fi
