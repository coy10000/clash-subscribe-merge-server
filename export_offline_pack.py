#!/usr/bin/env python3
"""生成离线规则包: 供无法连接订阅服务器所在局域网的客户端临时更新规则。

用法:  SUB_URL=http://服务器IP:端口/sub python export_offline_pack.py
      (默认 http://127.0.0.1:8080/sub, 配合本仓库 server.py 使用)

产物: <out_dir>/offline_pack_YYYYMMDD_HHMMSS.zip
  - merged.yaml        当前订阅输出(与在线 /sub 一致)
  - ruleset/*.mrs      订阅引用的规则集(按 path 版本名, 如 geolocation-!cn.v1.mrs)
  - README.txt         客户端安装说明

客户端用法(见包内 README): mrs 文件放入其 Clash Verge ruleset 目录后,
本地导入 merged.yaml; 内核检测到 path 本地文件存在即不访问订阅服务器 URL。
"""
import urllib.request, re, os, zipfile, datetime, sys
from urllib.parse import urlsplit

SUB_URL = os.environ.get("SUB_URL", "http://127.0.0.1:8080/sub")
SERVER_HOST = urlsplit(SUB_URL).netloc
APP_DATA = os.environ.get("APPDATA", "")
RULESET_DIR = os.path.join(APP_DATA, "io.github.clash-verge-rev.clash-verge-rev", "ruleset")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def fetch_sub():
    req = urllib.request.Request(SUB_URL, headers={"User-Agent": "clash-verge/v2.0"})
    return urllib.request.urlopen(req, timeout=15).read().decode("utf8", "ignore")


def collect_rulesets(txt):
    """从订阅文本提取 url(path 相邻): url 指向 /ruleset/<fname>, path 为本地版本名。
    返回 [(fname_src, path_basename), ...]"""
    items = []
    for m in re.finditer(r"url:\s*(http://\S+/ruleset/([\w!.\-]+))\s*\r?\n\s*path:\s*(\S+)", txt):
        fname_src, path_val = m.group(2), m.group(3)
        path_base = path_val.replace("\\", "/").rstrip("/").split("/")[-1]
        if path_base:
            items.append((fname_src, path_base))
    # 去重保序
    seen, out = set(), []
    for a, b in items:
        if (a, b) not in seen:
            seen.add((a, b))
            out.append((a, b))
    return out


def build_pack():
    txt = fetch_sub()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(OUT_DIR, f"offline_pack_{stamp}.zip")

    # 收集规则集源文件与目标版本名
    items = collect_rulesets(txt)
    missing = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("merged.yaml", txt)
        for fname_src, path_base in items:
            src = os.path.join(RULESET_DIR, fname_src)
            if not os.path.isfile(src):
                missing.append(fname_src)
                continue
            z.write(src, f"ruleset/{path_base}")
        readme = make_readme(stamp, len(items), missing)
        z.writestr("README.txt", readme)

    print(f"订阅输出: {len(txt)} 字节")
    print(f"规则集: {len(items)} 个, 缺失: {missing or '无'}")
    print(f"已生成: {zip_path} ({os.path.getsize(zip_path)} 字节)")
    return zip_path, missing


def make_readme(stamp, n_rules, missing):
    warn = ""
    if missing:
        warn = ("\n注意: 以下规则集在本机未找到, 包内缺失, 客户端若引用会尝试联网下载:\n  "
                + "\n  ".join(missing) + "\n")
    return f"""离线规则包 - 生成时间 {stamp}
适用: 无法连接订阅服务器局域网({SERVER_HOST})的 Clash Verge Rev 客户端
内容: 订阅配置 {n_rules} 个规则集文件

━━ 安装步骤 ━━
1. 先退出 Clash Verge 内核(托盘 -> 停止内核), 或切换到其它配置
2. 把 ruleset 文件夹里的全部 .mrs 文件复制到客户端:
   %APPDATA%\\io.github.clash-verge-rev.clash-verge-rev\\ruleset\\
   (覆盖同名文件; 若目录不存在则新建)
3. Clash Verge -> 订阅(配置) -> 导入配置 -> 选择本包内的 merged.yaml
4. 切换到新导入的配置 -> 完成
   内核加载时规则集走本地文件, 不需要访问订阅服务器

━━ 验证 ━━
打开连接页访问任一国内站(如 1688.com): 链路显示"国内服务/DIRECT",
规则显示 GeoSite:cn 即正常。

━━ 恢复在线订阅 ━━
客户端能连接订阅服务器后: 删除本地导入的这个配置, 重新添加订阅
URL http://{SERVER_HOST} 即可恢复自动更新。
{warn}"""


if __name__ == "__main__":
    try:
        build_pack()
    except Exception as e:
        print(f"打包失败: {e}", file=sys.stderr)
        sys.exit(1)
