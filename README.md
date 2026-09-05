# TechX Shude Mood Barometer

一个使用 FastAPI、Jinja2 和 SQLite 的学生心情记录应用。认证由独立部署的
NetHub Accounts 提供，TechX 只保存本网站成员、角色和业务资料，不需要 Node.js
构建流程。

## 账号模型

- 使用 OIDC Authorization Code + PKCE S256 接入 NetHub Accounts。
- 本地 `users` 表继续保存实名、昵称、年级、项目、管理员状态和隐私同意时间。
- `auth_sub` 唯一关联中央账号；中央管理员不会自动成为 TechX 管理员。
- 中央用户首次访问 TechX 时创建普通本地成员，并单独完成 TechX 隐私同意。
- 用户名和显示名称只在首次建档时复制，之后 TechX 资料独立维护。
- 本地注册、密码登录和修改密码入口在生产环境中关闭。
- 本地会话是数据库中的不透明随机令牌；Cookie 不保存用户资料或权限。
- TechX 退出只结束本站会话；从 Accounts 选择“退出所有网站”时，Accounts 通过
  签名的 Back-Channel Logout 撤销本站会话。
- Accounts 暂时不可用时，已经存在且未过期的 TechX 会话仍然有效；新登录会显示
  明确的服务不可用页面。

## 功能

- 每日 PANAS-C-SF 10 项心情量表、心情日历、历史和趋势图。
- 旧格式 emoji 与文字记录归档。
- 个人资料维护：姓名、昵称、年级和项目。
- 管理后台：本地成员搜索、详情、记录与角色管理。

网站用户列表只显示访问过 TechX 的本地成员，不会列出 Accounts 中的全部用户。

## 技术栈和目录

- Python 3.12、FastAPI、Authlib、Jinja2、SQLite、Uvicorn。
- `main.py`：应用、路由和自动数据库升级。
- `techx_auth.py`：OIDC 配置、不透明会话和退出令牌校验。
- `scripts/apply_auth_mapping.py`：将 Accounts 迁移映射应用到 TechX 备份库。
- `deploy-first-run.sh`：Conda 和 systemd 用户服务部署脚本。
- `tests/`：业务及统一认证回归测试。

## Accounts 客户端配置

先在 NetHub Accounts 项目中注册机密 Web 客户端。下面的地址必须替换成 TechX 的
真实 HTTPS 域名，回调地址必须精确匹配：

```bash
python -m app.cli register-client \
  --client-id techx \
  --name TechX \
  --redirect-uri https://techx.example.com/auth/callback \
  --launch-uri https://techx.example.com/ \
  --backchannel-logout-uri https://techx.example.com/auth/backchannel-logout
```

把命令输出的客户端密钥安全地写入 TechX 的 `.env`，不要提交到 Git。若 Accounts
仓库中的 CLI 参数发生调整，以该项目的 `python -m app.cli --help` 为准。

## 本地运行

推荐使用独立 Conda 环境：

```bash
conda create -n techx-shude-mood-barometer python=3.12 pip
conda activate techx-shude-mood-barometer
python -m pip install -r requirements.txt
cp .env.example .env
```

修改 `.env`：

```env
MOOD_HOST=127.0.0.1
MOOD_PORT=5000
MOOD_DB_PATH=data/mood_barometer.sqlite3
MOOD_PUBLIC_BASE_URL=https://techx.example.com
MOOD_SESSION_COOKIE_SECURE=true
ACCOUNTS_ISSUER=https://auth.nethub.wiki
ACCOUNTS_CLIENT_ID=techx
ACCOUNTS_CLIENT_SECRET=replace-with-the-client-secret
```

`MOOD_PUBLIC_BASE_URL` 不得带路径或末尾 `/`。生产环境必须使用 HTTPS 并保持
`MOOD_SESSION_COOKIE_SECURE=true`。启动服务：

```bash
python main.py
```

默认监听 `127.0.0.1:5000`。数据库表和新增列会在启动时自动创建。

## 旧账号硬切换

不要直接对运行中的生产数据库操作。先停止 TechX，分别备份 Accounts 和 TechX
数据库，并由 Accounts 的迁移工具生成、人工确认映射文件。映射文件格式为 Accounts
导出的版本 1 JSON，必须覆盖 TechX 的每个本地用户。

先对 TechX 数据库备份执行预检：

```bash
python scripts/apply_auth_mapping.py \
  --database backups/mood_barometer.sqlite3 \
  --mapping migration/identity-mapping.json \
  --dry-run
```

确认人数、缺失用户和冲突均为零后，再应用：

```bash
python scripts/apply_auth_mapping.py \
  --database backups/mood_barometer.sqlite3 \
  --mapping migration/identity-mapping.json
```

工具会在一个事务中写入 `auth_sub`，将旧密码哈希移入本地归档表后清空原字段，并
清除旧会话。重复应用同一映射是安全的；映射冲突会拒绝执行。完成后用这份已迁移
数据库替换生产库，再启动新版应用。迁移报告、映射文件、数据库备份和客户端密钥都
不得提交到公共仓库。

## Linux 简单部署

项目脚本会创建或复用 Python 3.12 Conda 环境、安装依赖、初始化数据库，并默认安装
和启动 systemd 用户服务：

```bash
cp .env.example .env
nano .env
chmod +x deploy-first-run.sh
./deploy-first-run.sh
```

脚本可幂等重复执行，并支持：

```bash
./deploy-first-run.sh --no-systemd
./deploy-first-run.sh --no-start
MOOD_CONDA_ENV=techx ./deploy-first-run.sh
```

查看服务：

```bash
systemctl --user status techx-shude-mood-barometer.service
journalctl --user -u techx-shude-mood-barometer.service -f
```

如需退出 SSH 后继续运行：

```bash
loginctl enable-linger "$USER"
```

Caddy 示例：

```caddyfile
techx.example.com {
    reverse_proxy 127.0.0.1:5000
}
```

应用只应监听回环地址，由 Caddy 负责公网 HTTPS。

## 备份与恢复

SQLite 默认位于 `data/mood_barometer.sqlite3`。停止服务后复制数据库文件即可得到一致
备份；恢复时停止服务、保存当前文件、替换数据库，再启动并检查日志。不要只复制正在
写入的 SQLite 主文件而忽略 WAL 文件。

## 测试

```bash
python -m pytest -q --basetemp data/.pytest-tmp -p no:cacheprovider
python -m compileall -q main.py techx_auth.py scripts
bash -n deploy-first-run.sh
```

测试覆盖旧入口关闭、OIDC 回调、首次建档、角色/隐私资料保留、Accounts 故障下的
本地会话、签名退出通知和迁移幂等性。测试不会访问真实网站。

## 安全说明

- 会话 Cookie 使用 `HttpOnly + SameSite=Lax`，生产环境同时使用 `Secure`。
- OIDC 客户端密钥只保存在权限受限的 `.env` 中。
- TechX 只接受 Accounts 使用 RS256 签名、受众为本客户端且未重放的退出令牌。
- 本地角色仍由 TechX 管理；不要根据中央账号名称或首次访问顺序授予管理员。
