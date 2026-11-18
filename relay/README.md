# 会议中继（可选）· Meeting relay (optional)

不配中继，Bloom 一个人开会照常。配了中继，主持人可以发一条邀请链接：别人用浏览器打开就进会，能听、能说，看到和主持人一样的论文、逐字稿和修改卡片。

Without a relay Bloom works for one person. With one, the host shares an invite link and others join from a browser.

## 一行装好 · One command

一台有公网地址的 Linux 服务器（Ubuntu 22.04/24.04 或 Debian 12，1 核 1 GB 够几个人用）：

```bash
curl -fsSL https://raw.githubusercontent.com/lightblue888/bloom-meeting/main/relay/setup.sh | sudo bash
```

- 有自己的域名：先把 A 记录指到这台服务器，再加 `-s -- --domain meet.example.com`。没有域名就用 `<公网IP>.sslip.io`，HTTPS 证书照样自动申请。
- 云服务器的安全组要放行 TCP 80、443、7881 和 UDP 50000–60000。
- 跑完会打印一行连接码（`bloom1.` 开头），粘到 Bloom 的 设置 → 通用 → 邀请，点「测试连接」。连接码里有密钥，别发给别人。
- 再运行一次就是更新，密钥和连接码不变。`--dry-run` 只看要做的事，什么都不改。
- 仓库是私有的时候，先 `export GITHUB_TOKEN=<有读权限的 token>`，再用 `sudo -E bash`。
- 国内的云服务器用域名开 80/443 一般要先备案；没备案可以用海外的服务器。

装了什么：LiveKit（语音）、Caddy（HTTPS）、`token_server.py`（发进会的 token、转发论文快照和逐字稿，只放内存，散会就删）、访客页（从最新发布的 `bloom-guest.zip` 取）。

## 手动装 · By hand

想自己装的话，脚本做的就是下面这些：

1. 装 [LiveKit server](https://docs.livekit.io/home/self-hosting/deployment/)，生成 API key 和 secret，配置写在 `/etc/livekit.yaml`（7880 端口，RTC 用 TCP 7881 和 UDP 50000–60000）。
2. `token_server.py` 放到 `/opt/bloom/`，`/opt/bloom/livekit.env` 写三行 `LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`、`LIVEKIT_URL=wss://<域名>`，用 systemd 跑（`StateDirectory=bloom`，监听 127.0.0.1:8787）。只依赖 PyJWT。
3. 访客页：发布里的 `bloom-guest.zip` 解到 `/opt/bloom/www/`（或者自己打：`python -X utf8 oss/pack_guest.py bloom-guest.zip`）。
4. Caddy：

```
<你的域名> {
	encode gzip
	@relay path /token* /view/* /push/*
	reverse_proxy @relay 127.0.0.1:8787
	@lk path /rtc* /twirp/*
	reverse_proxy @lk 127.0.0.1:7880
	@guest {
		path / /room/*
	}
	handle @guest {
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

5. 主持人这边：设置 → 通用 → 邀请 里粘连接码。连接码可以自己拼：`bloom1.` 加上 `{"url":"https://<域名>/","key":"<key>","secret":"<secret>"}` 的 base64url（不要末尾的 `=`）。

## 本机试一试

```powershell
python -X utf8 relay/run_local_relay.py <一份测试用 livekit.env> <一个空目录>     # 127.0.0.1:8787
```
