#!/usr/bin/env bash
# UGV Quick Setup: WireGuard-сервер для НРК і пілотів.
#
# Програма на ноутбуці дає одну команду, яка запускає цей скрипт. Він:
#   - ставить WireGuard, якщо його ще немає (якщо вже є — лише використовує);
#   - додає пілота й НРК (той самий пілот / НРК лишається на своїй адресі);
#   - дає доступ до НРК лише його пілоту: НРК належить пілоту, з яким його додали або якому передали;
#   - виводить рядок FLU1:...:END, який треба вставити в програму на ноутбуці.
# Запускати повторно безпечно.
#
# Ім'я пілота й номер НРК закріплені за ключем: якщо інший ноутбук прийде з тим самим ім'ям,
# сервер нічого не замінить, а відмовить. Так два ноутбуки не витіснять один одного зі спільного сервера.
#
# Аргументи:
#   <ім'я пілота> <ключ пілота> [<номер НРК> <ключ НРК>]   додати пілота (і НРК)
#   --give <номер НРК> <ім'я пілота>                        передати НРК іншому пілоту
#   --remove-pilot <ім'я пілота>                            видалити пілота (його НРК лишаються без пілота)
#   --remove-robot <номер НРК>                              видалити НРК, звільнити слот
#   --no-give перед будь-якими з них: НРК, що вже належить іншому пілоту, не передавати
#             (у програмі, де передача ще вимкнена)
# Для тестів: FLU_TEST=1, FLU_WG_DIR, FLU_ENDPOINT_HOST.
set -euo pipefail

if [ "$(id -u)" != 0 ] && [ -z "${FLU_TEST:-}" ]; then
  exec sudo bash "$0" "$@"
fi

say() { printf 'UGV Quick Setup: %s\n' "$*" >&2; }
die() { say "ПОМИЛКА: $*"; exit 1; }

MODE=add PILOT_NAME="" PILOT_KEY="" ROBOT_SERIAL="" ROBOT_KEY="" NO_GIVE=""
[ "${1:-}" != --no-give ] || { NO_GIVE=1; shift; }
case "${1:-}" in
  --give) MODE=give ROBOT_SERIAL=${2:-} PILOT_NAME=${3:-} ;;
  --remove-pilot) MODE=remove-pilot PILOT_NAME=${2:-} ;;
  --remove-robot) MODE=remove-robot ROBOT_SERIAL=${2:-} ;;
  *) PILOT_NAME=${1:-} PILOT_KEY=${2:-} ROBOT_SERIAL=${3:-} ROBOT_KEY=${4:-} ;;
esac
KEY_RE='^[A-Za-z0-9+/]{43}=$'
NAME_RE='^[a-z0-9][a-z0-9-]{0,19}$'
SERIAL_RE='^[A-Za-z0-9._-]{1,40}$'
case $MODE in
  add)
    [[ $PILOT_NAME =~ $NAME_RE ]] || die "некоректне ім'я пілота: $PILOT_NAME"
    [[ $PILOT_KEY =~ $KEY_RE ]] || die "некоректний ключ пілота"
    if [ -n "$ROBOT_SERIAL$ROBOT_KEY" ]; then
      [[ $ROBOT_SERIAL =~ $SERIAL_RE ]] || die "некоректний номер НРК: $ROBOT_SERIAL"
      [[ $ROBOT_KEY =~ $KEY_RE ]] || die "некоректний ключ НРК"
    fi ;;
  give)
    [[ $ROBOT_SERIAL =~ $SERIAL_RE ]] || die "некоректний номер НРК: $ROBOT_SERIAL"
    [[ $PILOT_NAME =~ $NAME_RE ]] || die "некоректне ім'я пілота: $PILOT_NAME" ;;
  remove-pilot) [[ $PILOT_NAME =~ $NAME_RE ]] || die "некоректне ім'я пілота: $PILOT_NAME" ;;
  remove-robot) [[ $ROBOT_SERIAL =~ $SERIAL_RE ]] || die "некоректний номер НРК: $ROBOT_SERIAL" ;;
esac

DIR=${FLU_WG_DIR:-/etc/wireguard}
IFACE=flu
CONF=$DIR/$IFACE.conf
KEYFILE=$DIR/$IFACE.key
DB=$DIR/flu-peers.tsv   # тип  номер  ім'я  ключ  [пілот-власник — для НРК]
ACL=$DIR/$IFACE-acl.sh  # правила iptables: який пілот до якого НРК
TUNNEL=10.150.0         # .1 — сервер, НРК зі слотом N — .(1+N)
PILOTS=10.150.1         # пілот з номером K — .K
LANS3=16                # мережі НРК: 10.150.16.0/20, по /28 на НРК
MAX=250
MTU=1280                # як на роутерах НРК і в тунелях пілотів (fleet/render.py)
MAV_PORT=5760           # ФК і камери в блоці НРК — як у fleet/addressing.py (MAPPED) і base/platforms.yaml
FC_HOST=2 CAM1_HOST=8 CAM2_HOST=9

# Службові команди міняють лише наявний сервер: без бази й ключа сервера їм нема з чим працювати
if [ "$MODE" != add ] && { [ ! -s "$DB" ] || [ ! -s "$KEYFILE" ] || ! command -v wg >/dev/null 2>&1; }; then
  die "на цьому сервері ще не налаштовано тунелі НРК: спершу виконайте команду з кроку «Сервер» у програмі"
fi

# --- 1. WireGuard ----------------------------------------------------------------
if [ "$MODE" = add ]; then
  if command -v wg >/dev/null 2>&1; then
    say "WireGuard уже встановлено"
  else
    say "встановлюю WireGuard..."
    if command -v apt-get >/dev/null 2>&1; then
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq >/dev/null
      apt-get install -y -qq wireguard-tools iptables >/dev/null
    elif command -v dnf >/dev/null 2>&1; then
      dnf install -y -q wireguard-tools iptables >/dev/null
    elif command -v yum >/dev/null 2>&1; then
      yum install -y -q wireguard-tools iptables >/dev/null
    else
      die "не знаю, як поставити WireGuard на цю систему (потрібна Ubuntu, Debian або RHEL)"
    fi
  fi
fi

# --- 2. Ключ і порт сервера ------------------------------------------------------
mkdir -p "$DIR"
umask 077
[ -s "$KEYFILE" ] || wg genkey > "$KEYFILE"
SERVER_PUB=$(wg pubkey < "$KEYFILE")
touch "$DB"
PORT=$(sed -n 's/^ListenPort *= *//p' "$CONF" 2>/dev/null | head -n 1 || true)
if [ -z "$PORT" ]; then
  # 51820 може бути зайнятий іншим WireGuard на цьому сервері — тоді беремо наступний
  PORT=51820
  used=" $(wg show all listen-port 2>/dev/null | awk '{print $2}' | tr '\n' ' ' || true) "
  while [[ $used == *" $PORT "* ]]; do PORT=$((PORT + 1)); done
fi

# --- 3. Пілот і НРК --------------------------------------------------------------
# Колонка запису за типом та ім'ям: 2 — номер, 4 — ключ, 5 — пілот-власник НРК
field_of() { awk -F'\t' -v t="$1" -v n="$2" -v c="$3" '$1 == t && $3 == n { print $c; exit }' "$DB"; }

# Ім'я з іншим ключем — це інший ноутбук: нічого не замінюємо, відмовляємо ще до змін у базі
check_owner() {
  local taken
  taken=$(field_of "$1" "$2" 4)
  [ -z "$taken" ] || [ "$taken" = "$3" ] && return 0
  if [ "$1" = pilot ]; then
    die "ім'я пілота $2 на цьому сервері вже зайняте іншим ноутбуком. Виберіть інше ім'я (наприклад, $2-2). Якщо це той самий пілот на новому ноутбуці — спершу видаліть старого («Видалити пілота» в програмі) і передайте йому НРК"
  fi
  die "НРК з номером $2 уже зареєстровано на цьому сервері з іншого ноутбука. Якщо це інший НРК — дайте йому інший номер. Якщо цей НРК перепрошивають з новими ключами — спершу видаліть його з сервера («Видалити НРК» у програмі)"
}

# Номер шукаємо за ім'ям, потім за ключем; не знайшли — беремо перший вільний
alloc() {
  local type=$1 name=$2 key=$3 first=$4 owner=${5:-} num old
  num=$(field_of "$type" "$name" 2)
  [ -n "$num" ] || num=$(awk -F'\t' -v t="$type" -v k="$key" '$1 == t && $4 == k { print $2; exit }' "$DB")
  if [ -z "$num" ]; then
    num=$first
    while awk -F'\t' -v t="$type" -v n="$num" '$1 == t && $2 == n { f = 1 } END { exit !f }' "$DB"; do
      num=$((num + 1))
    done
    [ "$num" -le "$MAX" ] || die "на сервері немає вільних місць"
  fi
  # Той самий ключ під новим ім'ям — перейменування: НРК пілота переходять разом з ним
  old=$(awk -F'\t' -v t="$type" -v n="$num" '$1 == t && $2 == n { print $3; exit }' "$DB")
  awk -F'\t' -v OFS='\t' -v t="$type" -v n="$num" -v k="$key" -v old="$old" -v new="$name" '
    $1 == t && ($2 == n || $4 == k) { next }
    t == "pilot" && old != "" && $1 == "robot" && $5 == old { $5 = new }
    { print }' "$DB" > "$DB.tmp"
  printf '%s\t%s\t%s\t%s\t%s\n' "$type" "$num" "$name" "$key" "$owner" >> "$DB.tmp"
  mv "$DB.tmp" "$DB"
  echo "$num"
}

# Записати базу заново, змінивши її awk-програмою
rewrite() { awk -F'\t' -v OFS='\t' "$@" "$DB" > "$DB.tmp" && mv "$DB.tmp" "$DB"; }

PILOT_ID="" ROBOT_SLOT="" PREV_OWNER="" OWNED=""
case $MODE in
  add)
    check_owner pilot "$PILOT_NAME" "$PILOT_KEY"
    [ -z "$ROBOT_KEY" ] || check_owner robot "$ROBOT_SERIAL" "$ROBOT_KEY"
    if [ -n "$ROBOT_KEY" ] && [ -n "$NO_GIVE" ]; then
      PREV_OWNER=$(field_of robot "$ROBOT_SERIAL" 5)
      [ -z "$PREV_OWNER" ] || [ "$PREV_OWNER" = "$PILOT_NAME" ] ||
        die "НРК $ROBOT_SERIAL уже належить пілоту $PREV_OWNER, а передача НРК у цій версії програми вимкнена"
    fi
    PILOT_ID=$(alloc pilot "$PILOT_NAME" "$PILOT_KEY" 10)
    if [ -n "$ROBOT_KEY" ]; then
      PREV_OWNER=$(field_of robot "$ROBOT_SERIAL" 5)
      ROBOT_SLOT=$(alloc robot "$ROBOT_SERIAL" "$ROBOT_KEY" 1 "$PILOT_NAME")
    fi ;;
  give)
    ROBOT_SLOT=$(field_of robot "$ROBOT_SERIAL" 2)
    [ -n "$ROBOT_SLOT" ] || die "НРК $ROBOT_SERIAL на цьому сервері немає"
    [ -n "$(field_of pilot "$PILOT_NAME" 2)" ] || die "пілота $PILOT_NAME на цьому сервері немає: спершу додайте його з його ноутбука (крок «Сервер» у програмі)"
    PREV_OWNER=$(field_of robot "$ROBOT_SERIAL" 5)
    rewrite -v s="$ROBOT_SERIAL" -v p="$PILOT_NAME" '$1 == "robot" && $3 == s { $5 = p } { print }' ;;
  remove-pilot)
    [ -n "$(field_of pilot "$PILOT_NAME" 2)" ] || die "пілота $PILOT_NAME на цьому сервері немає"
    OWNED=$(awk -F'\t' -v p="$PILOT_NAME" '$1 == "robot" && $5 == p { printf " %s", $3 }' "$DB")
    rewrite -v p="$PILOT_NAME" '$1 == "pilot" && $3 == p { next } $1 == "robot" && $5 == p { $5 = "" } { print }' ;;
  remove-robot)
    ROBOT_SLOT=$(field_of robot "$ROBOT_SERIAL" 2)
    [ -n "$ROBOT_SLOT" ] || die "НРК $ROBOT_SERIAL на цьому сервері немає"
    rewrite -v s="$ROBOT_SERIAL" '!($1 == "robot" && $3 == s)' ;;
esac

# --- 4. Конфіг WireGuard збирається заново з бази -----------------------------------
lan_of() { local o=$((($1 - 1) * 16)); echo "10.150.$((LANS3 + o / 256)).$((o % 256))/28"; }
host_of() { local b; b=$(lan_of "$1"); b=${b%/28}; echo "${b%.*}.$((${b##*.} + $2))"; }
{
  echo "# FLU: generated by flu-hub.sh - do not edit, run the command from the laptop again"
  echo "[Interface]"
  echo "Address = $TUNNEL.1/24"
  echo "ListenPort = $PORT"
  echo "PrivateKey = $(cat "$KEYFILE")"
  echo "MTU = $MTU"
  echo "# Each pilot may reach only own UGVs ($IFACE-acl.sh); UGVs only answer; nothing else is forwarded"
  echo "PostUp = sysctl -q -w net.ipv4.ip_forward=1"
  echo "PostUp = iptables -I FORWARD -i %i -j DROP"
  echo "PostUp = iptables -I FORWARD -i %i -o %i -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
  echo "PostUp = sh $ACL"
  echo "PostDown = iptables -D FORWARD -i %i -o %i -j flu-acl"
  echo "PostDown = iptables -F flu-acl"
  echo "PostDown = iptables -X flu-acl"
  echo "PostDown = iptables -D FORWARD -i %i -o %i -j flu-tel; iptables -F flu-tel; iptables -X flu-tel"
  echo "PostDown = iptables -t nat -D PREROUTING -i %i -j flu-tel; iptables -t nat -F flu-tel; iptables -t nat -X flu-tel"
  echo "PostDown = iptables -D FORWARD -i %i -o %i -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
  echo "PostDown = iptables -D FORWARD -i %i -j DROP"
  sort -t "$(printf '\t')" -k1,1 -k2,2n "$DB" | while IFS=$'\t' read -r type num name key owner; do
    echo
    if [ "$type" = robot ]; then
      echo "# UGV $name (slot $num)"
      echo "[Peer]"
      echo "PublicKey = $key"
      echo "AllowedIPs = $TUNNEL.$((1 + num))/32, $(lan_of "$num")"
    else
      echo "# pilot $name"
      echo "[Peer]"
      echo "PublicKey = $key"
      echo "AllowedIPs = $PILOTS.$num/32"
    fi
  done
} > "$CONF.tmp"
mv "$CONF.tmp" "$CONF"
chmod 600 "$CONF"

# Доступ пілотів до НРК: окремий ланцюжок flu-acl, його можна перебудувати без розриву тунелів.
# НРК без власника (додані до появи цього правила або після видалення пілота) не бачить ніхто.
{
  echo "#!/bin/sh"
  echo "# FLU: generated by flu-hub.sh - do not edit. Pilot -> own UGV networks only."
  echo "iptables -N flu-acl 2>/dev/null || iptables -F flu-acl"
  # flu-tel: телеметрія, яку адаптер ФК шле сам по UDP. Роутер НРК відправляє її на $TUNNEL.1,
  # сервер — пілоту-власнику; відповіді (керування) йдуть назад тим самим з'єднанням
  echo "iptables -N flu-tel 2>/dev/null || iptables -F flu-tel"
  echo "iptables -t nat -N flu-tel 2>/dev/null || iptables -t nat -F flu-tel"
  sort -t "$(printf '\t')" -k1,1 -k2,2n "$DB" | while IFS=$'\t' read -r type num name key owner; do
    [ "$type" = robot ] && [ -n "$owner" ] || continue
    pid=$(field_of pilot "$owner" 2)
    [ -n "$pid" ] || continue
    echo "iptables -A flu-acl -s $PILOTS.$pid/32 -d $(lan_of "$num") -j ACCEPT  # $owner -> UGV $name"
    echo "iptables -t nat -A flu-tel -s $(lan_of "$num") -d $TUNNEL.1 -p udp -j DNAT --to-destination $PILOTS.$pid  # UGV $name -> $owner"
    echo "iptables -A flu-tel -s $(lan_of "$num") -d $PILOTS.$pid/32 -p udp -j ACCEPT  # UGV $name -> $owner"
  done
  echo "iptables -C FORWARD -i $IFACE -o $IFACE -j flu-acl 2>/dev/null || iptables -I FORWARD -i $IFACE -o $IFACE -j flu-acl"
  echo "iptables -C FORWARD -i $IFACE -o $IFACE -j flu-tel 2>/dev/null || iptables -I FORWARD -i $IFACE -o $IFACE -j flu-tel"
  echo "iptables -t nat -C PREROUTING -i $IFACE -j flu-tel 2>/dev/null || iptables -t nat -I PREROUTING -i $IFACE -j flu-tel"
  echo "# Before per-pilot access every pilot reached every UGV: drop that rule if it is still there"
  echo "iptables -D FORWARD -i $IFACE -o $IFACE -s $PILOTS.0/24 -d 10.150.$LANS3.0/20 -j ACCEPT 2>/dev/null || true"
} > "$ACL.tmp"
mv "$ACL.tmp" "$ACL"
chmod 600 "$ACL"

# --- 5. Застосувати ----------------------------------------------------------------
# Маршрути до пірів: wg-quick ставить їх лише під час up, а syncconf — ні. Без маршруту сервер
# не переслав би нічого НРК чи пілоту, доданим пізніше. Ставимо потрібні, прибираємо від видалених.
sync_routes() {
  local want net
  # Перше поле — ключ піра (у base64 теж буває «/»), далі — його мережі: беремо лише мережі
  want=$(wg show "$IFACE" allowed-ips | awk '{ for (i = 2; i <= NF; i++) if ($i ~ /^[0-9.]+\/[0-9]+$/) print $i }' | sort -u)
  for net in $want; do ip -4 route replace "$net" dev "$IFACE"; done
  ip -4 route show dev "$IFACE" | awk '!/proto kernel/ { print ($1 ~ /\//) ? $1 : $1 "/32" }' | while read -r net; do
    grep -qxF "$net" <<< "$want" || ip -4 route del "$net" dev "$IFACE" || true
  done
}

if [ -z "${FLU_TEST:-}" ]; then
  if ip link show "$IFACE" >/dev/null 2>&1; then
    wg syncconf "$IFACE" <(wg-quick strip "$IFACE")   # нові піри без розриву наявних тунелів
    ip link set dev "$IFACE" mtu "$MTU"                # syncconf MTU не міняє, а тунелі від цього не рвуться
    sync_routes                                        # і маршрутів до нових пірів теж не ставить
    sh "$ACL"                                          # і PostUp теж: доступ пілотів оновлюємо самі
  else
    systemctl enable --now "wg-quick@$IFACE" >/dev/null 2>&1 || wg-quick up "$IFACE" >/dev/null
  fi
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow "$PORT/udp" >/dev/null
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="$PORT/udp" && firewall-cmd -q --reload
  fi
fi

ORPHANS=$(awk -F'\t' '$1 == "robot" && $5 == "" { printf " %s", $3 }' "$DB")
orphans_note() {
  [ -z "$ORPHANS" ] || say "НРК без пілота, їх зараз не бачить ніхто:$ORPHANS. Передайте кожен потрібному пілоту («Передати НРК» у програмі)"
}

# --- Службові команди: підсумок, рядок для програми не потрібен ----------------------
case $MODE in
  give)
    if [ -n "$PREV_OWNER" ] && [ "$PREV_OWNER" != "$PILOT_NAME" ]; then
      say "НРК $ROBOT_SERIAL передано від пілота $PREV_OWNER пілоту $PILOT_NAME"
    else
      say "НРК $ROBOT_SERIAL тепер у пілота $PILOT_NAME"
    fi
    say "адреси для пілота $PILOT_NAME: MAVLink $(host_of "$ROBOT_SLOT" $FC_HOST):$MAV_PORT, передня камера $(host_of "$ROBOT_SLOT" $CAM1_HOST), задня камера $(host_of "$ROBOT_SLOT" $CAM2_HOST)"
    orphans_note
    exit 0 ;;
  remove-pilot)
    say "пілота $PILOT_NAME видалено із сервера"
    orphans_note
    exit 0 ;;
  remove-robot)
    say "НРК $ROBOT_SERIAL видалено із сервера, слот $ROBOT_SLOT вільний. Щоб повернути НРК, додайте його знову й перепрошийте роутер"
    orphans_note
    exit 0 ;;
esac

# --- 6. Публічна адреса сервера ------------------------------------------------------
HOST=${FLU_ENDPOINT_HOST:-}
if [ -z "$HOST" ]; then
  for url in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
    HOST=$( (curl -4 -fsS --max-time 5 "$url" || wget -4 -qO- -T 5 "$url") 2>/dev/null | tr -d '[:space:]' || true)
    if [[ $HOST =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then break; fi
    HOST=""
  done
  [ -n "$HOST" ] || HOST=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
[ -n "$HOST" ] || die "не вдалося визначити публічну адресу сервера"

# --- 7. Відповідь для програми на ноутбуці ----------------------------------------------
json="{\"v\":1,\"endpoint\":\"$HOST:$PORT\",\"server_pubkey\":\"$SERVER_PUB\""
json="$json,\"tunnel\":\"$TUNNEL.0/24\",\"operators\":\"$PILOTS.0/24\",\"robot_lans\":\"10.150.$LANS3.0/20\""
# Від ключів пілота й НРК — лише відбитки (8 символів): за ними ноутбук перевіряє, що відповідь для нього
json="$json,\"pilot\":{\"name\":\"$PILOT_NAME\",\"id\":$PILOT_ID,\"key\":\"${PILOT_KEY:0:8}\"}"
if [ -n "$ROBOT_SLOT" ]; then
  json="$json,\"robot\":{\"serial\":\"$ROBOT_SERIAL\",\"slot\":$ROBOT_SLOT,\"key\":\"${ROBOT_KEY:0:8}\"}"
fi
json="$json}"

say "готово: сервер $HOST:$PORT, пілот $PILOT_NAME${ROBOT_SLOT:+, НРК $ROBOT_SERIAL (слот $ROBOT_SLOT)}"
if [ -n "$ROBOT_SLOT" ]; then
  if [ -n "$PREV_OWNER" ] && [ "$PREV_OWNER" != "$PILOT_NAME" ]; then
    say "НРК $ROBOT_SERIAL передано від пілота $PREV_OWNER пілоту $PILOT_NAME"
  fi
  say "НРК $ROBOT_SERIAL бачить лише пілот $PILOT_NAME"
fi
orphans_note
say "якщо в панелі хостингу є свій файрвол — відкрийте в ньому UDP-порт $PORT"
echo
echo "Скопіюйте весь рядок нижче й вставте в програму на ноутбуці:"
echo
echo "FLU1:$(printf '%s' "$json" | base64 | tr -d '\n'):END"
echo
