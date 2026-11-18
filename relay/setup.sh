#!/usr/bin/env bash
# Bloom 会议中继：在一台有公网地址的 Linux 服务器（Ubuntu 22.04/24.04、Debian 12）上一行装好。
#
#   curl -fsSL https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/relay/setup.sh | sudo bash
#   curl -fsSL .../relay/setup.sh | sudo bash -s -- --domain meet.example.com      有自己的域名时
#
# 装什么：LiveKit（语音）、Caddy（HTTPS，自动申请证书）、Bloom 的中继小服务（token_server.py）和访客页。
# 没给域名就用 <公网IP>.sslip.io（不用买域名，证书照样自动申请）。最后打印一行连接码，
# 粘到 Bloom 的 设置 → 通用 → 邀请 里就行。再运行一次就是更新（密钥不变，连接码不变）。
#
# 选项：
#   --domain D     用自己的域名（先把 D 的 A 记录指到这台服务器）
#   --email E      申请证书时留的邮箱（可以不填）
#   --repo R       代码仓库 owner/name（默认 lightblue888/bloom-meeting）
#   --ref REF      分支或标签（默认 main）
#   --dry-run      只打印要做的事，什么都不改
# 环境变量 GITHUB_TOKEN：仓库是私有的时候用。
#
# 服务器的安全组 / 防火墙要放行：TCP 80、443、7881，UDP 50000-60000（云厂商控制台里的安全组得自己开）。
set -euo pipefail

DOMAIN=""
EMAIL=""
REPO="lightblue888/bloom-meeting"
REF="main"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --ref) REF="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "不认识的选项：$1" >&2; exit 2 ;;
  esac
done

BLOOM=/opt/bloom
WWW=$BLOOM/www
ENV_FILE=$BLOOM/livekit.env
LK_CONF=/etc/livekit.yaml
CADDY_SITE=/etc/caddy/bloom.caddy
STEP=0

say() { STEP=$((STEP + 1)); printf '\n\033[36m[%d] %s\033[0m\n' "$STEP" "$*"; }
ok() { printf '    \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
# 真改东西的命令：--dry-run 时只打印
run() { if [ "$DRY" = 1 ]; then printf '    + %s\n' "$*"; else "$@"; fi; }
# 写文件：put 路径 权限 < 内容
put() {
  local path="$1" mode="$2" tmp
  if [ "$DRY" = 1 ]; then printf '    + 写 %s (%s)\n' "$path" "$mode"; sed 's/^/      | /' | grep -v 'SECRET=\|^      |   [A-Za-z0-9]*: ' || true; return; fi
  tmp="$(mktemp)"; cat >"$tmp"; install -D -m "$mode" "$tmp" "$path"; rm -f "$tmp"
}
gh_get() {   # gh_get <url> <输出文件>：带上 GITHUB_TOKEN（私有仓库）
  local auth=()
  [ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
  curl -fsSL --retry 3 "${auth[@]}" "$1" -o "$2"
}

[ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "要用 root 运行：curl ... | sudo bash"
. /etc/os-release 2>/dev/null || true
case "${ID:-}" in
  ubuntu|debian) ;;
  *) [ "$DRY" = 1 ] || die "只支持 Ubuntu / Debian（这台是 ${PRETTY_NAME:-未知系统}）" ;;
esac

# ---------------------------------------------------------------- 1. 地址
say "公网地址和域名"
IP="$(curl -fsS4 --max-time 8 https://api.ipify.org 2>/dev/null || curl -fsS4 --max-time 8 https://ifconfig.me 2>/dev/null || true)"
[ -n "$IP" ] || die "查不到这台服务器的公网 IP（能上网吗？）"
if [ -z "$DOMAIN" ]; then
  DOMAIN="${IP//./-}.sslip.io"
  ok "没给域名，用 $DOMAIN（指向 $IP）"
else
  RESOLVED="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)"
  if [ "$RESOLVED" != "$IP" ]; then
    warn "$DOMAIN 现在解析到 ${RESOLVED:-（没有）}，这台是 $IP。证书要等域名指过来才申请得到"
  else
    ok "$DOMAIN → $IP"
  fi
fi

# ---------------------------------------------------------------- 2. 软件
say "安装软件（LiveKit、Caddy、Python）"
run apt-get update -qq
run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates gnupg debian-keyring debian-archive-keyring \
  apt-transport-https python3 python3-jwt unzip openssl
if [ "$DRY" = 1 ] || ! command -v livekit-server >/dev/null 2>&1; then
  run bash -c 'curl -sSL https://get.livekit.io | bash'
fi
if [ "$DRY" = 1 ] || ! command -v caddy >/dev/null 2>&1; then
  run bash -c "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
  run bash -c "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list"
  run apt-get update -qq
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
fi
ok "livekit-server、caddy、python3-jwt"

# ---------------------------------------------------------------- 3. 密钥（有就沿用：再跑一次连接码不变）
say "密钥"
KEY=""; SECRET=""
if [ -f "$ENV_FILE" ]; then
  KEY="$(sed -n 's/^LIVEKIT_API_KEY=//p' "$ENV_FILE" | head -1)"
  SECRET="$(sed -n 's/^LIVEKIT_API_SECRET=//p' "$ENV_FILE" | head -1)"
fi
if [ -z "$KEY" ] || [ -z "$SECRET" ]; then
  KEY="API$(openssl rand -hex 6)"
  SECRET="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 40)"
  ok "新生成了一对"
else
  ok "沿用 $ENV_FILE 里的"
fi
put "$ENV_FILE" 600 <<EOF
LIVEKIT_API_KEY=$KEY
LIVEKIT_API_SECRET=$SECRET
LIVEKIT_URL=wss://$DOMAIN
EOF

# ---------------------------------------------------------------- 4. LiveKit
say "LiveKit"
put "$LK_CONF" 600 <<EOF
port: 7880
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true
keys:
  $KEY: $SECRET
EOF
put /etc/systemd/system/livekit.service 644 <<'EOF'
[Unit]
Description=LiveKit (Bloom meeting relay)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/local/bin/livekit-server --config /etc/livekit.yaml
Restart=always
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 5. 中继小服务和访客页
say "中继服务和访客页"
RAW="https://raw.githubusercontent.com/$REPO/$REF"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo .)"
run mkdir -p "$BLOOM" "$WWW"
if [ -f "$HERE/token_server.py" ]; then
  run install -m 644 "$HERE/token_server.py" "$BLOOM/token_server.py"
  ok "token_server.py（本地那份）"
else
  run gh_get "$RAW/relay/token_server.py" "$BLOOM/token_server.py"
  ok "token_server.py（$REPO@$REF）"
fi
# 访客页：发版时打好的 bloom-guest.zip（访客页 g/ 和 livekit-client.umd.min.js），取最新一版
API="https://api.github.com/repos/$REPO/releases/latest"
if [ "$DRY" = 1 ]; then
  printf '    + 从 %s 取 bloom-guest.zip，解到 %s\n' "$API" "$WWW"
else
  TMPD="$(mktemp -d)"
  auth=(); [ -n "${GITHUB_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
  ASSET="$(curl -fsSL "${auth[@]}" "$API" | python3 -c 'import json,sys
for a in json.load(sys.stdin).get("assets", []):
    if a.get("name") == "bloom-guest.zip": print(a["url"]); break')" || true
  [ -n "$ASSET" ] || die "最新的发布里没有 bloom-guest.zip（仓库 $REPO；私有仓库要设 GITHUB_TOKEN）"
  curl -fsSL --retry 3 "${auth[@]}" -H "Accept: application/octet-stream" "$ASSET" -o "$TMPD/guest.zip"
  rm -rf "$WWW/g"
  unzip -qo "$TMPD/guest.zip" -d "$WWW"
  rm -rf "$TMPD"
  ok "访客页 → $WWW"
fi
put /etc/systemd/system/bloom-token.service 644 <<'EOF'
[Unit]
Description=Bloom relay (token_server.py)
After=network.target
[Service]
ExecStart=/usr/bin/python3 /opt/bloom/token_server.py
StateDirectory=bloom
Restart=always
[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 6. Caddy（HTTPS）
say "HTTPS（Caddy）"
put "$CADDY_SITE" 644 <<EOF
$DOMAIN {
	encode gzip
	@relay path /token* /view/* /push/*
	reverse_proxy @relay 127.0.0.1:8787
	@lk path /rtc* /twirp/*
	reverse_proxy @lk 127.0.0.1:7880
	@guest {
		path / /room/*
	}
	handle @guest {
		root * $WWW
		rewrite * /g/guest.html
		file_server
	}
	handle {
		root * $WWW
		file_server
	}
}
EOF
CF=/etc/caddy/Caddyfile
if [ "$DRY" = 1 ]; then
  printf '    + 在 %s 里加一行 import %s（原来的站点不动）\n' "$CF" "$CADDY_SITE"
elif ! grep -qs "import $CADDY_SITE" "$CF"; then
  if [ -f "$CF" ] && grep -qs '^:80\b' "$CF" && [ "$(grep -cv '^\s*\(#.*\)\?$' "$CF")" -le 4 ]; then
    cp "$CF" "$CF.bak-bloom"                       # 装 Caddy 时自带的示例站点（只占 80 口）：换成我们的
    { [ -n "$EMAIL" ] && printf '{\n\temail %s\n}\n\n' "$EMAIL"; echo "import $CADDY_SITE"; } >"$CF"
  else
    [ -f "$CF" ] && cp "$CF" "$CF.bak-bloom"
    { echo; echo "import $CADDY_SITE"; } >>"$CF"
  fi
fi
run caddy validate --config "$CF" --adapter caddyfile

# ---------------------------------------------------------------- 7. 防火墙、启动
say "防火墙和服务"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  run ufw allow 80/tcp
  run ufw allow 443/tcp
  run ufw allow 7881/tcp
  run ufw allow 50000:60000/udp
  ok "ufw 已放行 80、443、7881/tcp 和 50000-60000/udp"
else
  ok "本机没开 ufw"
fi
run systemctl daemon-reload
run systemctl enable --now livekit bloom-token caddy
run systemctl restart livekit bloom-token
run systemctl reload caddy

# ---------------------------------------------------------------- 8. 检查
say "检查"
if [ "$DRY" = 1 ]; then
  printf '    + curl https://%s/token?room=probe 应该回 403（没在开会）\n' "$DOMAIN"
else
  code=""
  for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$DOMAIN/token?room=bloomprobe" || true)"
    [ "$code" = 403 ] || [ "$code" = 200 ] && break
    sleep 3
  done
  if [ "$code" = 403 ] || [ "$code" = 200 ]; then
    ok "https://$DOMAIN 通了"
  else
    warn "https://$DOMAIN 还连不上（$code）。看看安全组有没有放行 80、443，域名是不是指到了这台"
  fi
fi

# ---------------------------------------------------------------- 连接码
CODE="bloom1.$(printf '{"url":"https://%s/","key":"%s","secret":"%s"}' "$DOMAIN" "$KEY" "$SECRET" | base64 -w0 | tr '+/' '-_' | tr -d '=')"
printf '\n\033[32m装好了。\033[0m 把下面这行连接码粘到 Bloom 的 设置 → 通用 → 邀请：\n\n%s\n\n' "$CODE"
printf '云服务器的安全组记得放行 TCP 80、443、7881 和 UDP 50000-60000。\n'
printf '连接码里有密钥，别发给别人。再运行一次本脚本就是更新，连接码不变。\n'
