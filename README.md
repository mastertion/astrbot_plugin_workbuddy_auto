# WorkBuddy 自动签到 · AstrBot 插件

把 [gdemoni/workbuddy-Auto](https://github.com/gdemoni/workbuddy-Auto)（MIT）的 `daily.py` 封装成 AstrBot 插件：每日签到、成长任务、领奖、多账号、Token 自动续期，全部由 QQ 指令和 WebUI 设置页驱动。

**和独立 Docker 容器的区别**：引擎以 **子进程** 运行，不阻塞 AstrBot 事件循环；凭据留在插件数据目录；定时报告用 `context.send_message` 原生推送到 QQ。不再需要 cron 容器、`.token` 挂载和 OpenAPI 推送配置。

---

## 1. 安装（三种链接，任选其一）

| 方式 | 链接 / 操作 |
| --- | --- |
| **从仓库链接安装**（推荐） | `https://github.com/mastertion/astrbot_plugin_workbuddy_auto`<br>WebUI → 插件 → ➕ → 从链接/Git 安装，粘贴后确认 |
| **插件市场源（配置链接）** | `https://raw.githubusercontent.com/mastertion/astrbot_plugin_workbuddy_auto/main/plugin-market.json`<br>WebUI → 插件市场 → 添加源 → 粘贴 → 在列表里点安装 |
| **ZIP 上传** | `https://github.com/mastertion/astrbot_plugin_workbuddy_auto/archive/refs/heads/main.zip`<br>解压后把内层目录重命名为 `astrbot_plugin_workbuddy_auto`，上传到 `data/plugins/` |

也可手动放到 AstrBot 的 `data/plugins/astrbot_plugin_workbuddy_auto/` 后重载插件。

> **国内 NAS 访问不了 GitHub**：可用镜像前缀 `https://gitclone.com/github.com/mastertion/astrbot_plugin_workbuddy_auto`，或在本机下载 ZIP 后 scp/上传到 `data/plugins/`。插件本身运行时不需要访问 GitHub（除非你配置了 `config_url`）。

安装后在 WebUI 重载插件。依赖 `requests>=2.31.0` 已在 `requirements.txt` / `metadata.yaml` 声明（AstrBot 4.29.4 自带 requests）。

---

## 2. 设置页面

### 2.1 AstrBot WebUI 内嵌设置页（AstrBot ≥ 4.29）

`WebUI → 插件 → WorkBuddy 自动签到 → 设置`

- **账号令牌**：每行一个 `手机号:AT:RT`，保存后写入数据目录 `tokens.txt`（权限 600），不回显。
- **发消息的 QQ 号**：直接填 QQ 号，自动拼 `<适配器ID>:FriendMessage:<QQ>`；群填 `group:群号`。
- **平台适配器 ID**：WebUI「平台配置」里的 ID，默认 `aiocqhttp`；若你叫 `QQ` / `QQ2`，必须如实填写。
- 「**发送测试消息**」一键验证 QQ 推送是否通。
- 「**下载配置 JSON**」「**下载 tokens.txt**」「**导入本地配置**」。

页面由官方插件视图机制实现：`views/settings/index.html` + `context.register_web_api()` + `window.AstrBotPluginView` bridge。低版本 AstrBot 会自动跳过视图注册，插件其余功能照常工作。

### 2.2 独立设置页（无需 AstrBot 在线）

- 打开仓库根目录的 `settings.html`（双击本地文件即可）。
- 或启用 GitHub Pages（仓库 Settings → Pages → Source 选 `main / root`）后访问：
  `https://mastertion.github.io/astrbot_plugin_workbuddy_auto/settings.html`
- 不想开 Pages 也可用临时渲染链接：
  `https://htmlpreview.github.io/?https://github.com/mastertion/astrbot_plugin_workbuddy_auto/blob/main/settings.html`

独立模式下点「**生成并下载配置**」得到 `workbuddy-config.json` 与 `tokens.txt`，然后二选一：

1. 把 `workbuddy-config.json` 改名为 `config.json`，和 `tokens.txt` 一起放进
   `data/plugin_data/astrbot_plugin_workbuddy_auto/`，重载插件；
2. 把 JSON 托管成 http(s) 链接，填进设置页的「配置链接」或插件配置的 `config_url`。

---

## 3. 配置链接 · 下载配置

插件支持「配置链接」：启动、`/wb config pull` 或设置页点击按钮时，从 http(s) 链接下载 JSON 配置并应用。

```text
配置模板：https://raw.githubusercontent.com/mastertion/astrbot_plugin_workbuddy_auto/main/config.example.json
令牌模板：https://raw.githubusercontent.com/mastertion/astrbot_plugin_workbuddy_auto/main/tokens.example.txt
```

- 支持 **JSON 配置对象**（键见下表），也支持**纯令牌文本**（`手机号:AT:RT` 多行）。
- `tokens` 单独写入 `tokens.txt`；其余键写入数据目录 `config.json`，**优先级高于 WebUI 配置**。
- 想恢复 WebUI 配置：`/wb config clear`（清空 `config.json` 覆盖）。
- 服务端抓取（AstrBot 内嵌设置页 / `/wb config`）不受浏览器跨域限制；独立设置页直连受 CORS 影响，可用「导入本地配置」代替。

```bash
/wb config                       # 查看当前配置来源、推送目标
/wb config <http(s) 链接>        # 下载并应用配置，同时记住这个链接
/wb config pull                  # 重新下载已保存的配置链接
/wb config clear                 # 清除覆盖，恢复 WebUI 配置
/wb qq 123456789                # 直接设置发消息的 QQ 号
```

---

## 4. 配置项

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `tokens` | `手机号:AT:RT`，每行一个；留空读 `tokens.txt` | 空 |
| `notify_qqs` | 发消息的 QQ 号，逗号分隔；群用 `group:群号` | 空 |
| `umo_platform` | 平台适配器 ID（QQ 号前缀） | `aiocqhttp` |
| `config_url` | 配置链接（http/https） | 空 |
| `schedule_enabled` / `schedule_times` | 定时开关 / 北京时间点 | `true` / `07:00,12:00,23:30` |
| `notify_on_schedule` | 定时完成后推送报告 | `true` |
| `admin_qqs` | 允许用 `/wb` 指令的 QQ，留空不限制 | 空 |
| `notify_umos` | 高级：额外完整 UMO | 空 |
| `no_desktop` | 跳过 Windows 桌面端任务（Docker 必须开） | `true` |
| `timeout` / `reply_chars` | 引擎超时秒数 / 摘要最大字符数 | `1800` / `1200` |
| `pushplus_token` / `wecom_webhook` / `notify_url` / `notify_kind` | 可选的额外推送通道 | 空 |

凭据与运行数据都在 `data/plugin_data/astrbot_plugin_workbuddy_auto/`（`tokens.txt`、`config.json`、`wb_refresh_tokens.json`、`state.json`、`subscribers.json`、`logs/`）。

---

## 5. QQ 指令

```text
/wb help                 帮助
/wb run [账号序号]        立即执行完整签到（/wb run 2 只跑第 2 个账号）
/wb query                仅查询积分 / 用量 / 成长
/wb refresh              仅刷新全部账号 Token
/wb status               运行状态、账号数、下次定时
/wb accounts             已保存账号（脱敏）
/wb sub / /wb unsub      订阅 / 取消订阅定时报告
/wb log [行数]           最近一次引擎日志
/wb stop                 终止正在运行的任务
/wb qq <QQ号>            设置发消息的 QQ 号
/wb config [链接]        查看配置 / 从链接下载并应用配置
```

### 5.1 报告怎么看（v1.2 起）

`/wb query`、`/wb run` 与定时签到返回的报告按账号分组，账号用令牌里的昵称 + 脱敏手机号标识：

```text
【WorkBuddy 数据查询】✅ 成功 ｜ 2 个账号 ｜ 耗时 1s

━━━ 账号 1 · 晨曦（187****8664）━━━
💰 积分 · 3 个额度套餐（单位 credits）
   ① 剩余 1375.36 / 总额 2585 · 已用 1209.64
   ② 剩余 500 / 总额 500 · 已用 5
   ③ 剩余 1500 / 总额 1500 · 已用 15
   小计：剩余 3375.36 / 总额 4585 · 已用 1229.64
📊 用量：30 项资源 · 累计消耗 1875（数据延迟 2~3 小时）
🌱 成长：等级 3 · 连续签到 15 天 · 能量 3 · 累计签到 17 天

📖 名词解释
  · 积分：接口返回的多个额度套餐，格式为「剩余 / 总额（已用）」，各套餐独立结算。
  · 某套餐显示「剩余 0」是接口原值（额度未发放或已过期），不是算错；小计按各套餐原值求和。
  · 用量：资源项数 + 累计消耗量，统计有 2~3 小时延迟。
  · 成长：等级、连续签到天数、能量（成长中心可消耗）、累签（历史累计签到天数）。
```

完整签到时还会多出 `✅ 每日签到`、`✅ 成长任务：完成 N/M`、`🎁 本次领取奖励 N 项`、`⚠️ 需手动处理`。

**几个常见提示**：

| 提示 | 含义 | 要不要处理 |
| --- | --- | --- |
| `桌面专属任务已跳过` | 有两项任务必须在 Windows 客户端做一次，Linux/Docker 自动跳过 | 正常，无需处理 |
| `PushPlus 未送达：账户未实名认证` | 引擎自带的附加推送通道失败；报告本身已由插件直接发到 QQ | 在设置页清空 `PushPlus Token` 即可消除 |
| `执行超过 N 秒，已强制终止` | 单次任务超时 | 调大 `timeout`，或看 `/wb log` 排查 |

报告长度由 `reply_chars` 控制（建议 ≥ 2000）；设太小会先丢掉名词解释，账号数据始终优先保留。原始完整日志用 `/wb log [行数]` 查看。

---

## 6. 从独立 Docker 容器迁移

1. `docker compose -p workbuddy-auto -f <compose.yaml> down`（或 Unraid Compose Manager 里 Stop + Remove），**必须先停**，否则同一账号双跑会互相覆盖刷新令牌。
2. 把旧容器 `/data/.token` 内容复制成插件数据目录的 `tokens.txt`，把 `wb_refresh_tokens.json` 复制过去可保留续期状态。
3. 安装本插件 → 设置页填 QQ 号与适配器 ID → 「发送测试消息」→ `/wb run` 验证。
4. 确认正常后可删除旧 appdata 目录（保留凭据备份直到验证完成）。

> 注意：同一时间只允许一个实例跑定时。GitHub Actions 版本也要停掉。

---

## 7. 文件结构

```text
astrbot_plugin_workbuddy_auto/
├─ main.py                       # Star 插件：指令、定时、子进程、Web API、配置链接
├─ core/daily.py                 # 原签到引擎（仅新增 WORKBUDDY_DATA_DIR 数据目录支持）
├─ views/settings/index.html     # WebUI 内嵌设置页
├─ settings.html                 # 独立设置页（同一份页面，脱离 AstrBot 也能用）
├─ .astrbot-plugin/i18n/zh-CN.json
├─ _conf_schema.json             # WebUI 插件配置表单
├─ metadata.yaml                 # 插件元数据
├─ plugin-market.json            # 插件市场源（可作为「配置链接」添加）
├─ config.example.json           # 可下载的配置模板
├─ tokens.example.txt            # 令牌文件模板
├─ requirements.txt
└─ README.md
```

## 8. 安全说明

- 令牌、手机号在日志与 QQ 回复中都会被脱敏；但**导出的配置 JSON 含明文令牌**，请妥善保管。
- 配置链接请指向可信地址；插件会下载并应用其中的配置（包括令牌）。
- `admin_qqs` 建议填自己的 QQ，避免他人在群里触发签到或查看状态。

原项目 MIT License · 本插件封装部分同样以 MIT 方式提供。
