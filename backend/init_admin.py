"""Interactively initialize the encrypted local administrator account."""

import getpass
import os

from utils.admin_credentials import ADMIN_CREDENTIALS_FILE, save_credentials


def main() -> None:
    exists = os.path.isfile(ADMIN_CREDENTIALS_FILE)
    if exists:
        answer = input("管理员凭据已存在，是否覆盖？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消。")
            return

    username = input("管理员账号: ").strip()
    email = input("找回密码邮箱: ").strip()
    password = getpass.getpass("管理员密码（至少8位，含字母和数字）: ")
    confirmation = getpass.getpass("再次输入密码: ")
    if password != confirmation:
        raise SystemExit("两次输入的密码不一致。")

    try:
        save_credentials(
            username,
            email,
            password,
            overwrite=exists,
        )
    except (ValueError, FileExistsError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"管理员凭据已加密保存到 {ADMIN_CREDENTIALS_FILE}")


if __name__ == "__main__":
    main()
