# TechX Shude Mood Barometer

学生心情晴雨表，一个基于 FastAPI 的心情记录系统。普通用户可以提交每日心情、查看日历和历史记录；管理员可以搜索用户，并查看每个用户的心情记录。

## 功能

- 用户注册、登录、退出。
- 个人资料维护：昵称、年级、项目、密码。
- 心情报表：选择心情 emoji，并回答固定问题。
- 心情日历：按月显示每天最后一次提交，可点击有记录的日期跳到历史记录。
- 历史记录：查看自己的全部心情记录。
- 右侧最近记录：只显示最近 3 条。
- 管理员后台：用户搜索、用户列表、用户详情和心情记录查看。
- 初始化脚本：创建/更新管理员账号，并可创建 systemd 自启服务。
- 演示数据脚本：批量生成普通用户和心情记录。

## 技术栈

- Python 3.10+
- FastAPI
- Jinja2
- SQLite
- Uvicorn
- Werkzeug password hashing

## 本地开发

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少设置：

```env
SECRET_KEY=replace-with-a-random-secret-key
ADMIN_NICKNAME=admin
ADMIN_PASSWORD=replace-with-a-strong-password
```

开发运行：

```bash
uvicorn main:app --host 127.0.0.1 --port 8001 --reload
```

也可以用入口脚本运行：

```bash
PORT=8001 FASTAPI_RELOAD=1 python main.py
```

## 初始化管理员

初始化脚本会创建数据库表，并创建或更新 `.env` 中指定的管理员账号。

```bash
sh scripts/init_admin.sh
```

默认情况下，脚本会用当前默认 `python3` 创建 Linux systemd 用户服务。请先激活你要使用的虚拟环境或 Conda 环境，再运行脚本。

只初始化数据库和管理员账号，不创建服务：

```env
INSTALL_SYSTEMD_SERVICE=0
```

常用服务配置：

```env
APP_HOST=127.0.0.1
PORT=5000
SYSTEMD_SERVICE_NAME=techx-shude-mood-barometer.service
SYSTEMD_START_NOW=1
```

说明：

- 脚本使用当前 `PATH` 中的 `python3`，类似 `/usr/bin/env python3`。
- `SYSTEMD_START_NOW=1` 会在创建服务后立即重启服务。
- 服务文件路径为 `~/.config/systemd/user/<SYSTEMD_SERVICE_NAME>`。
- 如果服务需要在退出 SSH 后继续运行，执行 `loginctl enable-linger $USER`。

常用 systemd 命令：

```bash
systemctl --user status techx-shude-mood-barometer.service
systemctl --user restart techx-shude-mood-barometer.service
journalctl --user -u techx-shude-mood-barometer.service -f
```

## 演示数据

生成演示用户和记录：

```bash
python scripts/seed_demo_data.py
```

普通演示账号：

```text
demo_alice / test123456
```

脚本会创建或更新 `demo_*` 用户，并为他们插入心情记录。数据库路径读取 `.env` 中的 `MOOD_DB_PATH`，默认在 `data/mood_barometer.sqlite3`。

## 测试

```bash
python -m pytest -q --basetemp data/.pytest-tmp -p no:cacheprovider
```

`data/.pytest-tmp/` 是测试临时目录，已经加入 `.gitignore`。如果仓库里已有历史测试产物，新增忽略规则不会自动删除它们。

## 目录结构

```text
.
├── main.py                  # FastAPI 应用入口、路由、数据库逻辑
├── requirements.txt         # Python 依赖
├── scripts/
│   ├── init_admin.sh        # 初始化管理员和 systemd 服务
│   └── seed_demo_data.py    # 生成演示用户和记录
├── static/
│   ├── app.js               # 前端交互脚本
│   ├── login-campus.png     # 登录页图片
│   └── styles.css           # 页面样式
├── templates/               # Jinja2 模板
└── tests/                   # 测试
```

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `SECRET_KEY` | Session 加密密钥，生产环境必须改成随机强密钥。 |
| `MOOD_DB_PATH` | SQLite 数据库路径，默认 `data/mood_barometer.sqlite3`。 |
| `ADMIN_NICKNAME` | 管理员昵称。 |
| `ADMIN_PASSWORD` | 管理员密码。 |
| `ADMIN_REAL_NAME` | 管理员姓名，默认 `管理员`。 |
| `ADMIN_GRADE` | 管理员年级，可为空、`2024`、`2025`、`2026`。 |
| `ADMIN_PROGRAM` | 管理员项目，可为空、`AP`、`IB`。 |
| `APP_HOST` | systemd 服务监听地址。 |
| `PORT` | 服务端口；`python main.py` 也会读取这个值。 |
| `FASTAPI_RELOAD` | 设为 `1` 时，`python main.py` 使用 reload 模式。 |
| `INSTALL_SYSTEMD_SERVICE` | `1` 创建 systemd 服务，`0` 跳过。 |
| `SYSTEMD_SERVICE_NAME` | systemd 用户服务名。 |
| `SYSTEMD_START_NOW` | `1` 表示创建服务后立即启动/重启。 |

## 路由概览

- `/register` 注册
- `/login` 登录
- `/profile` 个人资料
- `/mood-report` 提交心情
- `/mood-calendar` 心情日历
- `/mood-history` 历史记录
- `/admin` 管理员后台
- `/admin/users/{user_id}` 用户心情记录

## 安全和数据

- `.env`、SQLite 数据库、测试临时数据库都不应提交到 Git。
- 管理员密码不会明文保存，数据库中保存的是 Werkzeug 生成的密码哈希。
- 生产环境不要使用 `.env.example` 里的示例密钥和示例密码。
