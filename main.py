# -*- coding: utf-8 -*-
"""
AstrBot 插件：WorkBuddy 自动签到 / 成长任务 / 领奖

原项目：https://github.com/gdemoni/workbuddy-Auto （MIT）

设计说明
--------
原 daily.py 是一个同步命令行脚本，模块加载时就会读取凭据、发起网络请求，
并且内部是 requests + time.sleep 的阻塞式流程。若直接 import 到插件进程，
会污染 AstrBot 的事件循环（一次签到可能跑十几分钟）。

因此这里把 daily.py 原样保留为「引擎」放在 core/ 下，插件只负责：
  - 指令解析（/wb run、/wb query ...）
  - 北京时间定时触发（07:00 / 12:00 / 23:30，可配置）
  - 凭据注入与持久化（data/plugin_data/...，自动续期不丢）
  - 结果摘要、脱敏日志、AstrBot 主动消息推送

引擎通过 asyncio 子进程调用，超时可终止；同一时刻只允许一个任务运行。

v1.1 新增：
  - WebUI 设置页（views/settings + context.register_web_api + bridge）
  - 「配置链接下载配置」：从 http(s) 链接拉取 JSON 配置并覆盖式应用
  - notify_qqs / umo_platform：直接填 QQ 号即可推送，无需手写 UMO

v1.2 新增：
  - 易读报告：按账号分组、套餐逐条解释、昵称/脱敏手机号标识、名词释义
  - 横幅/环境提示/附加通道报错单独归类，不再混进账号数据

v1.3 新增：
  - skill_share 成长任务：真实分享渠道 + 官方事件 + 进度回读，达标才领奖
  - 报告新增「分享技能给好友」完成行
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:  # 不同 AstrBot 版本的导出位置略有差异
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover
    from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_workbuddy_auto"
CST = timezone(timedelta(hours=8))  # 北京时间，无夏令时，比 zoneinfo 更少依赖
ENGINE_FILE = Path(__file__).parent / "core" / "daily.py"

# 允许由设置页 / 配置链接写入并覆盖 WebUI 配置的键。
# tokens 单独存到数据目录的 tokens.txt，不写进 config.json，避免凭据重复落盘。
CONFIG_KEYS = (
    "notify_qqs", "umo_platform", "config_url", "notify_umos", "admin_qqs",
    "schedule_enabled", "schedule_times", "notify_on_schedule", "no_desktop",
    "timeout", "reply_chars", "pushplus_token", "wecom_webhook", "notify_url",
    "notify_kind",
)

TOKEN_PATTERN = re.compile(r"eyJ[A-Za-z0-9_\-]{16,}[A-Za-z0-9_.\-=]*")
PHONE_PATTERN = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

SUMMARY_KEYS = (
    "[积分]", "[用量]", "[成长]", "[完成]", "积分:", "用量:", "成长:",
    "完成", "续期", "未找到账号", "失败", "错误", "警告", "Traceback", "Error",
    "无待领奖励", "跳过", "账号数",
)

# ---- 报告解析 -------------------------------------------------------------
# 引擎日志行形如：[12:58:57][账号1] 💰 积分: 余...; 余...
REPORT_LINE_RE = re.compile(r"^\[\d{1,2}:\d{2}:\d{2}\]\[([^\]]+)\]\s*(.*)$")
REPORT_CREDIT_RE = re.compile(
    r"余\s*([\d.]*)\s*/\s*总\s*([\d.]*)\s*\(\s*已用\s*([\d.]*)\s*([A-Za-z]*)\s*\)"
)
REPORT_USAGE_RE = re.compile(r"资源\s*(\d+)\s*项\s*/\s*总用量\s*([\d.]+)")
REPORT_GROWTH_RE = re.compile(
    r"等级\s*(\S+)\s+连签\s*(\S+?)\s*天\s+能量\s*(\S+)\s+累签\s*(\S+?)\s*天"
)
REPORT_FINISH_RE = re.compile(r"完成\s*(\d+)\s*/\s*(\d+)\s+等级\s*(\S+)\s+剩余:\s*(.*)")
REPORT_SIGN_RE = re.compile(r"签到成功\s*\+?([\d.]+)\s*积分\s*连签\s*(\d+)\s*天")
REPORT_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"

REPORT_LEGEND = (
    "📖 名词解释\n"
    "  · 积分：接口返回的多个额度套餐，格式为「剩余 / 总额（已用）」，单位 credits，各套餐独立结算。\n"
    "  · 某套餐显示「剩余 0」是接口原值（额度未发放或已过期），不是算错；小计按各套餐原值求和。\n"
    "  · 用量：资源项数 + 累计消耗量，统计有 2~3 小时延迟。\n"
    "  · 成长：等级、连续签到天数、能量（成长中心可消耗）、累签（历史累计签到天数）。"
)

SUBCMD_ALIASES = {
    "help": "help", "h": "help", "帮助": "help", "?": "help",
    "run": "run", "sign": "run", "qd": "run", "签到": "run", "执行": "run", "跑": "run",
    "query": "query", "cx": "query", "查询": "query", "积分": "query", "状态查询": "query",
    "refresh": "refresh", "续期": "refresh", "token": "refresh",
    "status": "status", "zt": "status", "状态": "status",
    "accounts": "accounts", "账号": "accounts", "acct": "accounts",
    "sub": "sub", "订阅": "sub", "subscribe": "sub",
    "unsub": "unsub", "取消订阅": "unsub", "退订": "unsub",
    "log": "log", "日志": "log", "logs": "log",
    "stop": "stop", "停止": "stop", "cancel": "stop",
    "config": "config", "配置": "config", "设置": "config", "cfg": "config",
    "qq": "qq", "推送qq": "qq", "收信qq": "qq",
}

HELP_TEXT = """[WorkBuddy 自动签到]
  /wb help                 帮助
  /wb run [账号序号]        立即执行完整签到（如 /wb run 2）
  /wb query                查询积分 / 用量 / 成长（分组易读报告）
  /wb refresh              仅刷新全部账号 Token
  /wb status               运行状态、账号数、下次定时
  /wb accounts             已保存账号（脱敏）
  /wb sub / /wb unsub      订阅 / 取消订阅定时报告
  /wb log [行数]           最近一次引擎日志
  /wb stop                 终止正在运行的任务
  /wb qq <QQ号>            设置发消息的 QQ 号
  /wb config               查看配置来源与推送目标
  /wb config <链接>        从配置链接下载并应用配置
  /wb config pull          重新下载已保存的链接
  /wb config clear         清除覆盖，恢复 WebUI 配置

首次使用：WebUI → 插件 → WorkBuddy 自动签到 →「设置」，
填「账号令牌」（手机号:AT:RT，每行一个）和「发消息的 QQ 号」，
点「发送测试消息」验证；再 /wb run 手动跑一次。

完整流程耗时较长（多账号可到十几分钟），请勿频繁触发。
原项目：https://github.com/gdemoni/workbuddy-Auto"""


class WorkBuddyAutoPlugin(Star):
    """把 workbuddy-Auto 的签到引擎挂到 AstrBot 上。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._remote_config = {}
        self.data_dir = self._resolve_data_dir()
        self._load_config_file()
        self._lock = asyncio.Lock()
        self._proc = None
        self._scheduler_task = None
        self._register_web_apis()
        self._start_scheduler()
        logger.info("[WorkBuddyAuto] 插件已加载，引擎: %s，数据目录: %s", ENGINE_FILE, self.data_dir)

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self):
        """AstrBot v4 生命周期钩子；补启定时任务并异步加载配置链接。"""
        self._start_scheduler()
        url = str(self._opt("config_url", "") or "").strip()
        if url:
            asyncio.create_task(self._auto_pull_config(url))

    async def _auto_pull_config(self, url):
        ok, message, _ = await self._pull_config(url)
        logger.info("[WorkBuddyAuto] 启动加载配置链接: %s", message)

    async def terminate(self):
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._proc and getattr(self._proc, "returncode", None) is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        logger.info("[WorkBuddyAuto] 插件已卸载")

    def _start_scheduler(self):
        if self._scheduler_task and not self._scheduler_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._scheduler_task = loop.create_task(self._scheduler_loop())

    # ------------------------------------------------------------------ 基础工具
    def _resolve_data_dir(self):
        try:
            path = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            try:
                path = Path(StarTools.get_data_dir())
            except Exception:
                path = Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME
        try:
            path.mkdir(parents=True, exist_ok=True)
            (path / "logs").mkdir(exist_ok=True)
        except Exception:
            logger.exception("[WorkBuddyAuto] 创建数据目录失败")
        return path

    # ------------------------------------------------------------------ 配置覆盖 / 配置链接
    def _load_config_file(self):
        """设置页与「配置链接下载配置」生成的 config.json 优先于 WebUI 配置。"""
        data = self._read_json("config.json", {})
        self._remote_config = data if isinstance(data, dict) else {}

    def _save_settings(self, payload):
        """把设置页 / 配置链接的内容写入数据目录；tokens 单独写 tokens.txt。"""
        if not isinstance(payload, dict):
            raise ValueError("配置必须是 JSON 对象")
        clean = {}
        for key in CONFIG_KEYS:
            if key not in payload or payload[key] is None:
                continue
            value = payload[key]
            if key in ("notify_qqs", "admin_qqs", "notify_umos") and isinstance(value, (list, tuple)):
                value = ",".join(str(x) for x in value)
            clean[key] = value
        tokens = str(payload.get("tokens") or "").strip()
        if tokens:
            path = self.data_dir / "tokens.txt"
            path.write_text(tokens.rstrip() + "\n", encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        merged = dict(self._remote_config)
        merged.update(clean)
        self._write_json("config.json", merged)
        self._remote_config = merged
        return merged

    def _clear_settings(self):
        """清除设置页 / 配置链接写入的覆盖，恢复使用 WebUI 插件配置。"""
        self._remote_config = {}
        self._write_json("config.json", {})

    @staticmethod
    def _fetch_url_text(url, timeout=15):
        import urllib.request
        req = urllib.request.Request(
            url.strip(), headers={"User-Agent": "astrbot-plugin-workbuddy-auto/1.1"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024)
        return raw.decode("utf-8", errors="replace")

    def _apply_config_text(self, text):
        """配置内容可以是 JSON 对象，也可以是纯 手机号:AT:RT 令牌文本。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("配置内容为空")
        if text.startswith("{"):
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("配置 JSON 顶层必须是对象")
        elif ":" in text and not text.lstrip().startswith("<"):
            payload = {"tokens": text}
        else:
            raise ValueError("无法识别配置：应为 JSON 对象或 手机号:AT:RT 文本")
        self._save_settings(payload)
        keys = [k for k in CONFIG_KEYS if k in payload]
        if str(payload.get("tokens") or "").strip():
            keys.append("tokens")
        return {"keys": keys, "accounts": self._account_count()}

    async def _pull_config(self, url):
        """从配置链接下载配置并应用，返回 (是否成功, 文本, 详情)。"""
        url = str(url or "").strip()
        if not re.match(r"^https?://", url, re.IGNORECASE):
            return False, "配置链接必须以 http:// 或 https:// 开头", {}
        try:
            text = await asyncio.to_thread(self._fetch_url_text, url)
            info = self._apply_config_text(text)
            message = "已从配置链接下载并应用 %d 项配置（当前 %d 个账号）" % (
                len(info["keys"]), info["accounts"]
            )
            return True, message, info
        except Exception as exc:
            return False, "下载配置失败: %s" % self._mask(str(exc)), {}

    def _config_status_text(self):
        _, source = self._tokens()
        return "\n".join([
            "[WorkBuddy 配置]",
            "凭据来源: %s" % (source or "未配置"),
            "账号数量: %s" % self._account_count(),
            "发消息 QQ: %s" % (str(self._opt("notify_qqs", "") or "") or "未设置"),
            "平台适配器: %s" % self._opt("umo_platform", "aiocqhttp"),
            "推送会话: %s 个" % len(self._push_targets()),
            "配置链接: %s" % (str(self._opt("config_url", "") or "") or "未设置"),
            "配置覆盖: %s" % (self.data_dir / "config.json"),
            "设置页面: WebUI → 插件 → WorkBuddy 自动签到 → 设置",
            "/wb config <链接>   下载并应用配置",
            "/wb config pull     重新下载已保存的链接",
            "/wb config clear    清除覆盖，恢复 WebUI 配置",
            "/wb qq <QQ号>       设置发消息的 QQ 号",
        ])

    def _opt(self, key, default=None):
        """读取配置：设置页/配置链接覆盖 > WebUI 插件配置 > 默认值。"""
        overrides = getattr(self, "_remote_config", {}) or {}
        if key in overrides:
            value = overrides[key]
        else:
            try:
                value = self.config.get(key, default)
            except Exception:
                value = default
        return default if value is None else value

    def _bool(self, key, default=False):
        value = self._opt(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")

    def _int(self, key, default, low, high):
        try:
            value = int(self._opt(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _read_json(self, name, default):
        try:
            path = self.data_dir / name
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[WorkBuddyAuto] 读取 %s 失败", name)
        return default

    def _write_json(self, name, data):
        try:
            (self.data_dir / name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.exception("[WorkBuddyAuto] 写入 %s 失败", name)

    @staticmethod
    def _mask(text):
        """日志与回复统一脱敏，避免 Token / 手机号通过 QQ 泄露。"""
        text = TOKEN_PATTERN.sub("eyJ***(已隐藏)", text or "")
        return PHONE_PATTERN.sub(
            lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:], text
        )

    def _tokens(self):
        """优先读取数据目录 tokens.txt，其次用插件配置。返回 (凭据, 来源)。"""
        token_file = self.data_dir / "tokens.txt"
        try:
            if token_file.is_file():
                value = token_file.read_text(encoding="utf-8").strip()
                if value:
                    return value, "tokens.txt"
        except Exception:
            logger.exception("[WorkBuddyAuto] 读取 tokens.txt 失败")
        value = str(self._opt("tokens", "") or "").strip()
        return value, "插件配置" if value else ""

    def _account_count(self):
        store = self._read_json("wb_refresh_tokens.json", {})
        if isinstance(store, dict) and store:
            return len(store)
        tokens, _ = self._tokens()
        return len([x for x in re.split(r"[@\r\n]+", tokens) if x.strip()])

    def _times(self):
        raw = str(self._opt("schedule_times", "07:00,12:00,23:30") or "")
        result = []
        for part in re.split(r"[,，\s]+", raw.strip()):
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
            if not m:
                continue
            hour, minute = int(m.group(1)), int(m.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                result.append("%02d:%02d" % (hour, minute))
        return sorted(set(result))

    def _next_run(self, now=None):
        now = now or datetime.now(CST)
        times = self._times()
        if not times:
            return "无有效时间点"
        current = now.strftime("%H:%M")
        for value in times:
            if value > current:
                return "%s %s" % (now.strftime("%Y-%m-%d"), value)
        return "%s %s" % ((now + timedelta(days=1)).strftime("%Y-%m-%d"), times[0])

    # ------------------------------------------------------------------ 订阅 / 推送
    def _subscribers(self):
        value = self._read_json("subscribers.json", [])
        return [str(x) for x in value] if isinstance(value, list) else []

    def _push_targets(self):
        targets = list(self._subscribers())
        platform = str(self._opt("umo_platform", "aiocqhttp") or "aiocqhttp").strip()
        for item in re.split(r"[,，;\s]+", str(self._opt("notify_qqs", "") or "")):
            item = item.strip()
            if not item:
                continue
            if item.lower().startswith("group:"):
                gid = item.split(":", 1)[1].strip()
                if gid.isdigit():
                    targets.append("%s:GroupMessage:%s" % (platform, gid))
            elif item.isdigit():
                targets.append("%s:FriendMessage:%s" % (platform, item))
            elif re.fullmatch(r"[A-Za-z0-9_-]+:(?:Friend|Group)Message:\d+", item):
                targets.append(item)  # 也允许直接填完整 UMO
        raw = str(self._opt("notify_umos", "") or "")
        targets.extend(x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip())
        return list(dict.fromkeys(targets))

    async def _broadcast(self, text, targets=None):
        text = self._mask(text)
        if len(text) > 1800:
            text = text[:1800] + "\n(内容已截断，完整日志见 /wb log)"
        for umo in (targets if targets is not None else self._push_targets()):
            try:
                try:
                    chain = MessageChain().message(text)
                except Exception:
                    from astrbot.api.message_components import Plain
                    chain = MessageChain([Plain(text)])
                await self.context.send_message(umo, chain)
            except Exception as exc:
                logger.warning("[WorkBuddyAuto] 推送到 %s 失败: %s", umo, exc)

    # ------------------------------------------------------------------ 引擎调用
    def _build_env(self, tokens):
        env = os.environ.copy()
        # 抹掉可能从宿主继承的推送/凭据变量，全部由插件配置决定
        for key in (
            "WORKBUDDY_REFRESH_TOKEN", "PUSHPLUS_TOKEN", "WECOM_WEBHOOK",
            "NOTIFY_URL", "NOTIFY_KIND", "ASTRBOT_CONFIG",
        ):
            env.pop(key, None)
        env["WORKBUDDY_REFRESH_TOKEN"] = tokens
        env["WORKBUDDY_DATA_DIR"] = str(self.data_dir)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        for cfg_key, env_key in (
            ("pushplus_token", "PUSHPLUS_TOKEN"),
            ("wecom_webhook", "WECOM_WEBHOOK"),
            ("notify_url", "NOTIFY_URL"),
            ("notify_kind", "NOTIFY_KIND"),
        ):
            value = str(self._opt(cfg_key, "") or "").strip()
            if value:
                env[env_key] = value
        return env

    def _append_log(self, label, output, rc):
        stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        safe = self._mask(output)
        try:
            logs = self.data_dir / "logs"
            logs.mkdir(exist_ok=True)
            with (logs / (datetime.now(CST).strftime("%Y-%m-%d") + ".log")).open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\n===== %s | %s | exit=%s =====\n%s\n" % (stamp, label, rc, safe))
            (logs / "latest.log").write_text(safe[-100000:], encoding="utf-8")
        except Exception:
            logger.exception("[WorkBuddyAuto] 写日志失败")

    # ------------------------------------------------------------------ 易读报告
    @staticmethod
    def _fmt_num(value):
        """把 1375.36000266 规范成 1375.36；空串（引擎把 0 截没了）补成 0。"""
        text = str(value or "").strip()
        if not text:
            return "0"
        try:
            return ("%.2f" % float(text)).rstrip("0").rstrip(".") or "0"
        except ValueError:
            return text

    def _account_labels(self):
        """返回 [(昵称, 脱敏手机号)]，顺序与 tokens.txt / 引擎账号序号一致。"""
        import base64
        tokens, _ = self._tokens()
        labels = []
        for line in re.split(r"[@\r\n]+", tokens or ""):
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            phone = parts[0].strip()
            phone_show = ""
            if re.fullmatch(r"1[3-9]\d{9}", phone):
                phone_show = phone[:3] + "****" + phone[-4:]
            elif re.fullmatch(r"\d{3}\*+\d{4}", phone):
                phone_show = phone  # 旧容器导出的首段本来就是脱敏格式
            nick = ""
            for seg in parts[1:]:
                if not seg.startswith("eyJ") or "." not in seg:
                    continue
                try:
                    chunk = seg.split(".")[1]
                    chunk += "=" * (4 - len(chunk) % 4)
                    nick = str(json.loads(base64.urlsafe_b64decode(chunk)).get("nickname") or "")
                except Exception:
                    pass
                break
            labels.append((nick[:12], phone_show))
        return labels

    def _parse_credits(self, text):
        """'余1375.36/总2585(已用1209.64 credits); ...' -> [(余, 总, 已用), ...]"""
        result = []
        for item in (text or "").split(";"):
            m = REPORT_CREDIT_RE.search(item)
            if m:
                result.append(tuple(self._fmt_num(x) for x in m.group(1, 2, 3)))
        return result

    @staticmethod
    def _sum_credits(items):
        totals = []
        for col in range(3):
            try:
                value = sum(float(x[col]) for x in items)
            except (ValueError, TypeError):
                value = 0
            totals.append(("%.2f" % value).rstrip("0").rstrip(".") or "0")
        return tuple(totals)

    def _format_report(self, output, max_chars, only_index=0):
        """把引擎原始日志重排成「按账号分组 + 字段释义」的报告。"""
        labels = self._account_labels()
        accounts = {}
        notices, notes, errors = [], [], []

        def bucket(idx):
            return accounts.setdefault(idx, {
                "credits": [], "usage": "", "growth": "", "finish": "",
                "sign": "", "share": "", "claims": 0, "manual": [], "issues": [],
            })

        for raw in (output or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            m = REPORT_LINE_RE.match(line)
            body = m.group(2).strip() if m else line
            idx = None
            if m and m.group(1).startswith("账号") and m.group(1)[2:].isdigit():
                idx = int(m.group(1)[2:])
                if only_index:  # --only N 时引擎重新从 1 编号，映射回真实账号序号
                    idx = only_index + idx - 1

            if idx is None:  # 全局行：横幅、环境提示、通知、异常
                if "账号数" in body:
                    continue
                if body.startswith(("╔", "║", "╚", "╭", "┌", "│", "└")):
                    continue
                if "非Windows环境" in body or "自动跳过桌面任务" in body:
                    notes.append("ℹ️ 桌面专属任务已跳过（这两项需在 Windows 客户端完成一次）")
                elif body.startswith("📢"):
                    notices.append(body)
                elif "Traceback" in body or body.startswith("Error") or "强制终止" in body:
                    errors.append(body)
                elif body.startswith("📝") or "需要续期" in body or "token已自动续期" in body:
                    notes.append("ℹ️ " + body)
                continue

            acc = bucket(idx)
            if body.startswith("💰 积分"):
                acc["credits"] = self._parse_credits(body.split(":", 1)[-1])
            elif body.startswith("📊 用量"):
                um = REPORT_USAGE_RE.search(body)
                acc["usage"] = (
                    "📊 用量：%s 项资源 · 累计消耗 %s（数据延迟 2~3 小时）" %
                    (um.group(1), self._fmt_num(um.group(2)))
                ) if um else "📊 用量：" + body.split(":", 1)[-1].strip()
            elif body.startswith("🌱 成长"):
                gm = REPORT_GROWTH_RE.search(body)
                acc["growth"] = (
                    "🌱 成长：等级 %s · 连续签到 %s 天 · 能量 %s · 累计签到 %s 天" %
                    gm.group(1, 2, 3, 4)
                ) if gm else "🌱 成长：" + body.split(":", 1)[-1].strip()
            elif body.startswith("🏁"):
                fm = REPORT_FINISH_RE.search(body)
                if fm:
                    acc["finish"] = "✅ 成长任务：完成 %s/%s · 等级 %s · 未完成：%s" % (
                        fm.group(1), fm.group(2), fm.group(3), fm.group(4).strip() or "无"
                    )
            elif "技能分享" in body and "✅" in body:
                acc["share"] = "✅ 分享技能给好友：已完成（真实分享渠道 + 官方事件）"
            elif "签到成功" in body:
                sm = REPORT_SIGN_RE.search(body)
                acc["sign"] = (
                    "✅ 每日签到：+%s 积分 · 连签 %s 天" % (self._fmt_num(sm.group(1)), sm.group(2))
                ) if sm else "✅ " + body.lstrip("✅ ").strip()
            elif "领奖[" in body or body.startswith("🎁领奖"):
                if "积分" in body or "能量" in body:
                    acc["claims"] += 1
            elif "新任务需手动" in body or "未覆盖新任务" in body:
                acc["manual"].append(body.replace("⚠️", "").strip())
            elif "失败" in body or "错误" in body or "⚠️" in body:
                acc["issues"].append(body)

        # 非查询类任务（如 --refresh）没有结构化数据，退回关键行列表
        if not any(
            a["credits"] or a["usage"] or a["growth"] or a["finish"] or a["sign"]
            for a in accounts.values()
        ):
            picked = []
            for line in (output or "").splitlines():
                item = line.strip()
                if not item or item.startswith(("╔", "║", "╚", "╭", "┌", "│", "└")):
                    continue
                if any(key in item for key in SUMMARY_KEYS):
                    picked.append(re.sub(r"^\[\d{1,2}:\d{2}:\d{2}\]\[[^\]]+\]\s*", "", item))
            if not picked:
                picked = [x.strip() for x in (output or "").splitlines() if x.strip()][-15:]
            return self._mask("\n".join(picked[-40:])[:max_chars]) or "(引擎无输出)"

        core = []
        for idx in sorted(accounts):
            acc = accounts[idx]
            nick, phone = labels[idx - 1] if 0 < idx <= len(labels) else ("", "")
            title = "账号 %d" % idx
            if nick:
                title += " · " + nick
            if phone:
                title += "（%s）" % phone
            lines = ["━━━ %s ━━━" % title]
            if acc["credits"]:
                items = acc["credits"]
                lines.append("💰 积分 · %d 个额度套餐（单位 credits）" % len(items))
                for i, (remain, total, used) in enumerate(items):
                    mark = REPORT_CIRCLED[i] if i < len(REPORT_CIRCLED) else "%d." % (i + 1)
                    lines.append("   %s 剩余 %s / 总额 %s · 已用 %s" % (mark, remain, total, used))
                if len(items) > 1:
                    lines.append("   小计：剩余 %s / 总额 %s · 已用 %s" % self._sum_credits(items))
            for key in ("usage", "growth", "sign", "finish", "share"):
                if acc[key]:
                    lines.append(acc[key])
            if acc["claims"]:
                lines.append("🎁 本次领取奖励 %d 项" % acc["claims"])
            if acc["manual"]:
                lines.append("⚠️ 需手动处理：")
                lines.extend("   · " + x for x in acc["manual"][:3])
            if acc["issues"]:
                lines.append("⚠️ 异常提示：")
                lines.extend("   · " + x for x in acc["issues"][:3])
            core.append("\n".join(lines))

        optional = []
        if notices:
            lines = ["📢 附加通知（引擎自带通道，不是本条 QQ 消息）"]
            for item in notices[:4]:
                if "失败" not in item:
                    lines.append("  · " + item)
                elif "PushPlus" in item and "实名" in item:
                    lines.append(
                        "  · PushPlus 未送达：账户未实名认证 → 建议在设置页清空该 Token；"
                        "报告已由插件直接发到 QQ，不影响签到"
                    )
                else:
                    lines.append("  · %s（附加通道，不影响签到结果）" % item)
            optional.append("\n".join(lines))
        if errors:
            optional.append("⚠️ 引擎异常\n" + "\n".join("  · " + x for x in errors[:4]))
        if notes:
            optional.append("\n".join(notes[:4]))
        optional.append(REPORT_LEGEND)

        body = "\n\n".join(core)
        for extra in optional:
            if len(body) + len(extra) + 2 <= max_chars:
                body += "\n\n" + extra
        body = self._mask(body)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n…(已截断，/wb log 查看完整日志)"
        return body

    def _summarize(self, output, max_chars, only_index=0):
        try:
            return self._format_report(output, max_chars, only_index)
        except Exception:
            logger.exception("[WorkBuddyAuto] 报告格式化失败，回退原始摘要")
            picked = [x.strip() for x in (output or "").splitlines() if x.strip()][-20:]
            return self._mask("\n".join(picked))[:max_chars] or "(引擎无输出)"

    async def _run_engine(self, args, label):
        """执行引擎，返回 (是否成功, 摘要文本, 原始输出)。"""
        if self._lock.locked():
            return False, "已有任务正在运行，请稍后再试（/wb status 查看状态）", ""
        async with self._lock:
            tokens, source = self._tokens()
            if not tokens:
                return False, (
                    "未配置账号凭据：在插件配置 tokens 填入 手机号:AT:RT，"
                    "或写入数据目录的 tokens.txt"
                ), ""
            if not ENGINE_FILE.is_file():
                return False, "引擎文件缺失: %s" % ENGINE_FILE, ""
            try:
                import requests  # noqa: F401  引擎由同一解释器以子进程运行
            except ImportError:
                return False, (
                    "引擎缺少依赖 requests：请在 AstrBot 容器内执行 "
                    "pip install requests 后重载插件（插件已声明 requirements.txt）"
                ), ""

            env = self._build_env(tokens)
            cmd = [sys.executable, str(ENGINE_FILE)] + list(args)
            timeout = self._int("timeout", 1800, 60, 7200)
            started = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    cwd=str(self.data_dir),
                )
                self._proc = proc
                out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                rc = proc.returncode
            except asyncio.TimeoutError:
                if self._proc and getattr(self._proc, "returncode", None) is None:
                    try:
                        self._proc.kill()
                        await self._proc.communicate()
                    except Exception:
                        pass
                out_bytes, rc = b"", -9
            except Exception as exc:
                return False, "引擎启动失败: %s" % exc, ""
            finally:
                self._proc = None

            output = out_bytes.decode("utf-8", errors="replace") if out_bytes else ""
            if rc == -9:
                output += "\n执行超过 %s 秒，已强制终止" % timeout
            self._append_log("%s [凭据来源: %s] [参数: %s]" % (label, source, " ".join(args)), output, rc)

            state = self._read_json("state.json", {})
            if not isinstance(state, dict):
                state = {}
            state["last_run"] = {
                "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                "label": label,
                "rc": rc,
                "ok": rc == 0,
                "seconds": int(time.time() - started),
            }
            self._write_json("state.json", state)

            only_index = 0
            if "--only" in args:
                try:
                    only_index = int(args[args.index("--only") + 1])
                except (ValueError, IndexError):
                    pass
            summary = self._summarize(
                output, self._int("reply_chars", 2000, 200, 4000), only_index
            )
            seconds = int(time.time() - started)
            status = "✅ 成功" if rc == 0 else "❌ 失败（退出码 %s）" % rc
            scope = (
                "单账号（第 %d 个）" % only_index
                if only_index else "%d 个账号" % self._account_count()
            )
            head = "【WorkBuddy %s】%s ｜ %s ｜ 耗时 %ss\n\n" % (
                label, status, scope, seconds
            )
            return rc == 0, head + summary, output

    async def _scheduler_loop(self):
        logger.info("[WorkBuddyAuto] 定时循环已启动")
        while True:
            try:
                await asyncio.sleep(20)
                if not self._bool("schedule_enabled", True):
                    continue
                now = datetime.now(CST)
                hhmm = now.strftime("%H:%M")
                if hhmm not in self._times():
                    continue
                key = now.strftime("%Y-%m-%d") + " " + hhmm
                state = self._read_json("state.json", {})
                if not isinstance(state, dict):
                    state = {}
                if state.get("last_schedule_key") == key:
                    continue
                # 先落盘再执行，避免重启后同一时间点重复跑
                state["last_schedule_key"] = key
                self._write_json("state.json", state)
                logger.info("[WorkBuddyAuto] 定时触发 %s", key)

                args = ["--no-desktop"] if self._bool("no_desktop", True) else []
                ok, text, _ = await self._run_engine(args, "定时签到")
                if self._bool("notify_on_schedule", True):
                    targets = self._push_targets()
                    if targets:
                        await self._broadcast(
                            "[WorkBuddy 定时签到 %s]\n%s" % (key, text), targets
                        )
                    else:
                        logger.info("[WorkBuddyAuto] 无订阅会话，跳过推送（可发 /wb sub）")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[WorkBuddyAuto] 定时循环异常")

    # ------------------------------------------------------------------ 设置页 Web API
    def _register_web_apis(self):
        """AstrBot >=4.29 的插件视图后端接口；旧版本自动跳过，不影响主功能。"""
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.info("[WorkBuddyAuto] 当前 AstrBot 无插件视图 API，设置页可用独立模式")
            return
        routes = (
            ("settings/get", self.api_get_settings, ["GET"], "读取 WorkBuddy 设置"),
            ("settings/save", self.api_save_settings, ["POST"], "保存 WorkBuddy 设置"),
            ("settings/export", self.api_export_settings, ["GET"], "导出配置 JSON"),
            ("settings/fetch", self.api_fetch_settings, ["POST"], "从配置链接下载配置"),
            ("settings/test", self.api_test_push, ["POST"], "发送测试消息"),
            ("status", self.api_status, ["GET"], "查看运行状态"),
        )
        for endpoint, handler, methods, desc in routes:
            try:
                register("/%s/%s" % (PLUGIN_NAME, endpoint), handler, methods, desc)
            except Exception as exc:
                logger.warning("[WorkBuddyAuto] 注册 Web API %s 失败: %s", endpoint, exc)

    @staticmethod
    def _api_ok(data):
        try:
            from astrbot.api.web import json_response
            return json_response(data)
        except Exception:
            return data

    @staticmethod
    def _api_err(message, status_code=400):
        try:
            from astrbot.api.web import error_response
            return error_response(message, status_code=status_code)
        except Exception:
            return {"status": "error", "message": message}

    @staticmethod
    async def _api_body():
        try:
            from astrbot.api.web import request as web_request
            return await web_request.json(default={})
        except Exception:
            try:
                from quart import request as quart_request
                return await quart_request.get_json() or {}
            except Exception:
                return {}

    async def api_get_settings(self):
        """令牌只返回是否已配置，不把明文回传给 iframe。"""
        tokens, source = self._tokens()
        return self._api_ok({
            "tokens_set": bool(tokens),
            "tokens_source": source,
            "account_count": self._account_count(),
            "notify_qqs": str(self._opt("notify_qqs", "") or ""),
            "umo_platform": str(self._opt("umo_platform", "aiocqhttp") or "aiocqhttp"),
            "admin_qqs": str(self._opt("admin_qqs", "") or ""),
            "schedule_enabled": self._bool("schedule_enabled", True),
            "schedule_times": str(self._opt("schedule_times", "07:00,12:00,23:30") or ""),
            "notify_on_schedule": self._bool("notify_on_schedule", True),
            "notify_umos": str(self._opt("notify_umos", "") or ""),
            "config_url": str(self._opt("config_url", "") or ""),
            "status": self._status_text(),
        })

    async def api_save_settings(self):
        payload = await self._api_body()
        if not isinstance(payload, dict):
            return self._api_err("配置格式错误：需要 JSON 对象")
        try:
            merged = self._save_settings(payload)
        except Exception as exc:
            return self._api_err("保存失败: %s" % exc)
        return self._api_ok({
            "saved": True, "keys": sorted(merged.keys()), "status": self._status_text()
        })

    async def api_export_settings(self):
        """导出完整配置（含明文令牌），由前端生成文件下载。"""
        tokens, _ = self._tokens()
        data = {
            "_plugin": PLUGIN_NAME,
            "_version": "1.1.0",
            "_exported": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "_warning": "本文件含明文账号令牌，请勿公开分享",
            "tokens": tokens,
        }
        for key in CONFIG_KEYS:
            data[key] = self._opt(key, "")
        return self._api_ok(data)

    async def api_fetch_settings(self):
        """服务端下载配置链接（不受浏览器 CORS 限制），解析后直接应用。"""
        payload = await self._api_body()
        url = str(payload.get("url") or self._opt("config_url", "") or "").strip()
        if not url:
            return self._api_err("请填写配置链接")
        if payload.get("save_url"):
            try:
                self._save_settings({"config_url": url})
            except Exception:
                pass
        ok, message, _ = await self._pull_config(url)
        if not ok:
            return self._api_err(message)
        return self._api_ok({"ok": True, "message": message, "status": self._config_status_text()})

    async def api_test_push(self):
        targets = self._push_targets()
        if not targets:
            return self._api_err("尚未设置发消息的 QQ 号，也没有订阅会话")
        await self._broadcast(
            "[WorkBuddy] 设置页测试消息：看到这条说明 QQ 推送已配置正确。", targets
        )
        return self._api_ok({"sent": True, "targets": len(targets)})

    async def api_status(self):
        return self._api_ok({"status": self._config_status_text()})

    # ------------------------------------------------------------------ 指令
    def _allowed(self, event):
        raw = str(self._opt("admin_qqs", "") or "")
        allow = {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}
        if not allow:
            return True
        try:
            return str(event.get_sender_id()) in allow
        except Exception:
            return False

    @staticmethod
    def _parse(event):
        try:
            raw = getattr(event, "message_str", "") or ""
        except Exception:
            raw = ""
        if not raw and hasattr(event, "get_message_str"):
            try:
                raw = event.get_message_str() or ""
            except Exception:
                raw = ""
        text = re.sub(r"^[\s/!！.。、#]+", "", str(raw).strip())
        text = re.sub(r"^(?:workbuddy|wb签到|wb)\s*", "", text, flags=re.IGNORECASE)
        return text.split()

    def _status_text(self):
        _, source = self._tokens()
        enabled = self._bool("schedule_enabled", True)
        times = self._times()
        state = self._read_json("state.json", {})
        last = state.get("last_run", {}) if isinstance(state, dict) else {}
        if not isinstance(last, dict):
            last = {}
        return "\n".join([
            "[WorkBuddy 自动签到 状态]",
            "引擎文件: %s" % ("正常" if ENGINE_FILE.is_file() else "缺失，请检查 core/daily.py"),
            "凭据来源: %s" % (source or "未配置"),
            "账号数量: %s" % self._account_count(),
            "当前任务: %s" % ("运行中" if self._lock.locked() else "空闲"),
            "定时执行: %s (%s)" % ("开启" if enabled else "关闭", ", ".join(times) or "无有效时间"),
            "下次执行: %s" % (self._next_run() if enabled else "已关闭"),
            "推送会话: %s 个" % len(self._push_targets()),
            "上次运行: %s | 退出码 %s | %s" % (
                last.get("time", "无"), last.get("rc", "-"), last.get("label", "-")
            ),
            "数据目录: %s" % self.data_dir,
        ])

    def _accounts_text(self):
        labels = self._account_labels()
        if not labels:
            return "尚未配置账号：在设置页或数据目录 tokens.txt 填入 手机号:AT:RT"
        store = self._read_json("wb_refresh_tokens.json", {})
        updated = [
            v.get("updated", "-") if isinstance(v, dict) else "-"
            for v in store.values()
        ] if isinstance(store, dict) else []
        lines = ["[已保存账号] 共 %d 个" % len(labels)]
        for index, (nick, phone) in enumerate(labels, 1):
            who = "%s（%s）" % (nick or "未命名", phone) if phone else (nick or "未命名")
            when = updated[index - 1] if index - 1 < len(updated) else "-"
            lines.append("%d. %s | 上次续期: %s" % (index, who, when))
        lines.append("提示：/wb run 2 可单独执行第 2 个账号")
        return "\n".join(lines)

    def _tail_log(self, count):
        path = self.data_dir / "logs" / "latest.log"
        if not path.is_file():
            return "暂无日志，先执行一次 /wb run"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return "读取日志失败: %s" % exc
        return self._mask("\n".join(lines[-count:])[-3000:]) or "(日志为空)"

    @filter.command("wb", alias={"workbuddy", "wb签到"})
    async def wb_command(self, event: AstrMessageEvent):
        if not self._allowed(event):
            yield event.plain_result("没有权限使用 WorkBuddy 指令（请在 admin_qqs 中加入自己的 QQ）")
            return

        parts = self._parse(event)
        raw_sub = parts[0].lower() if parts else "help"
        sub = SUBCMD_ALIASES.get(raw_sub, raw_sub)
        arg = parts[1] if len(parts) > 1 else ""

        if sub == "help":
            yield event.plain_result(HELP_TEXT)
            return

        if sub == "qq":
            qq = (arg or "").strip()
            if not qq or not re.fullmatch(r"[\d,，\s]+", qq):
                yield event.plain_result("用法：/wb qq 123456789（多个用逗号分隔）")
                return
            self._save_settings({"notify_qqs": qq})
            yield event.plain_result(
                "已设置发消息的 QQ：%s\n当前推送会话：%d 个\n可在设置页点「发送测试消息」验证"
                % (qq, len(self._push_targets()))
            )
            return

        if sub == "config":
            rest = " ".join(parts[1:]).strip()
            low = rest.lower()
            if low in ("clear", "清除", "reset"):
                self._clear_settings()
                yield event.plain_result("已清除设置页 / 配置链接写入的覆盖配置，恢复使用 WebUI 插件配置。")
                return
            if low in ("pull", "下载", "更新", "refresh"):
                url = str(self._opt("config_url", "") or "").strip()
                if not url:
                    yield event.plain_result("尚未保存配置链接，请用 /wb config <http(s) 链接>")
                    return
                ok, text, _ = await self._pull_config(url)
                yield event.plain_result(("[完成] " if ok else "[失败] ") + text)
                return
            if rest:
                yield event.plain_result("正在从配置链接下载配置...")
                ok, text, _ = await self._pull_config(rest)
                if ok:
                    self._save_settings({"config_url": rest})
                yield event.plain_result(("[完成] " if ok else "[失败] ") + text)
                return
            yield event.plain_result(self._config_status_text())
            return

        if sub == "status":
            yield event.plain_result(self._status_text())
            return

        if sub == "accounts":
            yield event.plain_result(self._accounts_text())
            return

        if sub in ("sub", "unsub"):
            umo = event.unified_msg_origin
            subs = self._subscribers()
            if sub == "sub":
                if umo not in subs:
                    subs.append(umo)
                message = "已订阅定时签到报告。当前共 %d 个接收会话。" % len(subs)
            else:
                subs = [x for x in subs if x != umo]
                message = "已取消订阅。当前共 %d 个接收会话。" % len(subs)
            self._write_json("subscribers.json", subs)
            yield event.plain_result(message)
            return

        if sub == "log":
            try:
                count = max(5, min(120, int(arg))) if arg.isdigit() else 30
            except ValueError:
                count = 30
            yield event.plain_result(self._tail_log(count))
            return

        if sub == "stop":
            proc = self._proc
            if proc and getattr(proc, "returncode", None) is None:
                try:
                    proc.terminate()
                    yield event.plain_result("已向签到任务发送停止信号")
                except Exception as exc:
                    yield event.plain_result("停止失败: %s" % exc)
            else:
                yield event.plain_result("当前没有运行中的任务")
            return

        if sub == "refresh":
            yield event.plain_result("开始刷新全部账号 Token，请稍候...")
            ok, text, _ = await self._run_engine(["--refresh"], "Token 续期")
            yield event.plain_result(("[完成] " if ok else "[失败] ") + text)
            return

        if sub == "query":
            yield event.plain_result("开始查询积分 / 用量 / 成长数据，请稍候...")
            ok, text, _ = await self._run_engine(["--query"], "数据查询")
            yield event.plain_result(("[完成] " if ok else "[失败] ") + text)
            return

        if sub == "run":
            args = ["--no-desktop"] if self._bool("no_desktop", True) else []
            label = "完整签到"
            if arg:
                if not arg.isdigit() or int(arg) < 1:
                    yield event.plain_result("账号序号应为正整数，例如 /wb run 2")
                    return
                number = int(arg)
                total = self._account_count()
                if total and number > total:
                    yield event.plain_result("账号序号超出范围（当前共 %d 个账号）" % total)
                    return
                args += ["--only", str(number)]
                label = "签到-账号%d" % number
            yield event.plain_result(
                "已开始「%s」，完整流程可能需要几分钟到十几分钟，完成后自动回报结果。" % label
            )
            ok, text, _ = await self._run_engine(args, label)
            yield event.plain_result(("[完成] " if ok else "[失败] ") + text)
            return

        yield event.plain_result("未知指令：%s\n\n%s" % (raw_sub, HELP_TEXT))
