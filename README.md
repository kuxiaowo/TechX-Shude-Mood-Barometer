# TechX Shude Mood Barometer

学生心情晴雨表，一个基于 FastAPI、Jinja2 和 SQLite 的心情记录系统。普通用户可以注册、登录、维护资料、提交每日心情、查看日历和历史记录；管理员可以搜索用户并查看用户心情记录。

## 功能

- 用户注册、登录、退出
- 个人资料维护：姓名、昵称、年级、项目、密码
- 心情报表：选择心情 emoji，并回答固定问题
- 心情日历：按月查看每日最后一次提交
- 历史记录：查看自己的全部心情记录
- 管理员后台：用户搜索、用户列表、用户详情和心情记录查看
- 首次部署脚本：初始化 SQLite、创建或更新管理员、可选创建 systemd 用户服务
- 演示数据脚本：批量生成普通用户和心情记录

## 技术栈

- Python 3.10+
- FastAPI
- Jinja2
- SQLite
- Uvicorn
- Werkzeug password hashing

## 本地开发

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少修改：

```env
SECRET_KEY=replace-with-a-random-secret-key
ADMIN_NICKNAME=admin
```

开发运行示例：

```bash
PORT=8001 FASTAPI_RELOAD=1 python main.py
```

或者：

```bash
uvicorn main:app --host 127.0.0.1 --port 8001 --reload
```

## 首次配置脚本

推荐使用根目录脚本：

```bash
MOOD_ADMIN_NICKNAME=admin MOOD_ADMIN_PASSWORD='换成强密码' ./deploy-first-run.sh
```

脚本会做这些事：

- 创建 `data/` 目录和 SQLite 数据库
- 导入项目自身的 `main.app` 并调用 `main.init_db()` 初始化表结构
- 使用项目自身的 Werkzeug 哈希逻辑创建或更新管理员账号
- 在 Linux 上可选写入 `~/.config/systemd/user/<service>.service`

常用选项：

```bash
./deploy-first-run.sh --no-systemd
./deploy-first-run.sh --no-start
```

兼容旧入口：

```bash
./scripts/init_admin.sh
```

它现在只转发到 `deploy-first-run.sh`，避免维护两份初始化逻辑。

脚本参数与参考部署脚本保持同一类风格：

```bash
MOOD_ADMIN_NICKNAME=admin
MOOD_ADMIN_NAME=管理员
MOOD_ADMIN_PASSWORD='换成强密码'
MOOD_SERVICE_NAME=techx-shude-mood-barometer.service
MOOD_PORT=5000
```

脚本使用当前 `PATH` 中的 `python3`。运行前请自己确保它指向你要用的环境。

## 生产环境部署

以下以 Ubuntu/Debian、普通用户部署、Nginx 反向代理为例。

1. 安装系统依赖：

```bash
sudo apt update
sudo apt install -y python3 nginx
```

2. 上传或拉取项目，然后进入项目目录，安装依赖：

```bash
cd /opt/techx-shude-mood-barometer
python3 -m pip install -r requirements.txt
```

3. 准备 `.env`。生产环境至少要改 `SECRET_KEY`，数据库路径可按需调整：

```bash
cp .env.example .env
nano .env
```

4. 首次初始化。脚本创建的 systemd 服务固定监听 `127.0.0.1`，由 Nginx 对外提供服务：

```bash
MOOD_PORT=5000 \
MOOD_ADMIN_NICKNAME=admin \
MOOD_ADMIN_NAME=管理员 \
MOOD_ADMIN_PASSWORD='换成足够强的密码' \
./deploy-first-run.sh
```

如果只想初始化数据库和管理员，不创建服务：

```bash
MOOD_ADMIN_NICKNAME=admin MOOD_ADMIN_PASSWORD='换成足够强的密码' ./deploy-first-run.sh --no-systemd
```

5. 让 systemd 用户服务在退出 SSH 后继续运行：

```bash
loginctl enable-linger "$USER"
systemctl --user status techx-shude-mood-barometer.service
journalctl --user -u techx-shude-mood-barometer.service -f
```

6. 配置 Nginx 反向代理示例：

```nginx
server {
    listen 80;
    server_name example.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

启用站点后 reload：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

生产注意事项：

- `.env`、SQLite 数据库和日志不要提交到 Git。
- `SECRET_KEY` 和 `MOOD_ADMIN_PASSWORD` 必须换成生产强随机值。
- 默认数据库路径是 `data/mood_barometer.sqlite3`，生产环境要定期备份这个文件。
- 如果直接对外开放应用端口，才需要放行应用端口；使用 Nginx 反代时通常只放行 80/443。
- HTTPS 建议用 Certbot 或你的云厂商证书方案配置。

## 演示数据

生成演示用户和心情记录：

```bash
python scripts/seed_demo_data.py
```

普通演示账号：

```text
demo_alice / test123456
```

脚本会创建或更新 `demo_*` 用户，并插入心情记录。数据库路径读取 `.env` 中的 `MOOD_DB_PATH`，默认在 `data/mood_barometer.sqlite3`。

## 测试

```bash
python -m pytest -q --basetemp data/.pytest-tmp -p no:cacheprovider
```

`data/.pytest-tmp/` 是测试临时目录，已加入 `.gitignore`。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `SECRET_KEY` | Session 加密密钥，生产环境必须改成随机强密钥。 |
| `MOOD_DB_PATH` | SQLite 数据库路径，默认 `data/mood_barometer.sqlite3`。 |
| `ADMIN_NICKNAME` | 应用启动时会把同昵称的已有用户提升为管理员。 |
| `PORT` | 服务端口；`python main.py` 也会读取这个值。 |
| `FASTAPI_RELOAD` | 设为 `1` 时，`python main.py` 使用 reload 模式。 |
| `MOOD_ADMIN_NICKNAME` | 首次部署脚本使用的管理员昵称。 |
| `MOOD_ADMIN_NAME` | 首次部署脚本使用的管理员姓名，默认同昵称。 |
| `MOOD_ADMIN_PASSWORD` | 首次部署脚本使用的管理员密码。 |
| `MOOD_SERVICE_NAME` | 首次部署脚本创建的 systemd 用户服务名。 |
| `MOOD_PORT` | 首次部署脚本创建 systemd 服务时使用的端口。 |

## 路由概览

- `/register` 注册
- `/login` 登录
- `/profile` 个人资料
- `/mood-report` 提交心情
- `/mood-calendar` 心情日历
- `/mood-history` 历史记录
- `/admin` 管理员后台
- `/admin/users/{user_id}` 用户心情记录
