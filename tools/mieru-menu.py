#!/usr/bin/env python3
"""Interactive installer and manager for upstream Mieru (mita)."""

import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote, urlencode

UPSTREAM = "enfein/mieru"
STATE = Path("/var/lib/mieru-menu")
NODE = STATE / "node.json"
PENDING = STATE / "pending.json"
CLIENT = STATE / "mieru-client.json"
MITA_CONFIG = Path("/etc/mita/server.conf.pb")


def run(*args):
    subprocess.run(args, check=True)


def latest():
    print("正在查询 GitHub 官方最新版（最多 15 秒）…", flush=True)
    url = f"https://api.github.com/repos/{UPSTREAM}/releases/latest"
    try:
        output = subprocess.check_output([
            "curl", "--fail", "--silent", "--show-error", "--connect-timeout", "8",
            "--max-time", "15", "--user-agent", "mieru-menu", url,
        ])
    except subprocess.CalledProcessError as error:
        raise ValueError(f"无法查询 api.github.com（curl 错误 {error.returncode}）") from error
    release = json.loads(output)
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", release["tag_name"]):
        raise ValueError("上游版本格式有变")
    return release


def installed():
    if not shutil.which("mita"):
        return None
    output = subprocess.check_output(["mita", "version"], text=True)
    match = re.search(r"\d+\.\d+\.\d+", output)
    if not match:
        raise ValueError("无法识别本机 mita 版本")
    return match[0]


def package(release):
    version = release["tag_name"][1:]
    arch = {"x86_64": ("amd64", "x86_64"), "aarch64": ("arm64", "aarch64")}.get(platform.machine())
    if not arch:
        raise ValueError("支持 x86_64 和 aarch64")
    if shutil.which("dpkg"):
        name = f"mita_{version}_{arch[0]}.deb"
        command = ["dpkg", "-i"]
    elif shutil.which("rpm"):
        name = f"mita-{version}-1.{arch[1]}.rpm"
        command = ["rpm", "-Uvh", "--replacepkgs"]
    else:
        raise ValueError("需要 Debian/Ubuntu 或 RPM 系发行版")

    assets = {asset["name"]: asset for asset in release["assets"]}
    if name not in assets:
        raise ValueError(f"上游尚未发布 {name}")
    url = f"https://github.com/{UPSTREAM}/releases/download/{release['tag_name']}/{name}"
    with tempfile.TemporaryDirectory(dir=STATE) as directory:
        path = Path(directory) / name
        print(f"正在下载官方安装包 {name}（最多 120 秒）…", flush=True)
        try:
            run("curl", "--fail", "--show-error", "--location", "--progress-bar",
                "--connect-timeout", "8", "--max-time", "120", "--output", str(path), url)
        except subprocess.CalledProcessError as error:
            raise ValueError(f"无法下载 GitHub Release 安装包（curl 错误 {error.returncode}）") from error
        print("正在校验 SHA-256…", flush=True)
        digest = hashlib.sha256()
        with path.open("rb") as package_file:
            for block in iter(lambda: package_file.read(1024 * 1024), b""):
                digest.update(block)
        if assets[name].get("digest") != "sha256:" + digest.hexdigest():
            raise ValueError("安装包 SHA-256 校验失败或官方未提供校验值")
        print("正在安装已校验的软件包…", flush=True)
        run(*command, str(path))


def read_node():
    return json.loads(NODE.read_text()) if NODE.exists() else None


def save(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def address(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        value = value.rstrip(".").encode("idna").decode("ascii")
        label = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
        if len(value) > 253 or not re.fullmatch(rf"{label}(?:\.{label})+", value):
            raise ValueError("请输入公网 IP 或域名，省略协议和端口")
        return value


def prompt_node(previous):
    default_host = previous["host"] if previous else ""
    default_port = previous["binding"]["port"] if previous else 8443
    default_protocol = previous["binding"]["protocol"] if previous else "TCP"
    host = address(input(f"公网 IP 或域名 [{default_host}] ").strip() or default_host)
    port = int(input(f"节点端口 [{default_port}] ").strip() or default_port)
    protocol = (input(f"协议 TCP/UDP [{default_protocol}] ").strip() or default_protocol).upper()
    if not 1025 <= port <= 65535 or protocol not in {"TCP", "UDP"}:
        raise ValueError("端口应为 1025–65535，协议应为 TCP 或 UDP")
    user = previous["user"] if previous else {"name": "mieru", "password": secrets.token_urlsafe(24)}
    if previous and input("生成新密码？旧节点将失效 [y/N] ").strip().lower() == "y":
        user = {"name": user["name"], "password": secrets.token_urlsafe(24)}
    return {"host": host, "binding": {"port": port, "protocol": protocol}, "user": user}


def wait_status(expected=None):
    for _ in range(20):
        status = subprocess.run(["mita", "status"], capture_output=True, text=True)
        if status.returncode == 0 and (expected is None or expected in status.stdout):
            return
        time.sleep(0.5)
    raise ValueError("mita 未就绪，请用 journalctl -u mita 查看日志")


def restart():
    print("正在重启 mita 并检查运行状态…", flush=True)
    run("systemctl", "restart", "mita")
    wait_status('"RUNNING"')


def configure(node):
    print("正在写入节点配置…", flush=True)
    previous = read_node()
    with tempfile.TemporaryDirectory(dir=STATE) as directory:
        path = Path(directory) / "server.json"

        def apply(value):
            save(path, {"users": [value["user"]], "portBindings": [value["binding"]],
                        "loggingLevel": "INFO", "mtu": 1400})
            run("mita", "replace", "config", str(path))

        run("systemctl", "enable", "--now", "mita")
        wait_status()
        save(PENDING, node)
        try:
            apply(node)
            restart()
        except (ValueError, subprocess.CalledProcessError):
            if previous:
                apply(previous)
                restart()
                PENDING.unlink()
            raise
        save(NODE, node)
        PENDING.unlink()


def export(node, clipboard=False):
    user, binding = node["user"], node["binding"]
    host = node["host"]
    try:
        ipaddress.ip_address(host)
        host_field = "ipAddress"
    except ValueError:
        host_field = "domainName"
    authority = f"[{host}]" if ":" in host else host
    query = urlencode({"port": binding["port"], "protocol": binding["protocol"], "profile": "Mieru"})
    link = f"mierus://{quote(user['name'], safe='')}:{quote(user['password'], safe='')}@{authority}?{query}"
    save(CLIENT, {
        "profiles": [{
            "profileName": "Mieru", "user": user,
            "servers": [{host_field: host, "portBindings": [binding]}],
            "handshakeMode": "HANDSHAKE_STANDARD",
        }],
        "activeProfile": "Mieru", "rpcPort": 8964, "socks5Port": 1080,
        "socks5ListenLAN": False,
    })
    print(f"\n{link}\n")
    print(f"v2rayN 自定义配置（mieru 核心） {CLIENT}")
    print(f"请在 VPS 防火墙及云安全组放行 {binding['port']}/{binding['protocol'].lower()}")
    if clipboard:
        encoded = base64.b64encode(link.encode()).decode()
        print(f"\x1b]52;c;{encoded}\x07", end="", flush=True)
        print("已发送剪贴板请求。若终端未复制，可选中上方整行链接。")


def install_or_upgrade():
    release = latest()
    current = installed()
    version = release["tag_name"][1:]
    node = read_node()
    pending = json.loads(PENDING.read_text()) if PENDING.exists() else None
    if not node and not pending and (current or MITA_CONFIG.exists()):
        raise ValueError("发现已有 mita 配置，请用原管理方式维护")
    node = pending or node or prompt_node(None)
    if current and tuple(map(int, current.split("."))) > tuple(map(int, version.split("."))):
        raise ValueError(f"已装 {current} 比上游正式版 {version} 更新")
    if not NODE.exists() and not pending:
        save(PENDING, node)
    if current != version:
        package(release)
    if pending or not NODE.exists() or current is None:
        configure(node)
    elif current != version:
        restart()
    else:
        print(f"已安装最新正式版 {version}")
    export(node)


def uninstall():
    if input("卸载 mita？菜单保存的节点信息将保留 [y/N] ").strip().lower() == "y":
        if shutil.which("dpkg"):
            run("dpkg", "-r", "mita")
        else:
            run("rpm", "-e", "mita")


def menu():
    try:
        print(f"本机版本 {installed() or '未安装'} ｜上游正式版 {latest()['tag_name']}")
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
        print(f"版本检查失败，仍可使用管理功能。{error}")
    while True:
        print("""
Mieru VPS 管理菜单
1. 安装最新版
2. 卸载
3. 启动、停止、重启或查看状态
4. 修改节点配置
5. 显示节点配置及链接
6. 查询并更新至最新正式版
7. 复制节点链接（终端剪贴板）
0. 退出""")
        try:
            choice = input("请选择 ").strip()
            if choice == "0":
                return
            if choice in {"1", "6"}:
                install_or_upgrade()
            elif choice == "2":
                uninstall()
            elif choice == "3":
                commands = {"1": "start", "2": "stop", "3": "restart", "4": "status"}
                action = commands.get(input("1 启动 / 2 停止 / 3 重启 / 4 状态 ").strip())
                if action == "status":
                    run("mita", "status")
                elif action:
                    run("systemctl", action, "mita")
            elif choice in {"4", "5", "7"}:
                node = read_node()
                if node is None:
                    raise ValueError("请先安装并配置节点")
                if choice == "4":
                    node = prompt_node(node)
                    configure(node)
                export(node, clipboard=choice == "7")
            else:
                print("请输入菜单编号")
        except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
            print(f"操作失败。{error}")
        except (EOFError, KeyboardInterrupt):
            print()
            return


def main():
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise SystemExit("请在使用 systemd 的 Linux VPS 上以 root 或 sudo 运行")
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "menu.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("另一个菜单实例正在运行")
        menu()


if __name__ == "__main__":
    main()
