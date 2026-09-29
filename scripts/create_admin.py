"""创建一个独立管理员账号；不提升、不重置已有普通账号。

运行：.venv/Scripts/python.exe scripts/create_admin.py --username admin
随机密码仅写入本机被 Git 忽略的 .env.admin，重复执行不会重置账号。
"""

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from backend.app.auth import hash_password
from backend.app.database import Database, UsernameExists
from backend.app.models.auth_schemas import Credentials


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="创建管理员账号")
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    load_dotenv(root / ".env")
    credentials = Credentials(username=args.username, password=secrets.token_urlsafe(24))
    destination = root / ".env.admin"
    if destination.exists():
        raise SystemExit(".env.admin 已存在，请保管原凭据；本次未修改任何账号。")
    database = Database.from_env()
    database.initialize()
    database.require_ready()
    # 先独占创建文件，防止并发运行覆盖密码；失败时删除本次创建的文件。
    handle = destination.open("x", encoding="utf-8")
    try:
        with handle:
            handle.write(f"管理员账号：{credentials.username}\n管理员密码：{credentials.password}\n登录地址：http://localhost:8501\n")
            handle.flush()
            database.create_user(credentials.username, hash_password(credentials.password), admin=True)
    except UsernameExists:
        destination.unlink(missing_ok=True)
        raise SystemExit("账号已存在。本次未提升权限、未重置密码，请使用一个新的账号名。")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    print(f"管理员 {credentials.username} 已创建；密码保存到 .env.admin（不会提交到 Git）。")


if __name__ == "__main__":
    main()
