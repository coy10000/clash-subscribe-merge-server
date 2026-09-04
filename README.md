# Clash Subscribe Merge Server

> 本地订阅合集自动合并分享服务 —— 读取 Clash Verge 中已有的订阅，合并成一份统一订阅，通过 HTTP 分享给本机与局域网内的 Clash 客户端、ios小火箭。

无需手动维护订阅链接：所有订阅源直接取自本机 Clash Verge 已配置的订阅，**源码不含任何订阅 URL / 机场信息 / 个人数据**。

## 功能特性

| 类别 | 功能 |
|---|---|
| 合并 | 多订阅自动合并去重；自动剔除"官网 / 流量 / 公告"等非节点信息条目 |
| 节点 | 短名重命名（地区缩写 + 序号），命名冲突自动去重 |
| 测速 | 实测延迟（真实 HTTP 探测 / Clash REST API 精确延迟）；后台周期自动测速；**CPU 负载感知暂停**（默认 >70% 暂停 5 分钟），保护低配主机 |
| 分组 | 按实测延迟将全部节点分为 **快 / 中 / 慢** 三组，客户端默认走最快组 |
| 屏蔽 | 自动屏蔽持续不通的节点（默认 24h 后自动解封）；手动屏蔽 / 解封；故障节点从订阅中即时剔除 |
| 规则集 | rule-providers 远程 URL 自动**本地化**为服务地址（无外网环境可用）；规则集文件**版本化**（.v{n}），新版本每天最多发布一次，避免客户端反复重载 |
| 分流 | 国内直连兜底：注入 `GEOSITE,cn` / `GEOIP,CN` / 补充域名规则，防止国内站漏网误走代理 |
| 输出 | Clash YAML 与 Shadowrocket 双格式 |
| 管理 | Web 控制台（节点状态 / 屏蔽管理 / 手动测速）+ REST API |
| 运维 | 端口冲突交互接管（按 1 确认停旧起新）；状态持久化（重启不丢测速与屏蔽数据）；离线规则包导出 |

## 工作原理

```
                    ┌─────────────────────────────────────────────┐
                    │  本机 Clash Verge                            │
                    │  profiles.yaml ── 读取已配置的订阅(源A/源B…) │
                    └──────────────────────┬──────────────────────┘
                                           ▼
                    ┌─────────────────────────────────────────────┐
                    │  server.py (本仓库)                          │
                    │  · 合并去重 / 剔除信息节点 / 短名重命名       │
                    │  · 实测延迟 → 快/中/慢 分组                  │
                    │  · 屏蔽管理(自动+手动) → 过滤不可用节点       │
                    │  · 规则集 URL 本地化 + 版本化                │
                    │  · 注入国内直连兜底规则                      │
                    └───────────────┬─────────────────────────────┘
                                    ▼
        HTTP  :8080/sub(Clash)  :8080/shadowrocket  :8080/web(面板)
                                    ▼
             本机 Clash Verge  /  局域网内其他 Clash 客户端
```

## 环境要求

- Windows（服务端需运行 Clash Verge Rev 的数据目录结构）
- Python 3.8+，依赖仅 **PyYAML**（缺失时自动安装）
- Clash Verge Rev（客户端；geosite/geoip 规则依赖其随附数据文件）

## 快速开始

```bash
# 1. 把 server.py 放到任意目录（建议与 Clash Verge 同机）
# 2. 启动（若 8080 端口被旧实例占用，交互提示按 1 自动接管）
python server.py

# 3. 浏览器打开管理面板
#    http://<本机IP>:8080/web

# 4. Clash Verge 添加订阅
#    URL: http://<本机IP>:8080/sub     （Clash 格式）
#    URL: http://<本机IP>:8080/shadowrocket  （Shadowrocket 格式）
```

> 局域网内其他设备填本机 IP 即可；防火墙需放行 8080 端口。

## 配置说明

所有配置集中在文件顶部"基础配置区"，按需修改后重启生效：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `PORT` / `HOST` | `8080` / `0.0.0.0` | 服务监听地址 |
| `ADMIN_TOKEN` | 空 | 管理令牌；设置后 `/web /status /refresh /api/*` 需 `?token=`，订阅端点不受限（建议启用） |
| `TEST_URL` | `https://www.gstatic.com/generate_204` | 测速探测地址 |
| `TEST_TIMEOUT` | `3` 秒 | 单节点探测超时 |
| `AUTO_BLOCK_FAIL_CNT` | `3` | 连续失败 N 次自动屏蔽 |
| `BLOCK_COOLDOWN_SEC` | `86400` | 自动屏蔽解禁时长（24h） |
| `SPEEDTEST_INTERVAL_SEC` | `21600` | 后台自动测速间隔（6h，0=关闭） |
| `CPU_LOAD_LIMIT` | `70` | 后台测速期间 CPU 超过此值暂停 |
| `CLASH_CONTROLLER` | `127.0.0.1:9097` | Clash external-controller（真实 HTTP 延迟测速，可选） |
| `RULESET_DAILY_LIMIT` | `True` | 规则集新版本每天最多发布一次 |
| `CN_DIRECT_EXTRA` | `["1688.com"]` | 国内直连补充域名（按需增删） |
| `CN_GEOSITE_PATCH` | `True` | 注入 `GEOSITE,cn`（依赖客户端 geosite.dat，缺失请关闭） |

## Web 面板与 API

- `GET /web` — 可视化面板：节点状态（延迟 / 屏蔽 / 故障）、手动测速、屏蔽 / 解封
- `GET /status` — 节点与测速状态 JSON
- `GET /refresh` — 手动触发重新合并
- `POST /api/block` / `POST /api/unblock` — 按节点名屏蔽 / 解封（屏蔽后自动重新合并并立即从订阅剔除）
- `POST /api/speedtest` — 手动触发全量测速

## 离线规则包导出（临时更新）

无法连接本服务局域网的客户端（如外出设备）可用离线包临时更新：

```bash
SUB_URL=http://<服务器IP>:8080/sub python export_offline_pack.py
# 生成 offline_pack_<时间戳>.zip: 含 merged.yaml + 全部规则集 + 安装说明
```

客户端解压后：将 `ruleset/*.mrs` 放入其 Clash Verge 的 `ruleset\` 目录，再本地导入 `merged.yaml` 即可（内核检测到本地规则集文件后不再访问网络）。

## 常见问题

**Q: 节点全部显示不可用 / 被屏蔽？**
A: 检查 `TEST_URL` 在你网络环境下是否可达；测速失败会自动临时屏蔽（24h 自动解封），可在面板手动解封并重测。

**Q: 为什么订阅里找不到被屏蔽的节点？**
A: 屏蔽节点在合并输出中已被剔除（节点列表与所有策略组均不含）。Web 面板显示全部节点用于管理，属正常设计。

**Q: 国内网站走了代理？**
A: 默认已注入 `GEOSITE,cn` / `GEOIP,CN` 兜底规则。若仍有个别域名漏网，将其加入 `CN_DIRECT_EXTRA` 后重启。

**Q: 更新订阅后提示规则集下载失败？**
A: 确认服务端 `ruleset\` 目录存在订阅引用的规则集文件；缺失时服务端日志会告警并保留远程 URL，请补齐文件后重新合并。

**Q: 其他客户端能访问面板吗？**
A: 可以，但建议设置 `ADMIN_TOKEN`；订阅端点（`/sub` 等）不受令牌限制。

## 支持范围

**支持**
- Clash Verge Rev 作为宿主环境（订阅发现与规则集目录基于其目录结构）
- 局域网 / 本机 Clash 系客户端订阅（Clash Verge / Clash for Windows / Stash 等，mihomo 内核）
- Shadowrocket 订阅输出
- MRS 格式规则集（mihomo rule-set）

**不支持 / 暂不计划**
- 非 Windows 平台运行（未适配其他系统目录结构）
- 非 Clash Verge 的目录结构（无 profiles.yaml 的客户端）
- 规则集文件的生成 / 编辑（请使用 mihomo 或第三方工具制作后放入 ruleset 目录）

## 隐私与安全

- 本工具**不收集任何数据**，无遥测、无外部请求（测速探测地址可自行为空关闭）
- 订阅链接仅存在于本机 Clash Verge 配置中，源码不含任何订阅 URL
- 局域网分享时请务必设置 `ADMIN_TOKEN`；如无局域网需求可绑定 `127.0.0.1` 仅本机使用

## License

[MIT](LICENSE)
