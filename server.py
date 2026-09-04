#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash Verge 订阅合集本地合并分享服务 (server.py)

零配置读取本机 Clash Verge 中已有的订阅(profiles.yaml), 自动合并为一份统一订阅,
通过 HTTP 分享给本机与局域网内的 Clash 客户端使用。

功能特性:
  - 多订阅自动合并: 读取 Clash Verge 已配置的订阅, 合并去重, 自动剔除官网/流量信息节点
  - 节点短名重命名: 冲突自动去重, 便于识别与分组
  - 实测延迟测速: 真实 HTTP 探测(可选 Clash REST API 精确延迟), 后台周期自动测速,
    支持 CPU 负载感知暂停, 保护低配主机
  - 延迟速度分组: 按实测延迟把节点分为 快/中/慢 三组, 客户端默认走最快组
  - 节点屏蔽管理: 自动屏蔽持续不通节点(默认24h)、手动屏蔽/解封、Web 面板可视化管理
  - 规则集本地化: rule-providers 的远程 URL 自动改写为本地/局域网地址, 无外网环境可用
  - 规则集版本化: 规则集文件带版本号(.v{n}), 新版本每天最多发布一次, 避免客户端频繁重载
  - 国内直连兜底: 注入 GEOSITE,cn / GEOIP,CN / 补充域名规则, 防止国内站漏网走代理
  - 双格式输出: Clash YAML 与 Shadowrocket
  - Web 控制台: /web 可视化节点状态与屏蔽管理, REST API: /status /refresh /api/*
  - 端口冲突接管: 检测到旧实例占用端口时, 交互确认后自动接管
  - 可选管理令牌: ADMIN_TOKEN 鉴权(订阅端点不受限)

运行环境: Windows + Clash Verge Rev(或兼容目录结构的 Clash 客户端)
依赖: 仅 PyYAML(缺失时自动安装)
Web地址: http://<本机IP>:8080/web
"""
import os
import sys
import re
import json
import base64
import socket
import time
import logging
import threading
import http.server
import socketserver
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, quote, urlparse, parse_qs
from collections import defaultdict
# ==================== 依赖检查 ====================
def ensure_deps():
    try:
        import yaml
        return yaml
    except ImportError:
        print("正在安装依赖 PyYAML ...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyyaml"],
            check=True, capture_output=True,
        )
        import yaml
        print("依赖安装成功")
        return yaml
yaml = ensure_deps()
# ==================== 基础配置区 ====================
PORT = 8080
HOST = "0.0.0.0"
# 可选管理令牌: 留空=关闭鉴权; 设置后 /web /status /refresh /api/* 需 ?token=xxx
# 订阅地址 (/, /sub, /shadowrocket, /ruleset/) 不受限, 客户端照常拉取
ADMIN_TOKEN = ""
APPDATA = os.environ.get("APPDATA", "")
CLASH_DIR = os.path.join(
    APPDATA, "io.github.clash-verge-rev.clash-verge-rev"
)
PROFILES_DIR = os.path.join(CLASH_DIR, "profiles")
RULESET_DIR = os.path.join(CLASH_DIR, "ruleset")
BLOCKLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocklist.json")
# 规则集版本状态: 记录每个 MRS 文件的发布版本号, 限制新版本每天最多发布一次
RULESET_VERSIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ruleset_versions.json")
RULESET_DAILY_LIMIT = True   # 规则集新版本每天(自然日)最多发布一次; False=关闭限制(文件一变就发新版)
# 国内直连补充域名: 自定义精简 geosite-cn 规则集常缺失的国内站点 (fake-ip 模式下
# ipcidr 规则集无法兜底域名, 只能靠域名级规则)。按需自行增删。
CN_DIRECT_EXTRA = ["1688.com"]
# 注入 GEOSITE,cn 完整国内域名规则(依赖客户端 Clash Verge/mihomo 的 geosite.dat,
# Verge 默认随 geox-url 下载, 缺失时 mihomo 会报错, 故保留开关可关闭)
CN_GEOSITE_PATCH = True
# 测速配置
TEST_URL = "https://www.gstatic.com/generate_204"
TEST_TIMEOUT = 3            # 单节点探测超时(秒)
TEST_CONCURRENCY = 5        # 并发探测数(mihomo delay API 有并发限流, 过高会批量假失败)
AUTO_BLOCK_FAIL_CNT = 3     # 连续失败多少次自动屏蔽
BLOCK_COOLDOWN_SEC = 86400   # 自动屏蔽节点多久后解禁(秒)
ENABLE_AUTO_BLOCK = True    # 是否开启超时自动屏蔽
BLOCK_FAILED_DURATION_SEC = 86400  # "屏蔽不通节点"按钮的临时屏蔽时长(秒, 24小时)
SPEEDTEST_INTERVAL_SEC = 21600  # 后台定时测速间隔(秒), 6小时一轮; 0=不自动测速
AUTO_TEST_CONCURRENCY = 1   # 非主动(后台自动)测速并发: 1=串行, 不影响服务器
CPU_LOAD_LIMIT = 70         # 系统CPU使用率(%): 后台测速期间超过此值则暂停
CPU_CHECK_EVERY = 10        # 后台测速每测N个节点检查一次CPU
CPU_PAUSE_SEC = 300         # CPU负载过高时测速暂停的秒数(5分钟), 暂停后继续本轮
AUTO_TEST_DELAY_SEC = 0.5   # 非主动测速每测完1个节点的间隔(秒), 低频慢速测完所有节点
SPEED_FIRST_GROUP = True  # 速度优先: 按延迟把全部节点分成快/中/慢三组(不再按订阅分组), 节点选择默认选中快组
# 可选: Clash external-controller (真实 HTTP 延迟测速)
# 在 Clash Verge 中开启 "允许局域网/外部控制" 后可填, 如 "127.0.0.1:9097"
CLASH_CONTROLLER = "127.0.0.1:9097"
CLASH_SECRET = "9097"
# ==================== 全局状态 ====================
merged_yaml_bytes = None
merged_sr_bytes = None
subscription_userinfo = None
stats = {}
merge_lock = threading.Lock()
speedtest_lock = threading.Lock()
blocklist_lock = threading.Lock()
_last_merge_ts = 0.0
_last_speedtest_ts = 0
# node_name -> {"delay":float|None, "fail_cnt":int, "server":str, "port":int}
node_status = dict()
NODE_STATUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_status.json")
speedtest_progress = {"running": False, "done": 0, "total": 0, "last_ts": 0.0}
_manual_break = threading.Event()  # 手动测速请求打断后台测速的标志
blocklist = {"permanent": set(), "temp": dict()}
# 规则集版本状态: fname -> {"version": int, "last_bump": "YYYY-MM-DD", "fp": "mtime|size"}
ruleset_versions = dict()
# ==================== 日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("clash-merge")
# ==================== 测速状态持久化 ====================
def load_node_status():
    """重启后恢复上次测速缓存(延迟/失败计数等), 避免重启后数据清空"""
    global node_status
    if not os.path.exists(NODE_STATUS_PATH):
        return
    try:
        with open(NODE_STATUS_PATH, "r", encoding="utf8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            node_status = {k: dict(v) for k, v in data.items()}
            log.info(f"加载测速缓存: {len(node_status)} 个节点")
    except Exception as e:
        log.warning(f"测速缓存读取失败: {e}")

def save_node_status():
    try:
        with open(NODE_STATUS_PATH, "w", encoding="utf8") as f:
            json.dump(node_status, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.warning(f"测速缓存保存失败: {e}")
# ==================== 黑名单持久化 ====================
def load_blocklist():
    global blocklist
    if not os.path.exists(BLOCKLIST_PATH):
        blocklist = {"permanent": [], "temp": {}}
        save_blocklist()
    try:
        with open(BLOCKLIST_PATH, "r", encoding="utf8") as f:
            raw = json.load(f)
        blocklist["permanent"] = set(raw.get("permanent", []))
        blocklist["temp"] = raw.get("temp", {})
        log.info(f"加载黑名单:永久{len(blocklist['permanent'])}条,临时{len(blocklist['temp'])}条")
    except Exception as e:
        log.warning(f"黑名单读取失败:{e},使用空黑名单")
        blocklist = {"permanent": set(), "temp": dict()}

def save_blocklist():
    out = {
        "permanent": sorted(blocklist["permanent"]),
        "temp": blocklist["temp"],
    }
    with open(BLOCKLIST_PATH, "w", encoding="utf8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
# ==================== 规则集版本状态持久化 ====================
def load_ruleset_versions():
    """重启后恢复规则集版本状态, 避免重启导致版本号重置(会触发客户端全量重下)"""
    global ruleset_versions
    if not os.path.exists(RULESET_VERSIONS_PATH):
        return
    try:
        with open(RULESET_VERSIONS_PATH, "r", encoding="utf8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            ruleset_versions = data
            log.info(f"加载规则集版本状态: {len(ruleset_versions)} 个规则集")
    except Exception as e:
        log.warning(f"规则集版本状态读取失败: {e}")

def save_ruleset_versions():
    try:
        with open(RULESET_VERSIONS_PATH, "w", encoding="utf8") as f:
            json.dump(ruleset_versions, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.warning(f"规则集版本状态保存失败: {e}")

def _ruleset_version_for(fname, mtime, size):
    """决定规则集的发布版本号: 文件未变→沿用旧版本; 变了→每天(自然日)最多发布一次新版本。
    返回 (version, bumped): bumped=True 表示本次发布了新版本(客户端将重下该规则集)"""
    today = time.strftime("%Y-%m-%d")
    fp = f"{int(mtime)}|{size}"
    rec = ruleset_versions.get(fname)
    if rec is None:
        # 首次出现: 发布 v1
        ruleset_versions[fname] = {"version": 1, "last_bump": today, "fp": fp}
        save_ruleset_versions()
        return 1, True
    if rec.get("fp") == fp:
        # 文件未变, 沿用当前版本
        return rec.get("version", 1), False
    if RULESET_DAILY_LIMIT and rec.get("last_bump") == today:
        # 今天已发布过: 保持版本号(客户端不重下), 明天合并时再发布
        return rec.get("version", 1), False
    ver = rec.get("version", 1) + 1
    rec["version"] = ver
    rec["last_bump"] = today
    rec["fp"] = fp
    save_ruleset_versions()
    return ver, True

def is_node_blocked(node_name):
    now = time.time()
    if node_name in blocklist["permanent"]:
        return True
    expire = blocklist["temp"].get(node_name, 0)
    if expire > now:
        return True
    elif node_name in blocklist["temp"]:
        del blocklist["temp"][node_name]
        save_blocklist()
    return False

def block_node(node_name, duration_sec=0):
    """duration_sec=0永久屏蔽, >0临时屏蔽多少秒"""
    now = time.time()
    with blocklist_lock:
        if duration_sec <= 0:
            blocklist["permanent"].add(node_name)
        else:
            blocklist["temp"][node_name] = now + duration_sec
        save_blocklist()
    s = node_status.get(node_name)
    if s is not None:
        s.setdefault("blocked_at", now)

def unblock_node(node_name):
    """解除屏蔽并重置失败计数(M2修复)"""
    with blocklist_lock:
        if node_name in blocklist["permanent"]:
            blocklist["permanent"].discard(node_name)
        if node_name in blocklist["temp"]:
            del blocklist["temp"][node_name]
        save_blocklist()
    s = node_status.get(node_name)
    if s is not None:
        s["fail_cnt"] = 0
# ==================== 工具函数 ====================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def clean_name(name):
    if not name:
        return ""
    return name.replace("\\", "").replace('"', "").strip()

def _usage_bar(pct, width=10):
    pct = min(max(pct, 0), 100)
    if pct >= 100:
        return "[" + "=" * (width - 1) + ">]"
    filled = round(pct / 100 * width)
    if filled == 0:
        return "[" + "-" * width + "]"
    if filled == width:
        return "[" + "=" * (width - 1) + ">]"
    return "[" + "=" * (filled - 1) + ">" + "-" * (width - filled) + "]"
# ==================== 节点名缩短 & 过滤 ====================
INFO_KEYWORDS = [
    "官网", "域名", "www.", "http", "剩余流量", "距离下次重置",
    "套餐到期", "更新app", "频道", "请更新",
]
COUNTRY_ALIAS = {
    "狮城": "新加坡",
    "澳洲": "澳大利亚",
    "印尼": "印度尼西亚",
}
PROVIDER_ABBR = {
    "YinYun.Ltd":     "YY",
    "Platinum Package": "PL",
    "XSUS":            "XS",
}
COUNTRIES = [
    "印度尼西亚", "哈萨克斯坦", "马来西亚", "澳大利亚", "孟加拉国",
    "菲律宾", "柬埔寨", "阿联酋", "立陶宛", "拉脱维亚", "格鲁吉亚",
    "罗马尼亚", "保加利亚", "新加坡", "亚美尼亚", "尼日利亚", "阿根廷",
    "加拿大", "墨西哥", "土耳其", "乌克兰", "俄罗斯", "奥地利",
    "葡萄牙", "以色列", "西班牙", "意大利", "南非", "瑞士",
    "芬兰", "瑞典", "捷克", "波兰", "法国", "德国",
    "荷兰", "挪威", "英国", "泰国", "越南", "印度",
    "巴西", "冰岛",
    "香港", "台湾", "日本", "韩国", "美国",
]
def _is_info_node(name):
    for kw in INFO_KEYWORDS:
        if kw.lower() in name.lower():
            return True
    return False
def _has_bak(name):
    return "ᴮᴬᴷ" in name
def _extract_parts(raw_name):
    s = raw_name
    s = re.sub(r"[\U0001F000-\U0001FFFF]", "", s)
    s = re.sub(r"\[.*?\]", "", s)
    s = re.sub(
        r"\s*[-|]\s*(ANYTLS|TROJAN|VMESS|VLESS|HY2|SSR?|HTTP|SOCKS5|H2|GRPC|WS|TCP)\b",
        "", s, flags=re.I,
    )
    s = re.sub(r"\s*\d+\.?\d*\s*倍率", "", s)
    s = re.sub(r"\s*(BGP|IIJ|TRI|CUG)(\d+)", r" \2", s, flags=re.I)
    s = re.sub(r"\s*(PCCW|TEST)\b", "", s, flags=re.I)
    s = re.sub(r"\s+TR\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    for alias, canonical in COUNTRY_ALIAS.items():
        if alias in s:
            s = s.replace(alias, canonical)
    country = ""
    for cn in COUNTRIES:
        if cn in s:
            country = cn
            s = s.replace(cn, "", 1).strip()
            break
    num = ""
    m = re.search(r"(\d+)", s)
    if m:
        num = m.group(1)
        s = re.sub(r"\d+", "", s, count=1).strip()
    marker = ""
    mk = re.search(r"[（(]([^)]+)[）)]", s)
    if mk and mk.group(1) in ("T", "R"):
        marker = f"({mk.group(1)})"
    if "优选" in s:
        marker = "优" + marker
    if "家宽" in s:
        marker += "家"
    if "魔法" in s:
        country = "魔法"
    if not country:
        parts = s.split()
        country = parts[0] if parts else s
        if len(country) > 4:
            country = country[:4]
    return country, num, marker
def shorten_proxies(proxies, provider_key):
    abbr = PROVIDER_ABBR.get(provider_key, provider_key[:4])
    result = []
    name_map = {}
    seen = {}
    for p in proxies:
        name = p["name"]
        if _has_bak(name) or _is_info_node(name):
            continue
        country, num, marker = _extract_parts(name)
        key = (country, abbr)
        if key not in seen:
            seen[key] = set()
        if not num and not marker:
            n = 1
            while str(n).zfill(2) in seen[key]:
                n += 1
            num = str(n).zfill(2)
        uid = num + marker
        if uid in seen[key]:
            n = 2
            while f"{num}-{n}{marker}" in seen[key]:
                n += 1
            num = f"{num}-{n}"
            uid = num + marker
        seen[key].add(uid)
        new_name = f"{country}{num}{marker} {abbr}"
        name_map[name] = new_name
        p["name"] = new_name
        result.append(p)
    return result, name_map
# ==================== Shadowrocket 格式转换 ====================
def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _clash_to_sr_uri(proxy):
    ptype = (proxy.get("type") or "").lower()
    name = quote(proxy.get("name", "unnamed"), safe="")
    server = proxy.get("server", "")
    port = proxy.get("port", 0)
    if not server or not port:
        return None
    try:
        if ptype == "ss":
            method = proxy.get("cipher", "aes-256-gcm")
            pwd = proxy.get("password", "")
            userinfo = _b64url(f"{method}:{pwd}".encode())
            return f"ss://{userinfo}@{server}:{port}#{name}"
        elif ptype == "ssr":
            method = proxy.get("cipher", "aes-256-cfb")
            pwd = proxy.get("password", "")
            protocol = proxy.get("protocol", "origin")
            proto_param = proxy.get("protocol-param", "")
            obfs = proxy.get("obfs", "plain")
            obfs_param = proxy.get("obfs-param", "")
            pwd_b64 = base64.b64encode(pwd.encode()).decode()
            main = f"{server}:{port}:{protocol}:{method}:{obfs}:{pwd_b64}/"
            params = []
            if obfs_param:
                params.append(f"obfsparam={quote(obfs_param, safe='')}")
            if proto_param:
                params.append(f"protoparam={quote(proto_param, safe='')}")
            remarks = quote(proxy.get("name", ""), safe="")
            if remarks:
                params.append(f"remarks={remarks}")
            if proxy.get("udp"):
                params.append("udp=1")
            params_str = "&".join(params)
            body = main + ("?" + params_str if params else "")
            return "ssr://" + base64.b64encode(body.encode()).decode()
        elif ptype == "vmess":
            net = proxy.get("network", "tcp")
            vm = {"v":"2","ps":proxy.get("name","unnamed"),"add":server,
                  "port":str(port),"id":proxy.get("uuid",""),
                  "aid":str(proxy.get("alterId",0)),"scy":proxy.get("cipher","auto"),
                  "net":net,"type":proxy.get("type","none") if net=="tcp" else "",
                  "host":"","path":"","tls":""}
            if net in ("ws","h2"):
                ws = proxy.get("ws-opts",{}) or {}
                h2 = proxy.get("h2-opts",{}) or {}
                opts = ws if net=="ws" else h2
                vm["host"] = opts.get("headers",{}).get("Host","")
                vm["path"] = opts.get("path","")
                vm["tls"] = "tls" if proxy.get("tls") else ""
                if proxy.get("servername"):
                    vm["host"] = proxy["servername"]
            elif net == "grpc":
                grpc = proxy.get("grpc-opts",{}) or {}
                vm["path"] = grpc.get("grpc-service-name","")
                vm["tls"] = "tls" if proxy.get("tls") else ""
            elif net == "http":
                h1 = proxy.get("http-opts",{}) or {}
                hops = h1.get("headers",{}) or {}
                vm["host"] = ",".join(hops.get("Host",[])) if isinstance(hops.get("Host"),list) else hops.get("Host","")
                vm["path"] = h1.get("path","")
            elif proxy.get("tls"):
                vm["tls"] = "tls"
            return "vmess://"+base64.b64encode(json.dumps(vm,ensure_ascii=False).encode()).decode()
        elif ptype == "trojan":
            pwd = quote(proxy.get("password",""), safe="")
            params = []
            if proxy.get("sni"):
                params.append(f"sni={proxy['sni']}")
            elif proxy.get("servername"):
                params.append(f"sni={proxy['servername']}")
            if proxy.get("skip-cert-verify"):
                params.append("allowInsecure=1")
            net = proxy.get("network","tcp")
            if net in ("ws","grpc"):
                params.append(f"type={net}")
                if net == "ws":
                    ws = proxy.get("ws-opts",{}) or {}
                    params.append(f"path={quote(ws.get('path','/'), safe='')}")
                    host = ws.get("headers",{}).get("Host","") or proxy.get("servername","")
                    if host:
                        params.append(f"host={host}")
                else:
                    grpc = proxy.get("grpc-opts",{}) or {}
                    params.append(f"serviceName={grpc.get('grpc-service-name','')}")
            if proxy.get("tls"):
                params.append("security=tls")
            qs = "&".join(params)
            uri = f"trojan://{pwd}@{server}:{port}"
            if qs:
                uri += f"?{qs}"
            uri += f"#{name}"
            return uri
        elif ptype == "vless":
            uid = proxy.get("uuid","")
            params = [f"encryption={proxy.get('encryption','none')}"]
            net = proxy.get("network","tcp")
            params.append(f"type={net}")
            if proxy.get("tls"):
                params.append("security=tls")
            if proxy.get("servername"):
                params.append(f"sni={proxy['servername']}")
            if net == "ws":
                ws = proxy.get("ws-opts",{}) or {}
                params.append(f"path={quote(ws.get('path','/'), safe='')}")
                host = ws.get("headers",{}).get("Host","") or proxy.get("servername","")
                if host:
                    params.append(f"host={host}")
            elif net == "grpc":
                grpc = proxy.get("grpc-opts",{}) or {}
                params.append(f"serviceName={grpc.get('grpc-service-name','')}")
            if proxy.get("flow"):
                params.append(f"flow={quote(proxy['flow'], safe='')}")
            qs = "&".join(params)
            return f"vless://{uid}@{server}:{port}?{qs}#{name}"
        elif ptype in ("http","socks5"):
            user = quote(proxy.get("username",""), safe="")
            pwd = quote(proxy.get("password",""), safe="")
            uinfo = f"{user}:{pwd}@" if user else ""
            return f"{ptype}://{uinfo}{server}:{port}#{name}"
        elif ptype == "hysteria2":
            pwd = quote(proxy.get("password","") or proxy.get("auth",""), safe="")
            params = []
            if proxy.get("sni"):
                params.append(f"sni={proxy['sni']}")
            if proxy.get("skip-cert-verify"):
                params.append("insecure=1")
            qs = "&".join(params)
            uri = f"hysteria2://{pwd}@{server}:{port}"
            if qs:
                uri += f"?{qs}"
            uri += f"#{name}"
            return uri
        elif ptype == "tuic":
            uid = proxy.get("uuid","")
            pwd = quote(proxy.get("password",""), safe="")
            params = []
            if proxy.get("sni"):
                params.append(f"sni={proxy['sni']}")
            if proxy.get("skip-cert-verify"):
                params.append("insecure=1")
            params.append(f"congestion_control={proxy.get('congestion-controller','bbr')}")
            alpn = proxy.get("alpn",["h3"])
            params.append(f"alpn={alpn[0] if isinstance(alpn,list) else alpn}")
            qs = "&".join(params)
            uri = f"tuic://{uid}:{pwd}@{server}:{port}"
            if qs:
                uri += f"?{qs}"
            uri += f"#{name}"
            return uri
        elif ptype == "anytls":
            pwd = quote(proxy.get("password",""), safe="")
            params = []
            if proxy.get("sni"):
                params.append(f"sni={proxy['sni']}")
            elif proxy.get("servername"):
                params.append(f"sni={proxy['servername']}")
            if proxy.get("skip-cert-verify"):
                params.append("insecure=1")
            alpn = proxy.get("alpn")
            if alpn:
                params.append(f"alpn={','.join(alpn) if isinstance(alpn,list) else alpn}")
            if proxy.get("udp"):
                params.append("udp=1")
            qs = "&".join(params)
            uri = f"anytls://{pwd}@{server}:{port}"
            if qs:
                uri += f"?{qs}"
            uri += f"#{name}"
            return uri
        else:
            return None
    except Exception as e:
        log.debug(f"SR转换失败 [{proxy.get('name','?')}]: {e}")
        return None
def build_shadowrocket(proxies):
    lines = []
    skipped = 0
    for p in proxies:
        uri = _clash_to_sr_uri(p)
        if uri:
            lines.append(uri)
        else:
            skipped += 1
    content = "\n".join(lines).encode()
    result = base64.b64encode(content)
    log.info(f"Shadowrocket 格式: {len(lines)} 个节点成功, {skipped} 个跳过")
    return result
def read_profiles_meta():
    meta_path = os.path.join(CLASH_DIR, "profiles.yaml")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = yaml.safe_load(f)
    items = meta.get("items", [])
    return [i for i in items if i.get("type") == "remote"]
def read_profile(filename):
    path = os.path.join(PROFILES_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
def _is_self_ref(url, local_ip):
    try:
        u = urlparse(url)
        if u.port != PORT:
            return False
        host = (u.hostname or "").lower()
        return host in ("127.0.0.1","localhost","::1") or host == local_ip
    except Exception:
        return f":{PORT}" in url
# ==================== 合并逻辑 ====================
def do_merge():
    global merged_yaml_bytes, merged_sr_bytes, subscription_userinfo, stats
    global _last_merge_ts, node_status
    with merge_lock:
        local_ip = get_local_ip()
        base_url = f"http://{local_ip}:{PORT}"
        remote_profiles = read_profiles_meta()
        skip_local = []
        filtered = []
        for p in remote_profiles:
            url = p.get("url", "")
            if _is_self_ref(url, local_ip):
                skip_local.append(clean_name(p.get("name")) or p["uid"])
                continue
            filtered.append(p)
        if skip_local:
            log.info(f"已跳过本机自引用订阅: {', '.join(skip_local)}")
        log.info(f"有效远程订阅 {len(filtered)} 个")
        all_configs = []
        for p in filtered:
            name = clean_name(p.get("name")) or p["uid"]
            try:
                cfg = read_profile(p["file"])
                n = len(cfg.get("proxies", []))
                all_configs.append({"uid":p["uid"],"name":name,"config":cfg,
                                    "extra":p.get("extra") or {}})
                log.info(f"  [{name}] {n} 个节点")
            except Exception as e:
                log.error(f"  [{name}] 读取失败: {e}")
        if not all_configs:
            raise ValueError("没有可用的远程订阅")
        base = max(all_configs, key=lambda c: len(c["config"].get("proxies", [])))
        others = [c for c in all_configs if c is not base]
        pg_count = len(base['config'].get('proxy-groups', []))
        log.info(f"基础配置: [{base['name']}] (proxies={len(base['config'].get('proxies',[]))}, groups={pg_count})")
        merged = base["config"]
        proxy_meta = {}  # 短名 -> (server,port), 供被屏蔽节点恢复测试使用
        total_raw = 0
        for c in all_configs:
            raw_proxies = c["config"].get("proxies", [])
            total_raw += len(raw_proxies)
            sp, nm = shorten_proxies(raw_proxies, c["name"])
            for p in sp:
                proxy_meta[p["name"]] = (p.get("server", ""), p.get("port", 0))
            sp = [p for p in sp if not is_node_blocked(p["name"])]
            c["short_proxies"] = sp
            c["name_map"] = nm
        total_short = sum(len(c["short_proxies"]) for c in all_configs)
        log.info(f"过滤 BAK/信息节点+黑名单: {total_raw} -> {total_short}")
        # [C2修复] 先全局去重(基础配置优先保留原名), 再构建 short_names
        all_proxies = []
        existing_names = set()
        new_names = []
        for c in [base] + others:
            for p in c["short_proxies"]:
                name = p["name"]
                while name in existing_names:
                    name += "+"
                p["name"] = name
                existing_names.add(name)
                all_proxies.append(p)
                if c is not base:
                    new_names.append(name)
            c["short_names"] = [p["name"] for p in c["short_proxies"]]
        merged["proxies"] = all_proxies
        log.info(f"合并后总计 {len(all_proxies)} 个节点 (新增 {len(new_names)})")
        base_name_map = base["name_map"]
        sub_groups = []
        for i, c in enumerate(all_configs):
            label = f"节点组{i+1}"
            sub_groups.append({"name":label,"type":"select","proxies":c["short_names"]})
            log.info(f"  {label} ({len(c['short_names'])} 节点)")
        if SPEED_FIRST_GROUP:
            # 速度优先: 不再按订阅(YY/PL/XS)分组, 把全部节点按延迟分成 快/中/慢 三组
            all_names = []
            for c in [base] + others:
                all_names.extend(c["short_names"])
            def _delay_of(m):
                s = node_status.get(m)
                return s.get("delay") if s and s.get("delay") is not None else float("inf")
            all_names.sort(key=_delay_of)
            measured = [m for m in all_names if _delay_of(m) != float("inf")]
            if measured:
                n = len(all_names)
                split1 = max(1, n // 3)
                split2 = max(split1 + 1, (2 * n) // 3)
                groups = [
                    ("节点组1", all_names[:split1]),
                    ("节点组2", all_names[split1:split2]),
                    ("节点组3", all_names[split2:]),
                ]
                sub_groups = [{"name": label, "type": "select", "proxies": members}
                              for label, members in groups]
                log.info(f"按速度分组: 节点组1(快)={len(groups[0][1])}, "
                         f"节点组2(中)={len(groups[1][1])}, 节点组3(慢)={len(groups[2][1])}")
            else:
                log.info("暂无测速数据, 保持按订阅分组")
        sub_names = [g["name"] for g in sub_groups]
        fallback_proxy = sub_names[0] if sub_names else "DIRECT"  # [M4修复]
        original_groups = base["config"].get("proxy-groups", [])
        group_name_set = set(g["name"] for g in original_groups)
        cleaned_groups = []
        bak_removed_total = 0
        for g in original_groups:
            cleaned_proxies = []
            for pn in g.get("proxies", []):
                if pn in base_name_map:
                    newpn = base_name_map[pn]
                    if not is_node_blocked(newpn):
                        cleaned_proxies.append(newpn)
                elif pn in ("DIRECT","REJECT","REJECT-DROP") or pn in group_name_set:
                    cleaned_proxies.append(pn)
                else:
                    bak_removed_total += 1
            if cleaned_proxies:
                cleaned_groups.append(dict(g, proxies=cleaned_proxies))
        if bak_removed_total:
            log.info(f"  从原始代理组中移除 {bak_removed_total} 个已过滤节点引用")
        referenced = set()
        for rule in base["config"].get("rules", []):
            if isinstance(rule, str):
                parts = [p.strip() for p in rule.split(",")]
                if len(parts) >= 2 and parts[-1]:
                    referenced.add(parts[-1])
        for g in cleaned_groups:
            if g["name"] in referenced:
                have = set(g["proxies"])
                add = [x for x in sub_names if x not in have]
                if add:
                    g["proxies"] = g["proxies"] + add
                    log.info(f"已将订阅组注入规则引用组 [{g['name']}]")
        group_by_name = {g["name"]: g for g in cleaned_groups}
        def find_group(kw):
            return next((n for n in group_by_name if kw in n), None)
        auto_actual = find_group("自动选择") or "自动选择"
        node_select_actual = find_group("节点选择") or "节点选择"
        country_buckets = defaultdict(lambda: defaultdict(list))
        for i, c in enumerate(all_configs):
            for nm in c["short_names"]:
                country, _, _ = _extract_parts(nm)
                country_buckets[country or "其他"][i].append(nm)
        ordered_countries = sorted(country_buckets.keys(),
            key=lambda c: -sum(len(v) for v in country_buckets[c].values()))
        interleaved = []
        for country in ordered_countries:
            prov_map = country_buckets[country]
            maxlen = max((len(l) for l in prov_map.values()), default=0)
            for idx in range(maxlen):
                for pi in sorted(prov_map.keys()):
                    if idx < len(prov_map[pi]):
                        interleaved.append(prov_map[pi][idx])
        log.info(f"  节点选择单体节点 {len(interleaved)} 个")
        if find_group("自动选择"):
            ag = group_by_name[auto_actual]
            if ag.get("type") != "url-test":
                ag["type"] = "url-test"
                ag.setdefault("url", "https://www.gstatic.com/generate_204")
                ag.setdefault("interval", 300)
                ag.setdefault("tolerance", 50)
            ag["proxies"] = list(sub_names)
        else:
            cleaned_groups.append({"name":"自动选择","type":"url-test",
                "url":"https://www.gstatic.com/generate_204","interval":300,
                "tolerance":50,"proxies":list(sub_names)})
        node_select_members = interleaved + sub_names + [auto_actual]
        seen = set(); ns_members = []
        for m in node_select_members:
            if m not in seen:
                seen.add(m); ns_members.append(m)
        if node_select_actual in group_by_name:
            ns = group_by_name[node_select_actual]
            ns["proxies"] = ns_members
            ns["now"] = fallback_proxy
        else:
            cleaned_groups.append({"name":node_select_actual,"type":"select",
                "proxies":ns_members,"now":fallback_proxy})
        lw_actual = find_group("漏网之鱼")
        if lw_actual:
            lw = group_by_name[lw_actual]
            lw_members = sub_names + [auto_actual, node_select_actual]
            seen2 = set(); lw_ordered = []
            for m in lw_members:
                if m not in seen2:
                    seen2.add(m); lw_ordered.append(m)
            lw["proxies"] = lw_ordered
            lw["now"] = node_select_actual
        else:
            cleaned_groups.append({"name":"漏网之鱼","type":"select",
                "proxies":[node_select_actual]+sub_names+[auto_actual],
                "now":node_select_actual})
        merged["proxy-groups"] = sub_groups + cleaned_groups
        merged["rules"] = _patch_cn_direct_rules(base["config"].get("rules", []))
        rps = merged.get("rule-providers", {})
        local_count = 0
        missing_files = []
        for rp_name, rp_cfg in rps.items():
            path = rp_cfg.get("path", "")
            if not path:
                continue
            fname = os.path.basename(path.replace("\\", "/"))
            mrs_path = os.path.join(RULESET_DIR, fname)
            if not os.path.isfile(mrs_path):
                # 本地无规则集文件: 保留远程URL并告警, 避免客户端对不可达URL长时间等待后卡住
                missing_files.append(f"{rp_name}:{fname}")
                continue
            st = os.stat(mrs_path)
            ver, bumped = _ruleset_version_for(fname, st.st_mtime, st.st_size)
            stem, ext = os.path.splitext(fname)
            ver_name = f"{stem}.v{ver}{ext}"
            parent = os.path.dirname(path.replace("\\", "/"))
            # url 指向无版本真实文件(服务端只有一份); path 带版本号, 版本号变化时客户端本地无此文件→自动重下
            rp_cfg["url"] = f"{base_url}/ruleset/{fname}"
            rp_cfg["path"] = f"{parent}/{ver_name}" if parent else ver_name
            local_count += 1
            if bumped:
                log.info(f"规则集发布新版本: {fname} -> v{ver} (今日首次发布)")
        merged["rule-providers"] = rps
        if local_count:
            log.info(f"已本地化 {local_count} 个规则集文件 (MRS)")
        if missing_files:
            log.warning(f"本地缺少规则集文件, 保留远程URL: {', '.join(missing_files)}")
        yaml_str = yaml.dump(merged, allow_unicode=True,
                             default_flow_style=False, sort_keys=False)
        merged_yaml_bytes = yaml_str.encode("utf-8")
        merged_sr_bytes = build_shadowrocket(all_proxies)
        def fmt_size(b):
            if b <= 0:
                return "-"
            if b < 1024:
                return f"{b} B"
            for u in ("KB","MB","GB","TB"):
                b /= 1024
                if b < 1024:
                    return f"{b:.1f} {u}" if b < 100 else f"{int(b)} {u}"
            return f"{b:.1f} PB"
        per_profile_traffic = []
        total_up = 0
        total_down = 0
        total_all = 0
        YINYUN_DEFAULT_TOTAL = 150 * 1024**3
        for c in all_configs:
            extra = c["extra"]
            up = extra.get("upload", 0)
            down = extra.get("download", 0)
            t = extra.get("total", 0)
            expire = extra.get("expire")
            if "yinyun" in c["name"].lower() and t <= 0:
                t = YINYUN_DEFAULT_TOTAL
            remaining = t - up - down if t > 0 else 0
            used_pct = round((up + down) / t * 100, 1) if t > 0 else 0
            total_up += up
            total_down += down
            total_all += t
            per_profile_traffic.append({"name":c["name"],"total":fmt_size(t),
                "used":fmt_size(up+down),"down":fmt_size(down),
                "remaining":fmt_size(remaining),"used_pct":used_pct,
                "expire":expire,"total_raw":t,"remaining_raw":remaining})
        subscription_userinfo = None
        if total_all > 0:
            expires = [p["expire"] for p in per_profile_traffic if p["expire"]]
            earliest = min(expires) if expires else None
            parts = [f"upload={total_up}", f"download={total_down}", f"total={total_all}"]
            if earliest:
                parts.append(f"expire={earliest}")
            subscription_userinfo = "; ".join(parts)
        stats = {"total_proxies":len(all_proxies),"new_proxies":len(new_names),
            "profiles":[c["name"] for c in all_configs],"base_profile":base["name"],
            "ruleset_localized":local_count,
            "yaml_size_kb":round(len(merged_yaml_bytes)/1024,1),
            "sr_size_kb":round(len(merged_sr_bytes)/1024,1),
            "sr_proxies":len([l for l in base64.b64decode(merged_sr_bytes).decode().strip().split("\n") if l]),
            "traffic":per_profile_traffic}
        _last_merge_ts = time.time()
        # 更新节点状态(保留被屏蔽节点, 便于面板展示与故障恢复测试)
        new_status = {}
        for p in all_proxies:
            n = p["name"]
            old = node_status.get(n) or {"delay":None,"fail_cnt":0}
            old["server"] = p.get("server","")
            old["port"] = p.get("port",0)
            new_status[n] = old
        now_ts = time.time()
        for n in list(blocklist["permanent"]) + list(blocklist["temp"]):
            if n not in new_status:
                old = node_status.get(n) or {"delay":None,"fail_cnt":0}
                if not old.get("server") and n in proxy_meta:
                    old["server"], old["port"] = proxy_meta[n]
                old.setdefault("blocked_at", now_ts)
                new_status[n] = old
        node_status = new_status
        save_node_status()  # 合并后持久化状态缓存, 重启不丢
        return merged_yaml_bytes, subscription_userinfo, stats
# ==================== 节点后台测速 ====================
def _tcp_probe(server, port, timeout=TEST_TIMEOUT):
    """TCP 连通性探测(真实): 返回 (延迟ms, 是否成功)"""
    if not server or not port:
        return None, False
    st = time.perf_counter()
    try:
        with socket.create_connection((server, port), timeout=timeout):
            return round((time.perf_counter() - st) * 1000, 1), True
    except Exception:
        return None, False
def _clash_delay(name, timeout=TEST_TIMEOUT):
    """可选: 通过 Clash external-controller 测真实HTTP延迟; 未配置/失败返回 None"""
    if not CLASH_CONTROLLER:
        return None
    try:
        url = (f"http://{CLASH_CONTROLLER}/proxies/{quote(name, safe='')}/delay"
               f"?url={quote(TEST_URL, safe='')}&timeout={int(timeout*1000)}")
        req = urllib.request.Request(url)
        if CLASH_SECRET:
            req.add_header("Authorization", f"Bearer {CLASH_SECRET}")
        # 禁用系统代理直连 controller (Windows 上 urllib 默认读系统代理, 会被 Clash 系统代理劫持)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("delay")
    except Exception:
        return None
def _probe_one(name):
    """[C1修复] 真实探测: 失败才计数, 成功清零, 跳过已屏蔽节点"""
    s = node_status.get(name)
    if s is None or is_node_blocked(name):
        return
    delay, ok = None, False
    d = _clash_delay(name)
    if d is not None and d > 0:
        delay, ok = d, True
    elif not CLASH_CONTROLLER:
        # 仅未配置 controller 时才用 TCP 连通性兜底;
        # 配置后 delay API 失败(504/503)即判不通, 避免"TCP通但代理不通"的假通
        delay, ok = _tcp_probe(s.get("server"), s.get("port"))
    if ok:
        s["delay"] = delay
        s["fail_cnt"] = 0
    else:
        s["delay"] = None
        s["fail_cnt"] = s.get("fail_cnt", 0) + 1
        if ENABLE_AUTO_BLOCK and s["fail_cnt"] >= AUTO_BLOCK_FAIL_CNT:
            block_node(name, BLOCK_COOLDOWN_SEC)
            log.info(f"自动屏蔽节点 {name},连续失败{s['fail_cnt']}次")
def _system_cpu_percent():
    """返回系统CPU使用率(0-100); 非Windows或失败返回0(视为无压力)"""
    try:
        import ctypes
        class _FT(ctypes.Structure):
            _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]
        def _sample():
            idle, ker, usr = _FT(), _FT(), _FT()
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(ker), ctypes.byref(usr))
            if not ok:
                return None
            def _v(t):
                return (t.hi << 32) | t.lo
            return _v(idle), _v(ker), _v(usr)
        a = _sample()
        if a is None:
            return 0.0
        time.sleep(0.5)
        b = _sample()
        if b is None:
            return 0.0
        idle_d = b[0] - a[0]
        total_d = (b[1] + b[2]) - (a[1] + a[2])
        if total_d <= 0:
            return 0.0
        return max(0.0, min(100.0, (1 - idle_d / total_d) * 100))
    except Exception:
        return 0.0

def run_speedtest(concurrency=None, cpu_aware=False):
    """测速入口.
    concurrency=None -> 默认 TEST_CONCURRENCY.
    主动测速(面板按钮): 并发 TEST_CONCURRENCY, 不查CPU, 尽快完成.
    非主动(后台自动): concurrency=AUTO_TEST_CONCURRENCY(串行), cpu_aware=True,
    每测 CPU_CHECK_EVERY 个节点检查系统CPU, 负载超过 CPU_LOAD_LIMIT 则暂停本轮,
    让出资源保证 Web/订阅服务不受影响.
    """
    global _last_speedtest_ts
    if concurrency is None:
        concurrency = TEST_CONCURRENCY
    with speedtest_lock:
        names = list(node_status.keys())
        log.info(f"开始测速,节点总数:{len(names)},并发:{concurrency}"
                 + (",CPU感知" if cpu_aware else ""))
        speedtest_progress.update({"running": True, "done": 0, "total": len(names)})
        done = 0
        if concurrency <= 1:
            # 非主动: 串行 + 低频节奏 + CPU 压力感知(高负载暂停后继续, 可被手动测速打断)
            for idx, name in enumerate(names):
                if _manual_break.is_set():
                    log.info("检测到手动测速请求, 后台测速提前结束")
                    break
                if cpu_aware and CPU_LOAD_LIMIT > 0 and idx % CPU_CHECK_EVERY == 0:
                    cpu = _system_cpu_percent()
                    if cpu >= CPU_LOAD_LIMIT:
                        log.warning(f"CPU负载过高({cpu:.0f}%),测速暂停{CPU_PAUSE_SEC}秒后继续")
                        waited = 0
                        while waited < CPU_PAUSE_SEC and not _manual_break.is_set():
                            time.sleep(1)
                            waited += 1
                _probe_one(name)
                done += 1
                if AUTO_TEST_DELAY_SEC > 0:
                    time.sleep(AUTO_TEST_DELAY_SEC)  # 低频慢速, 不抢资源
        else:
            # 主动: 并发快速完成
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                list(ex.map(_probe_one, names))
            done = len(names)
        _last_speedtest_ts = time.time()
        speedtest_progress.update({"running": False, "done": done, "last_ts": _last_speedtest_ts})
        save_node_status()
        log.info(f"一轮测速任务结束: 完成 {done}/{len(names)}")
def _recover_one(n):
    """故障恢复: 测试单个被屏蔽节点, 通→解禁; 不通→刷新24h屏蔽"""
    s = node_status.get(n)
    if s is None:
        return "skip"
    delay, ok = None, False
    if CLASH_CONTROLLER:
        d = _clash_delay(n)
        if d is not None and d > 0:
            delay, ok = d, True
        # 配置 controller 后不回退 TCP, 避免"TCP通(本地握手<50ms)但代理不通"的假恢复
    elif s.get("server"):
        delay, ok = _tcp_probe(s["server"], s.get("port"))
    s["last_test"] = time.time()
    if ok:
        unblock_node(n)
        log.info(f"故障恢复: {n} 已恢复 ({delay}ms)")
        return "recovered"
    block_node(n, BLOCK_FAILED_DURATION_SEC)  # 不通继续屏蔽24h
    log.info(f"故障恢复: {n} 仍不通, 继续屏蔽24h")
    return "still_blocked"

def recover_blocked():
    """手动故障恢复/启动扫描: 测试所有未过期临时屏蔽节点, 通的解禁"""
    now = time.time()
    with blocklist_lock:
        temp = [n for n, exp in blocklist["temp"].items() if exp > now]
    rec = st = 0
    for n in temp:
        r = _recover_one(n)
        if r == "recovered":
            rec += 1
        elif r == "still_blocked":
            st += 1
    if temp:
        do_merge()
    return rec, st

def weekly_recover():
    """自动: 屏蔽超过3天且距上次测试>=7天的节点, 每周重测一次"""
    now = time.time()
    with blocklist_lock:
        temp = [n for n, exp in blocklist["temp"].items() if exp > now]
    for n in temp:
        s = node_status.get(n)
        if not s:
            continue
        if now - s.get("blocked_at", 0) > 3*86400 and now - s.get("last_test", 0) >= 7*86400:
            _recover_one(n)

def start_speedtest_background(interval=SPEEDTEST_INTERVAL_SEC):
    if interval <= 0:
        return
    def runner():
        while True:
            try:
                run_speedtest(concurrency=AUTO_TEST_CONCURRENCY, cpu_aware=True)  # 非主动: 串行+CPU感知
            except Exception as e:
                log.error(f"后台测速失败: {e}")
            try:
                weekly_recover()  # 屏蔽超3天节点每周重测一次
            except Exception as e:
                log.error(f"每周恢复检查失败: {e}")
            time.sleep(interval)
    threading.Thread(target=runner, daemon=True).start()
# ==================== WEB面板单文件HTML ====================
WEB_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Clash订阅合并管理器</title>
    <style>
        *{box-sizing:border-box;margin:0;padding:0;font-family:system-ui}
        body{padding:16px;background:#f3f4f6}
        .container{max-width:1200px;margin:0 auto;background:white;padding:20px;border-radius:12px;box-shadow:0 2px 10px #00000015}
        .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
        .btn{padding:8px 14px;border:none;border-radius:6px;cursor:pointer;margin:3px}
        .btn-primary{background:#2563eb;color:white}
        .btn-danger{background:#dc2626;color:white}
        .btn-success{background:#16a34a;color:white}
        table{width:100%;border-collapse:collapse;margin-top:15px}
        th,td{border:1px solid #ddd;padding:10px;text-align:left;font-size:14px}
        th{background:#f9fafb}
        .badge-block{background:#ef4444;color:white;padding:2px 6px;border-radius:4px;font-size:12px}
        .badge-ok{background:#22c55e;color:white;padding:2px 6px;border-radius:4px;font-size:12px}
        .traffic-item{padding:8px;margin:6px 0;background:#f0f7ff;border-radius:6px}
        .bar-bg{width:100%;height:12px;background:#e5e7eb;border-radius:6px;margin-top:4px}
        .bar-fill{height:100%;background:#3b82f6;border-radius:6px}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h2>订阅合集管理面板</h2>
        <div>
            <button class="btn btn-primary" onclick="refreshMerge()">刷新订阅合并</button>
            <button class="btn btn-primary" onclick="startSpeedtest()">开始测速</button>
            <button class="btn btn-danger" onclick="blockFailed()">屏蔽不通节点</button>
            <button class="btn btn-success" onclick="unblockAll()">故障恢复</button>
            <button class="btn btn-primary" onclick="refreshStatus()">刷新页面</button>
        </div>
    </div>
    <div id="status-box"></div>
    <h3 style="margin-top:20px">节点列表</h3>
    <table>
        <thead>
            <tr><th>节点名称</th><th>延迟</th><th>失败次数</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody id="node-table"></tbody>
    </table>
</div>
<script>
const apiBase = "";
function esc(s){
    return String(s).replace(/[&<>"']/g,
        c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
async function refreshStatus(){
    const res = await fetch(apiBase+"/status");
    const data = await res.json();
    let html = `<h4>订阅流量</h4>`;
    (data.traffic||[]).forEach(sub=>{
        const pct = Math.min(sub.used_pct||0,100);
        html += `<div class="traffic-item">
            <div>${esc(sub.name)} 剩余:${esc(sub.remaining)} / ${esc(sub.total)} (${pct}%)</div>
            <div class="bar-bg"><div class="bar-fill" style="width:${pct}%"></div></div>
        </div>`;
    });
    const st = data.speedtest||{};
    let stText = "未测速";
    if(st.running){ stText = `测速中 ${st.done}/${st.total}`; }
    else if(st.last_ts){ stText = `空闲(上次测速 ${new Date(st.last_ts*1000).toLocaleTimeString()})`; }
    html += `<div>总节点数:${data.total_proxies ?? 0} · 测速状态: ${stText}</div>`;
    document.getElementById("status-box").innerHTML = html;
    let nodeHtml = "";
    for(const [name,info] of Object.entries(data.node_status||{})){
        const blocked = info.blocked;
        const delay = info.delay ?? "-";
        const fail = info.fail_cnt ?? 0;
        const badge = blocked ? `<span class="badge-block">已屏蔽</span>` : `<span class="badge-ok">正常</span>`;
        const act = blocked ? "unblock" : "block";
        const lbl = blocked ? "解除屏蔽" : "临时屏蔽";
        nodeHtml += `<tr>
            <td>${esc(name)}</td>
            <td>${delay}</td>
            <td>${fail}</td>
            <td>${badge}</td>
            <td><button class="btn ${blocked?"btn-success":"btn-danger"}" data-act="${act}" data-name="${esc(name)}">${lbl}</button></td>
        </tr>`;
    }
    document.getElementById("node-table").innerHTML = nodeHtml;
}
document.getElementById("node-table").addEventListener("click", e=>{
    const btn = e.target.closest("button[data-act]");
    if(!btn) return;
    const act = btn.dataset.act, name = btn.dataset.name;
    fetch(apiBase+"/api/"+act+"?name="+encodeURIComponent(name))
        .then(()=>refreshStatus());
});
async function refreshMerge(){
    await fetch(apiBase+"/refresh");
    refreshStatus();
}
async function startSpeedtest(){
    const res = await fetch(apiBase+"/api/speedtest").then(r=>r.json());
    alert(res.msg);
    refreshStatus();
}
async function blockFailed(){
    if(!confirm("确定屏蔽所有测速不通的节点?局域网订阅将立即剔除它们")) return;
    const res = await fetch(apiBase+"/api/block-failed").then(r=>r.json());
    alert(res.msg);
    refreshStatus();
}
async function unblockAll(){
    if(!confirm("确定重新测试所有被屏蔽节点?通的恢复,不通继续屏蔽24小时")) return;
    const res = await fetch(apiBase+"/api/unblock-all").then(r=>r.json());
    alert(res.msg);
    refreshStatus();
}
refreshStatus();
</script>
</body>
</html>
'''
# ==================== 源文件变更检测 ====================
def _check_source_changed():
    global _last_merge_ts
    if _last_merge_ts == 0:
        return True
    meta_path = os.path.join(CLASH_DIR, "profiles.yaml")
    if os.path.isfile(meta_path) and os.path.getmtime(meta_path) > _last_merge_ts:
        return True
    try:
        remote_profiles = read_profiles_meta()
    except Exception:
        return False
    for p in remote_profiles:
        if f":{PORT}" in p.get("url", ""):
            continue
        fp = os.path.join(PROFILES_DIR, p["file"])
        if os.path.isfile(fp) and os.path.getmtime(fp) > _last_merge_ts:
            return True
    return False
class MergeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _token_ok(self):
        """[S2修复] 管理路径鉴权; ADMIN_TOKEN 为空则不校验"""
        if not ADMIN_TOKEN:
            return True
        qs = parse_qs(urlparse(self.path).query)
        return qs.get("token", [""])[0] == ADMIN_TOKEN
    def do_HEAD(self):
        if self.path in ("/", "/sub", "/profile"):
            if not merged_yaml_bytes:
                return self._err(503, "not ready")
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml; charset=utf-8")
            self.send_header("Content-Length", str(len(merged_yaml_bytes)))
            if subscription_userinfo:
                self.send_header("Subscription-Userinfo", subscription_userinfo)
            self.send_header("Profile-Update-Interval", "24")
            self.end_headers()
            return
        if self.path == "/shadowrocket":
            if not merged_sr_bytes:
                return self._err(503, "not ready")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(merged_sr_bytes)))
            if subscription_userinfo:
                self.send_header("Subscription-Userinfo", subscription_userinfo)
            self.end_headers()
            return
        if self.path.startswith("/ruleset/"):
            safe = self._safe_ruleset_path(self.path)
            if not safe:
                return self._err(404, "not found")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(safe)))
            self.end_headers()
            return
        self._err(404, "not found")
    def _safe_ruleset_path(self, path):
        """[S3修复] 用 commonpath 防前缀穿越"""
        fname = unquote(path[len("/ruleset/"):])
        safe = os.path.normpath(os.path.join(RULESET_DIR, fname))
        try:
            inside = os.path.commonpath([RULESET_DIR, safe]) == RULESET_DIR
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(safe):
            return None
        return safe
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        # 管理路径鉴权 [S2修复]
        if parsed.path in ("/web", "/status", "/refresh", "/api/block", "/api/unblock",
                           "/api/speedtest", "/api/block-failed", "/api/unblock-all"):
            if not self._token_ok():
                return self._err(401, "unauthorized")
        if parsed.path == "/web":
            body = WEB_HTML.encode("utf8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/block":
            name = qs.get("name", [""])[0]  # [C3修复] 去掉二次unquote
            if not name:
                return self._json({"ok": False, "msg": "name 参数为空"})
            duration = int(qs.get("duration", [BLOCK_COOLDOWN_SEC])[0] or BLOCK_COOLDOWN_SEC)
            block_node(name, duration)
            do_merge()  # [M1修复] 屏蔽后即时生效
            return self._json({"ok": True, "msg": f"已屏蔽:{name}"})
        if parsed.path == "/api/unblock":
            name = qs.get("name", [""])[0]  # [C3修复]
            if not name:
                return self._json({"ok": False, "msg": "name 参数为空"})
            unblock_node(name)  # [M2修复] 内部重置 fail_cnt
            do_merge()  # [M1修复]
            return self._json({"ok": True, "msg": f"已解除屏蔽:{name}"})
        if parsed.path == "/api/speedtest":
            # 手动测速: 强制打断后台(串行/CPU暂停)测速, 并发快速完成, 不受CPU限制
            global _last_speedtest_ts
            _manual_break.set()
            acquired = speedtest_lock.acquire(timeout=90)
            _manual_break.clear()
            if not acquired:
                return self._json({"ok": False, "msg": "后台测速未响应,请稍后再试"})
            try:
                names = list(node_status.keys())
                speedtest_progress.update({"running": True, "done": 0, "total": len(names)})
                with ThreadPoolExecutor(max_workers=TEST_CONCURRENCY) as ex:
                    list(ex.map(_probe_one, names))
                _last_speedtest_ts = time.time()
                speedtest_progress.update({"running": False, "done": len(names),
                                           "last_ts": _last_speedtest_ts})
                save_node_status()
                return self._json({"ok": True, "msg": f"测速完成: {len(names)}/{len(names)}"})
            finally:
                speedtest_lock.release()
        if parsed.path == "/api/block-failed":
            """测试屏蔽: 只屏蔽本轮测速判定失败的节点(fail_cnt>0且delay为None),
            避免把"尚未测速/测速未完成"的节点误伤屏蔽"""
            duration = int(qs.get("duration", [BLOCK_FAILED_DURATION_SEC])[0] or BLOCK_FAILED_DURATION_SEC)
            bad = [n for n, s in node_status.items()
                   if s.get("delay") is None and s.get("fail_cnt", 0) > 0]
            for n in bad:
                block_node(n, duration)
            if bad:
                do_merge()  # 屏蔽立即对局域网订阅生效
            return self._json({"ok": True, "msg": f"已屏蔽 {len(bad)} 个不通节点",
                               "blocked": len(bad)})
        if parsed.path == "/api/unblock-all":
            """故障恢复: 重新测试所有临时屏蔽节点, 通的解禁, 不通继续屏蔽24h"""
            rec, st = recover_blocked()
            return self._json({"ok": True, "msg": f"恢复 {rec} 个, 仍屏蔽 {st} 个",
                               "recovered": rec, "still_blocked": st})
        if parsed.path == "/status":
            export_node = {}
            for k, v in node_status.items():
                export_node[k] = {"delay": v.get("delay"),
                                  "fail_cnt": v.get("fail_cnt", 0),
                                  "blocked": is_node_blocked(k)}
            return self._json({"status": "running", "title": "订阅合集",
                               **stats, "userinfo": subscription_userinfo,
                               "node_status": export_node,
                               "speedtest": speedtest_progress})
        if parsed.path == "/refresh":
            try:
                do_merge()
                return self._json({"success": True, **stats})
            except Exception as e:
                return self._json({"success": False, "error": str(e)})
        if parsed.path.startswith("/ruleset/"):
            safe = self._safe_ruleset_path(self.path)
            if not safe:
                return self._err(404, "not found")
            with open(safe, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/shadowrocket":
            self._auto_refresh_if_changed()  # [M6修复] 恢复自动重合并
            if not merged_sr_bytes:
                return self._err(503, "SR not ready")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(merged_sr_bytes)))
            if subscription_userinfo:
                self.send_header("Subscription-Userinfo", subscription_userinfo)
            self.end_headers()
            self.wfile.write(merged_sr_bytes)
            return
        if parsed.path in ("/", "/sub", "/profile"):
            self._auto_refresh_if_changed()  # [M6修复]
            if not merged_yaml_bytes:
                return self._err(503, "config not ready")
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml; charset=utf-8")
            self.send_header("Content-Length", str(len(merged_yaml_bytes)))
            if subscription_userinfo:
                self.send_header("Subscription-Userinfo", subscription_userinfo)
            self.send_header("Profile-Update-Interval", "24")
            self.end_headers()
            self.wfile.write(merged_yaml_bytes)
            return
        self._err(404, "not found")
    def _auto_refresh_if_changed(self):
        if _check_source_changed():
            log.info("检测到源文件变更，自动重新合并...")
            try:
                do_merge()
                log.info("自动合并完成")
            except Exception as e:
                log.error(f"自动合并失败: {e}")
    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def _err(self, code, msg):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} {fmt % args}")
class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def handle_error(self, request, client_address):
        # 客户端提前断开(刷新/取消请求)会抛 ConnectionReset/BrokenPipe, 属正常现象, 不打印堆栈
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)
def _get_process_cmdline(pid):
    """查询进程命令行(Windows), 失败返回空串"""
    try:
        ps = ("powershell -NoProfile -Command "
              f"\"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine\"")
        out = subprocess.run(ps, capture_output=True, text=True, timeout=15).stdout
        return (out or "").strip()
    except Exception:
        return ""

def _find_port_holder(port):
    """返回占用端口(监听中)的进程 (pid, cmdline); 无占用或占用者是本进程返回 None"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception as e:
        log.warning(f"netstat 查询失败: {e}")
        return None
    pids = set()
    for line in (out or "").splitlines():
        if "LISTENING" not in line:
            continue
        if not re.search(rf"[.:]{port}\s+", line):
            continue
        parts = line.split()
        if len(parts) >= 5 and parts[-1].isdigit():
            pids.add(int(parts[-1]))
    for pid in pids:
        if pid == os.getpid():
            continue
        return pid, _get_process_cmdline(pid)
    return None

def _stop_conflicting_service():
    """启动前检测端口被旧版服务占用: 提示用户按 1 确认停止后再接管端口"""
    holder = _find_port_holder(PORT)
    if not holder:
        return
    pid, cmdline = holder
    shown = cmdline or "(未知进程, 非本服务?)"
    print("-" * 55)
    print(f"  检测到端口 {PORT} 被占用: PID {pid}")
    print(f"  进程: {shown}")
    print("  这通常是旧版订阅服务仍在运行, 需停止后由本程序接管端口。")
    try:
        ans = input("  输入 1 停止该进程并继续启动; 其他键退出: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("  已取消启动")
        sys.exit(1)
    if ans != "1":
        print("  已取消启动")
        sys.exit(1)
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)
    except Exception as e:
        print(f"  停止进程失败: {e}")
        sys.exit(1)
    for _ in range(20):
        if not _find_port_holder(PORT):
            break
        time.sleep(0.5)
    else:
        print("  端口未能及时释放, 请手动检查占用进程")
        sys.exit(1)
    print(f"  已停止旧进程 PID {pid}, 继续启动...")

def _patch_cn_direct_rules(rules):
    """国内直连兜底: 1) CN_DIRECT_EXTRA 域名规则插到最前; 2) GEOSITE,cn 完整国内域名;
    3) GEOIP,CN 兜底插到 MATCH 前。rules 元素为裸规则字符串(不含 "- " 前缀)。
    自定义精简 geosite/geoip 规则集常缺国内域名(如 1688.com), fake-ip 模式下
    ipcidr 规则对域名流量几乎无效, 在此统一兜底, 避免漏网流量走代理。幂等, 组名跟随现有规则。"""
    if not isinstance(rules, list):
        return rules
    out = list(rules)
    group = "DIRECT"
    for r in out:
        if isinstance(r, str) and "国内服务" in r:
            parts = [p.strip() for p in r.split(",")]
            if len(parts) >= 3 and parts[0].lstrip("- ").upper().startswith("RULE-SET"):
                group = parts[-1]
                break
    existing = {str(r).strip() for r in out}
    extra = []
    for d in CN_DIRECT_EXTRA:
        line = f"DOMAIN-SUFFIX,{d},{group}"
        if line not in existing:
            extra.append(line)
    if CN_GEOSITE_PATCH:
        gline = f"GEOSITE,cn,{group}"
        if gline not in existing:
            extra.append(gline)
    if extra:
        out = extra + out
    geo = f"GEOIP,CN,{group}"
    if geo not in existing:
        for i, r in enumerate(out):
            if isinstance(r, str) and r.lstrip("- ").strip().upper().startswith("MATCH"):
                out.insert(i, geo)
                break
        else:
            out.append(geo)
    return out

def _initial_recover():
    """启动后优先扫描一次被屏蔽节点: 有通的自动解禁"""
    try:
        rec, st = recover_blocked()
        log.info(f"启动恢复扫描: 恢复 {rec} 个, 仍屏蔽 {st} 个")
    except Exception as e:
        log.error(f"启动恢复扫描失败: {e}")
# ==================== 主入口 ====================
def main():
    log.info("=" * 55)
    log.info("  Clash Verge 订阅合集本地分享服务 [v2]")
    log.info("=" * 55)
    _stop_conflicting_service()  # 启动前检测旧服务占用端口, 按1确认停止
    if not os.path.isdir(CLASH_DIR):
        log.error(f"未找到 Clash Verge 数据目录:\n  {CLASH_DIR}")
        input("按回车键退出...")
        sys.exit(1)
    log.info(f"数据目录: {CLASH_DIR}")
    load_blocklist()
    load_ruleset_versions()  # 恢复规则集版本状态, 避免重启导致版本号重置
    load_node_status()  # 重启后恢复上次测速缓存
    log.info("正在读取并合并订阅...")
    try:
        do_merge()
    except Exception as e:
        log.error(f"合并失败: {e}")
        import traceback
        traceback.print_exc()
        input("按回车键退出...")
        sys.exit(1)
    ip = get_local_ip()
    server = ThreadedHTTPServer((HOST, PORT), MergeHandler)
    threading.Thread(target=_initial_recover, daemon=True).start()  # 启动优先扫描被屏蔽节点
    start_speedtest_background(SPEEDTEST_INTERVAL_SEC)
    log.info("-" * 55)
    log.info("  服务已启动!")
    log.info(f"  局域网合集地址:")
    log.info(f"    Clash:        http://{ip}:{PORT}")
    log.info(f"    Shadowrocket: http://{ip}:{PORT}/shadowrocket")
    log.info(f"    Web面板:     http://{ip}:{PORT}/web")
    log.info(f"  状态:    http://{ip}:{PORT}/status")
    log.info(f"  刷新:    http://{ip}:{PORT}/refresh")
    log.info(f"  规则集:  http://{ip}:{PORT}/ruleset/<name>.mrs")
    if ADMIN_TOKEN:
        log.info(f"  管理令牌已启用: 面板/API 需携带 ?token={ADMIN_TOKEN}")
    log.info("-" * 55)
    pcount = stats.get("total_proxies", 0)
    plist = stats.get("profiles", [])
    log.info(f"  合集 {len(plist)} 个订阅, 共 {pcount} 个节点")
    log.info(f"  订阅列表: {', '.join(plist)}")
    log.info("-" * 55)
    for t in stats.get("traffic", []):
        bar = _usage_bar(t["used_pct"])
        mark = " [已用较多]" if t["used_pct"] > 80 else ""
        log.info(f"  [{t['name']}] {t['remaining']} 剩余 | {t['used']}/{t['total']} ({t['used_pct']}%) {bar}{mark}")
    if subscription_userinfo:
        log.info(f"  总计: {subscription_userinfo}")
    log.info("-" * 55)
    log.info("  按 Ctrl+C 停止服务")
    log.info("=" * 55)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("\n服务已停止")
        server.shutdown()
if __name__ == "__main__":
    main()
