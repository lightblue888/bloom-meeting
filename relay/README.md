# 会议中继（可选）· Meeting relay (optional)

不配中继，Bloom 一个人开会照常。配了中继，主持人可以发一条邀请链接：别人用浏览器打开就进会，能听、能说，看到和主持人一样的论文、逐字稿和修改卡片，也能加自己的论文；主持人设为共同主持人的人还能确认、撤回改动。

Without a relay Bloom works for one person. With one, the host shares an invite link and others join from a browser.

## 需要什么

一台有公网地址的 Linux 服务器（1 核 1 GB 够几个人用），一个域名指向它，开放 TCP 443、7881 和 UDP 50000–60000。

## 1. LiveKit

装 [LiveKit server](https://docs.livekit.io/home/self-hosting/deployment/)，记下 API key 和 secret，让它只听本机 7880（信令经下面的 Caddy 走 443）。

## 2. 中继服务（token_server.py）

```bash
sudo mkdir -p /opt/bloom /var/lib/bloom
sudo cp token_server.py /opt/bloom/
sudo pip install pyjwt            # 只依赖 PyJWT
cat <<'EOF' | sudo tee /opt/bloom/livekit.env
LIVEKIT_API_KEY=<你的 key>
LIVEKIT_API_SECRET=<你的 secret>
LIVEKIT_URL=wss://<你的域名>
EOF
sudo chmod 600 /opt/bloom/livekit.env
```

用 systemd 跑（`StateDirectory=bloom`，监听 127.0.0.1:8787）：

```ini
[Unit]
Description=Bloom relay
After=network.target
[Service]
ExecStart=/usr/bin/python3 /opt/bloom/token_server.py
StateDirectory=bloom
Restart=always
[Install]
WantedBy=multi-user.target
```

## 3. 访客页

在开发机上构建（`desktop/web/apps/desktop`）：

```powershell
pnpm exec vite build --config vite.guest.config.ts     # 产物在 dist-guest/
```

把 `dist-guest/` 里的东西放到服务器的 `/opt/bloom/www/g/`，再放一份 LiveKit 的浏览器版 `livekit-client.umd.min.js` 到 `/opt/bloom/www/`。

## 4. Caddy（HTTPS）

```
<你的域名> {
	encode gzip
	@relay path /token* /view/* /push/*
	reverse_proxy @relay 127.0.0.1:8787
	@lk path /rtc* /twirp/*
	reverse_proxy @lk 127.0.0.1:7880
	handle /room/* {
		root * /opt/bloom/www
		rewrite * /g/guest.html
		file_server
	}
	handle {
		root * /opt/bloom/www
		file_server
	}
}
```

## 5. 主持人这边

把同一份 `livekit.env`（三行）放到主持人电脑的 `%USERPROFILE%\.zuhui\livekit.env`，`config.json` 里写：

```json
"share": { "base_url": "https://<你的域名>/" }
```

重启 Bloom，会议页的「邀请」就能复制链接了。会议内容（论文快照、逐字稿）只在中继的内存里，散会就删。

## 本机试一试

```powershell
python -X utf8 relay/run_local_relay.py <一份测试用 livekit.env> <一个空目录>     # 127.0.0.1:8787
```
