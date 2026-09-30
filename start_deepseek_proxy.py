#!/usr/bin/env python3
"""启动本地 deepseek-web2api 代理。

从 config.ini 的 [deepseek_web2api] 节读取 DeepSeek 账号凭证(token/cookies)与代理地址,
注入对应环境变量后启动 deepseek-web2api/server.py。

用法:
    python start_deepseek_proxy.py [-c config.ini]

凭证优先级: config.ini 的 token/cookies > 已有环境变量 > deepseek-web2api/.env
"""
import argparse
import configparser
import os
import subprocess
import sys
from urllib.parse import urlparse

SECTION = "deepseek_web2api"


def load_section(config_path: str) -> dict:
    config = configparser.ConfigParser()
    if not config.read(config_path, encoding="utf-8"):
        return {}
    if not config.has_section(SECTION):
        return {}
    return dict(config.items(SECTION))


def build_env(section: dict) -> dict:
    env = dict(os.environ)

    token = (section.get("token") or "").strip()
    cookies = (section.get("cookies") or "").strip()

    if token:
        env["DEEPSEEK_TOKEN_1"] = token
        env["DEEPSEEK_TOKEN"] = token
    if cookies:
        env["DEEPSEEK_COOKIES_1"] = cookies
        env["DEEPSEEK_COOKIES"] = cookies

    if not token or not cookies:
        print(
            "警告: config.ini [{}] 的 token/cookies 未填写, 将回退使用环境变量或 deepseek-web2api/.env".format(SECTION),
            file=sys.stderr,
        )

    endpoint = (section.get("endpoint") or "").strip()
    if endpoint:
        try:
            port = urlparse(endpoint).port
            if port:
                env.setdefault("PORT", str(port))
        except ValueError:
            pass

    env.setdefault("HOST", "127.0.0.1")
    env.setdefault("PORT", "8080")
    env.setdefault("ALLOW_UNAUTHENTICATED_API", "true")
    env.setdefault("LOG_FORMAT", "text")

    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本地 deepseek-web2api 代理")
    parser.add_argument("-c", "--config", default="config.ini", help="配置文件路径, 默认 config.ini")
    args = parser.parse_args()

    section = load_section(args.config)
    env = build_env(section)

    server_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek-web2api")
    server_script = os.path.join(server_dir, "server.py")
    if not os.path.isfile(server_script):
        print("错误: 未找到 {}".format(server_script), file=sys.stderr)
        return 1

    print("启动 deepseek-web2api: HOST={} PORT={}".format(env["HOST"], env["PORT"]))
    return subprocess.call([sys.executable, "server.py"], cwd=server_dir, env=env)


if __name__ == "__main__":
    sys.exit(main())
