# TechX Shude Mood Barometer

一个面向学生每日情绪记录的心情晴雨表 Web 应用。项目使用 FastAPI、Jinja2 和 SQLite 存储，不需要 Node.js 构建流程。

## 功能

- 账号注册、登录、退出登录。
- 个人资料维护：姓名、昵称、年级、项目和密码。
- 每日心情报表：选择心情 emoji，并回答固定问题。
- 心情日历：按月查看每天最后一次提交。
- 心情历史：查看自己的全部心情记录。
- 管理员后台：搜索用户、查看用户列表、用户详情和心情记录。
- 演示数据脚本：批量生成普通用户和心情记录。

## 技术栈

- 后端：FastAPI、Uvicorn、SQLite。
- 模板：Jinja2。
- 密码：Werkzeug password hashing。
- 数据库：SQLite，默认写入 `data/mood_barometer.sqlite3`。
- 部署：可直接运行 `main.py`，也可使用 `deploy-first-run.sh` 创建 systemd 用户服务。

## 项目结构

```text
.
├── templates/            # Jinja2 页面模板
├── static/               # CSS、前端脚本和图片
├── scripts/
│   ├── init_admin.sh     # 兼容入口，转发到 deploy-first-run.sh
│   └── seed_demo_data.py # 生成演示用户和心情记录
├── tests/                # 回归测试
├── main.py               # FastAPI 应用入口、路由和 SQLite 初始化
├── deploy-first-run.sh   # Linux 首次部署脚本
├── requirements.txt      # Python 依赖
├── .env.example          # 环境变量示例
└── README.md
```

运行后会自动生成：

```text
data/
└── mood_barometer.sqlite3
```

## 本地运行

确保当前 shell 的 `python3` 指向你要使用的 Python 环境，然后在项目根目录运行：

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

修改 `.env`，至少替换 `MOOD_SECRET_KEY`。示例内容：

```env
MOOD_HOST=127.0.0.1
MOOD_PORT=5000
MOOD_SECRET_KEY=replace-with-a-random-secret-key
MOOD_DB_PATH=data/mood_barometer.sqlite3
```

启动：

```bash
python3 main.py
```

默认监听：

```text
http://127.0.0.1:5000
```

开发时如需 reload：

```bash
FASTAPI_RELOAD=1 python3 main.py
```

服务启动时会自动创建 SQLite 数据库和所需表结构。旧数据库可以直接随新版启动，缺失列会自动补齐。

## 首次使用

1. 打开 `http://127.0.0.1:5000`。
2. 注册第一个账号。
3. 第一个注册用户会自动成为管理员。
4. 登录后即可提交心情、查看日历和历史记录。

也可以在首次部署脚本里预创建管理员账号，见下方 Linux 部署说明。

## Linux 部署

建议先在项目根目录创建 `.env`：

```bash
cd /root/TechX-Shude-Mood-Barometer
cp .env.example .env
nano .env
```

生产环境建议保持：

```env
MOOD_HOST=127.0.0.1
MOOD_PORT=5000
```

如果已经用 Caddy 反代，`MOOD_HOST` 保持 `127.0.0.1`，不要把 Python 服务直接开放到公网。

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

项目提供首次部署脚本：

```bash
chmod +x deploy-first-run.sh scripts/init_admin.sh
./deploy-first-run.sh
```

脚本会：

- 检查 `python3`。
- 创建 `data/` 目录。
- 初始化 SQLite 数据库。
- 默认创建并启动 systemd 用户服务 `techx-shude-mood-barometer.service`。
- 不会创建 `.env`，也不会向 systemd service 写入 `MOOD_PORT`、`MOOD_SECRET_KEY` 等运行环境变量。

只初始化数据库、不创建 systemd 服务：

```bash
./deploy-first-run.sh --no-systemd
```

创建 systemd 服务但不立即启动：

```bash
./deploy-first-run.sh --no-start
```

可选环境变量：

```bash
MOOD_ADMIN_NICKNAME=admin \
MOOD_ADMIN_NAME=管理员 \
MOOD_ADMIN_PASSWORD='change-this-password' \
./deploy-first-run.sh
```

也可以把管理员初始化配置写进 `.env`：

```env
MOOD_ADMIN_NICKNAME=admin
MOOD_ADMIN_NAME=管理员
MOOD_ADMIN_PASSWORD=change-this-password
```

`deploy-first-run.sh` 初始化数据库时会导入 `main.py`，而 `main.py` 会读取 `.env`，所以这些管理员变量可以从 `.env` 生效。再次运行脚本时，如果昵称已存在，会把该账号更新为管理员并重设密码。

如果只设置了 `MOOD_ADMIN_NICKNAME` 或只设置了 `MOOD_ADMIN_PASSWORD`，脚本会报错退出；两者需要同时设置。部署完成后建议从 `.env` 中移除明文管理员密码，别把钥匙挂门口。

其他环境变量：

- `MOOD_SERVICE_NAME`：systemd 用户服务名，默认 `techx-shude-mood-barometer.service`。
- `MOOD_PORT`：只用于脚本最后输出访问地址提示；实际监听端口由 `.env`、外部环境变量或程序默认值决定。

注意：`MOOD_SERVICE_NAME` 是 shell 脚本开头读取的变量，不会通过 `.env` 生效。如需自定义服务名，请在运行脚本时直接传入：

```bash
MOOD_SERVICE_NAME=mood-barometer.service ./deploy-first-run.sh
```

脚本生成的用户服务默认位置：

```bash
~/.config/systemd/user/techx-shude-mood-barometer.service
```

生成后的 service 大致如下：

```ini
[Unit]
Description=TechX Shude Mood Barometer
After=network.target

[Service]
WorkingDirectory=/root/TechX-Shude-Mood-Barometer
ExecStart=/usr/bin/env python3 /root/TechX-Shude-Mood-Barometer/main.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

修改 `.env` 或 service 后，重载并重启：

```bash
systemctl --user daemon-reload
systemctl --user restart techx-shude-mood-barometer.service
systemctl --user status techx-shude-mood-barometer.service
```

如需退出 SSH 后服务继续运行：

```bash
loginctl enable-linger "$USER"
```

本机检查：

```bash
curl http://127.0.0.1:5000/login
```

如果使用 Caddy 反代，示例配置：

```caddyfile
your-domain.com {
    reverse_proxy 127.0.0.1:5000
}
```

修改 Caddyfile 后：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 演示数据

生成演示用户和心情记录：

```bash
python3 scripts/seed_demo_data.py
```

普通演示账号：

```text
demo_alice / test123456
```

脚本会创建或更新 `demo_*` 用户，并插入心情记录。数据库路径读取 `.env` 中的 `MOOD_DB_PATH`，默认在 `data/mood_barometer.sqlite3`。

## 数据说明

SQLite 数据库默认位置：

```text
data/mood_barometer.sqlite3
```

该目录已被 `.gitignore` 忽略，避免提交本地运行数据。生产环境建议定期备份该文件。

密码不会明文保存，数据库中保存的是 Werkzeug 生成的密码哈希。生产环境必须替换 `.env` 里的 `MOOD_SECRET_KEY`，并在管理员初始化完成后移除明文 `MOOD_ADMIN_PASSWORD`。

## 开发说明

项目没有前端打包步骤。修改 `templates/`、`static/` 或 `main.py` 后，通常刷新浏览器或重启 `main.py` 即可查看效果。

运行测试：

```bash
python -m pytest -q --basetemp data/.pytest-tmp -p no:cacheprovider
```

测试不会占用默认 `5000` 端口。
