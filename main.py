import asyncio
import json
import os
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger

@register("group_link_monitor", "南一", "进群关联监控插件，支持规则别名与群主/群管管理，未加关联群禁言/踢出，进群自动@提醒，加入目标群自动解除禁言", "v1.5.1")
class GroupLinkMonitor(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.rules = self.config.get("rules", [])
        self.debug_raw = False  # 原始事件日志开关（群监控调试 开/关）
        self.target_members = {}  # 目标群(g2)成员缓存: {group_id: set(uid)}，规避 SnowLuma 损坏的 get_group_member_info
        # 兼容处理历史没有 name 属性或缺少 mute_duration 的规则
        changed = False
        for r in self.rules:
            if not r.get("name"):
                r["name"] = str(r.get("group1", "监控规则"))
                changed = True
            if "mute_duration" not in r:
                r["mute_duration"] = 86400
                changed = True
        if changed:
            self.save_rules()

        # 冷启动预热：尝试初始化各目标群成员名单（best-effort，适配器未就绪则静默跳过；
        # 若启动后未及时预热，可随时用「群监控重建缓存」手动刷新，解决「早于插件启动前
        # 就已在目标群的老成员，后加入监控群却被误罚」的盲区）
        asyncio.create_task(self._seed_target_members())

    def save_rules(self):
        try:
            for r in self.rules:
                if not r.get("name"):
                    r["name"] = str(r.get("group1", "监控群"))
                if "mute_duration" not in r:
                    r["mute_duration"] = 86400
            self.config["rules"] = self.rules
            if hasattr(self.config, "save_config"):
                self.config.save_config()
            elif hasattr(self.context, "save_config"):
                self.context.save_config()
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _parse_time_to_sec(self, value) -> int:
        """把 '3天' / '1.5天' / '2小时' / '30分钟' / '60秒' / '60' 解析为秒数。纯数字按秒。"""
        if value is None:
            return 0
        s = str(value).strip()
        if not s:
            return 0
        try:
            if s.endswith("天"):
                return int(float(s[:-1]) * 86400)
            if s.endswith("小时"):
                return int(float(s[:-2]) * 3600)
            if s.endswith("时"):
                return int(float(s[:-1]) * 3600)
            if s.endswith("分钟"):
                return int(float(s[:-2]) * 60)
            if s.endswith("分"):
                return int(float(s[:-1]) * 60)
            if s.endswith("秒"):
                return int(float(s[:-1]))
            # 纯数字默认按秒
            return int(float(s))
        except ValueError:
            return 0

    def _fmt_duration(self, sec: int) -> str:
        """把秒数格式化为人类可读：天/小时/分钟/秒。"""
        try:
            sec = int(sec)
        except Exception:
            return str(sec)
        if sec <= 0:
            return "0秒"
        if sec % 86400 == 0:
            return f"{sec // 86400}天"
        if sec % 3600 == 0:
            return f"{sec // 3600}小时"
        if sec % 60 == 0:
            return f"{sec // 60}分钟"
        return f"{sec}秒"

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否具有权限：Bot 管理员、群主 或 群管理员"""
        admin_list = getattr(self.context, "admins", []) or []
        user_id = str(event.get_sender_id())
        if user_id in [str(a) for a in admin_list]:
            return True

        raw = getattr(event.message_obj, "raw_message", {})
        if isinstance(raw, dict):
            sender = raw.get("sender", {})
            role = sender.get("role", "")
            if role in ["owner", "admin"]:
                return True
        return False

    # ===================== 诊断指令（新增，不影响原有逻辑） =====================
    @filter.command("群监控自检")
    async def self_check(self, event: AstrMessageEvent):
        """诊断：检查机器人权限与适配器类型（适配 Napcat / SnowLuma 等 OneBot v11 端）"""
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        raw = getattr(event.message_obj, "raw_message", {}) or {}
        try:
            platform = event.get_platform_name()
        except Exception:
            platform = "未知"
        bot_id = str(raw.get("self_id", ""))
        lines = ["🔧 【群互联 自检报告】", f"· 平台适配器: {platform}", f"· 机器人账号: {bot_id or '未知'}", f"· 当前规则数: {len(self.rules)}"]

        if not self.rules:
            lines.append("· 未配置任何规则，无法进一步检查。")
            yield event.plain_result("\n".join(lines))
            return

        for idx, r in enumerate(self.rules, 1):
            g1 = str(r.get("group1", ""))
            g2 = str(r.get("group2", ""))
            role1 = "未查询"
            try:
                info1 = await event.bot.api.call_action("get_group_member_info", group_id=int(g1), user_id=int(bot_id), no_cache=True)
                role1 = info1.get("role", "未知") if isinstance(info1, dict) else "未知"
            except Exception as e:
                role1 = f"查询失败({e})"
            lines.append(f"· 规则[{idx}] 监控群{g1}: 机器人身份={role1}")
            if role1 == "member":
                lines.append(f"   ⚠️ 机器人在监控群{g1}是普通成员 → 禁言/踢人会被QQ服务端拒绝！")
            elif role1.startswith("查询失败"):
                lines.append(f"   ℹ️ 该群查不到机器人身份（SnowLuma 已知限制：get_group_member_info 报 robot classification unavailable），请用「群监控测试 {g1} <QQ>」做最终判定。")
        lines.append("")
        lines.append("后续：")
        lines.append("· 身份为 member → 请在群里把它设为管理员，再试。")
        lines.append("· 仍失效 → 用「群监控测试 <群号> <测试QQ>」验证动作链路。")
        lines.append("· 怀疑没收到入群事件 →「群监控调试 开」后让人入群，看 AstrBot 日志原始事件。")
        yield event.plain_result("\n".join(lines))

    @filter.command("群监控测试")
    async def link_probe(self, event: AstrMessageEvent, group_id: str = "", test_qq: str = "", duration: str = "5"):
        """诊断：对指定群/QQ 做一次 禁言(duration秒)->自动解除，验证 OneBot 动作链路（SnowLuma/Napcat 通用）"""
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return
        if not group_id or not test_qq:
            yield event.plain_result("用法：群监控测试 <群号> <测试QQ> [禁言秒数，默认5]")
            return
        try:
            gid = int(group_id); uid = int(test_qq); sec = max(1, int(float(duration)))
        except ValueError:
            yield event.plain_result("❌ 群号/QQ/秒数必须是数字。")
            return

        # 1) 最佳努力：读取成员信息（SnowLuma 下可能报 robot classification unavailable，属已知限制，不阻断后续测试）
        try:
            info = await event.bot.api.call_action("get_group_member_info", group_id=gid, user_id=uid, no_cache=True)
            role = info.get("role", "?") if isinstance(info, dict) else info
            yield event.plain_result(f"① 读取成员信息成功（对方角色: {role}）")
        except Exception as e:
            yield event.plain_result(f"① 读取成员信息失败（SnowLuma 已知限制，忽略并继续测试禁言）: {e}")

        # 2) 禁言
        try:
            await event.bot.api.call_action("set_group_ban", group_id=gid, user_id=uid, duration=sec)
            yield event.plain_result(f"② 已发出对 {uid} 在群 {gid} 禁言 {sec} 秒的指令（请去 SnowLuma / QQ 里确认对方是否真被禁言；若显示成功但对方没被禁，说明 SnowLuma 未真正执行，需查 SnowLuma 侧日志）。")
        except Exception as e:
            yield event.plain_result(f"② 禁言失败：{e}\n→ 这是失效根因：bot 无管理权限，或 SnowLuma 未实现该动作。")
            return

        # 3) 等待后解除（确保不留尾巴）
        await asyncio.sleep(sec)
        try:
            await event.bot.api.call_action("set_group_ban", group_id=gid, user_id=uid, duration=0)
            yield event.plain_result(f"③ 已自动解除 {uid} 在群 {gid} 的禁言（测试结束）。")
        except Exception as e:
            yield event.plain_result(f"③ 解除禁言失败：{e}（请手动检查 {uid} 是否已解除）。")

    @filter.command("群监控调试")
    async def toggle_debug(self, event: AstrMessageEvent, onoff: str = ""):
        """诊断：开关原始事件日志（开/关），用于确认 SnowLuma 是否正确转发 group_increase 入群事件"""
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足。")
            return
        if onoff in ("开", "on", "1"):
            self.debug_raw = True
            yield event.plain_result("✅ 已开启原始事件日志，请到 AstrBot 日志观察入群事件。")
        elif onoff in ("关", "off", "0"):
            self.debug_raw = False
            yield event.plain_result("✅ 已关闭原始事件日志。")
        else:
            yield event.plain_result(f"当前原始事件日志: {'开启' if self.debug_raw else '关闭'}。用法：群监控调试 开/关")

    @filter.command("群监控重建缓存")
    async def rebuild_cache(self, event: AstrMessageEvent):
        """诊断：重建目标群成员名单缓存（解决「早于插件启动前已在目标群的老成员被误罚」问题）"""
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return
        yield event.plain_result("🔄 正在重建目标群成员名单缓存……")
        await self._seed_target_members(event.bot)
        lines = ["✅ 目标群成员名单已重建："]
        for r in self.rules:
            g2 = str(r.get("group2", ""))
            n = len(self.target_members.get(g2, set()))
            lines.append(f"· 目标群{g2}: {n} 人")
        lines.append("")
        lines.append("说明：此名单用于判断「对方是否已加目标群」。若 SnowLuma 的 get_group_member_list 也不可用，数字可能为 0，此时仍可能误罚已合规老成员（建议改用 NapCat，或让对方退群重加以触发事件）。")
        yield event.plain_result("\n".join(lines))
    # ===================== 诊断指令结束 =====================

    @filter.command("群监控帮助")
    async def help_cmd1(self, event: AstrMessageEvent):
        """获取群监控帮助文档"""
        yield event.plain_result(self._get_help_text())

    @filter.command("群互联帮助")
    async def help_cmd2(self, event: AstrMessageEvent):
        """获取群互联帮助文档"""
        yield event.plain_result(self._get_help_text())

    def _get_help_text(self) -> str:
        return (
            "📖 【群互联/群监控指令帮助 v1.5.1】\n"
            "----------------------------\n"
            "仅【群主 / 群管理员 / Bot管理员】可用：\n\n"
            "1. 添加群监控 <监控群> <目标群> [禁言/踢出] [缓冲时间] [禁言时长] [规则名称]\n"
            "   - 时间单位支持：天、小时、分钟、秒，纯数字按秒。例：3天、1.5天、2小时、30分钟、60秒、60\n"
            "   - 例: 添加群监控 111 222 (默认群号为名，禁言+60秒缓冲+1天禁言)\n"
            "   - 例: 添加群监控 111 222 踢出 10分钟 0 粉丝二群联动\n"
            "   - 例: 添加群监控 111 222 禁言 1天 30天 (缓冲1天，禁言30天)\n"
            "2. 群监控列表 : 查看所有规则名称及配置\n"
            "3. 删除群监控 <序号> : 删除对应规则\n"
            "4. 设置惩罚 <序号> <禁言/踢出> [禁言时长或缓冲时间] [缓冲时间] : 调整规则\n"
            "   - 例: 设置惩罚 1 禁言 7天 (将第1条规则禁言时长设为7天)\n"
            "   - 例: 设置惩罚 1 禁言 1天 2小时 (禁言1天，缓冲2小时)\n"
            "----------------------------\n"
            "提示：已支持加入目标群自动解除群一禁言！\n"
            "【诊断】群监控自检 / 群监控测试 <群号> <QQ> / 群监控调试 开|关 / 群监控重建缓存"
        )

    @filter.command("添加群监控")
    async def add_rule(self, event: AstrMessageEvent, group1: str, group2: str, punish: str = "禁言", delay: str = "0", mute_duration: str = "1天", name: str = ""):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        g1 = str(group1).strip()
        g2 = str(group2).strip()
        p = punish.strip()
        delay_sec = self._parse_time_to_sec(delay)
        mute_sec = self._parse_time_to_sec(mute_duration)

        if p in ["踢出", "kick"]:
            action = "kick"
            wait_time = delay_sec if delay_sec > 0 else 600
        else:
            action = "mute"
            wait_time = delay_sec if delay_sec > 0 else 60

        m_dur = mute_sec if mute_sec > 0 else 86400
        rule_name = str(name).strip() if name else f"群{g1}"

        rule = {
            "__template_key": "group_rule",
            "name": rule_name,
            "group1": g1,
            "group2": g2,
            "punish": action,
            "delay": wait_time,
            "mute_duration": m_dur
        }
        self.rules.append(rule)
        self.save_rules()
        p_desc = f"禁言({self._fmt_duration(m_dur)})" if action == "mute" else "踢出"
        yield event.plain_result(
            f"✅ 添加成功！序号: {len(self.rules)}\n"
            f"规则名称: {rule_name}\n"
            f"监控群: {rule['group1']}\n"
            f"目标群: {rule['group2']}\n"
            f"惩罚方式: {p_desc}\n"
            f"缓冲时限: {self._fmt_duration(wait_time)}"
        )

    @filter.command("群监控列表")
    async def list_rules(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        if not self.rules:
            yield event.plain_result("暂无任何群监控规则，可发送【群监控帮助】查看配置方法。")
            return
        msg = "📋 【群监控规则列表】\n"
        for idx, r in enumerate(self.rules, 1):
            r_name = r.get("name") or f"群{r.get('group1')}"
            m_dur = int(r.get("mute_duration", 86400))
            p_text = f"禁言({self._fmt_duration(m_dur)})" if r.get("punish") == "mute" else "踢出"
            d = int(r.get("delay", 60))
            msg += f"\n[{idx}] 📌 {r_name} (监控群: {r.get('group1')} -> 目标群: {r.get('group2')}) | 惩罚: {p_text} | 缓冲: {self._fmt_duration(d)}"
        yield event.plain_result(msg)

    @filter.command("删除群监控")
    async def del_rule(self, event: AstrMessageEvent, index: int):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        idx = int(index) - 1
        if 0 <= idx < len(self.rules):
            removed = self.rules.pop(idx)
            self.save_rules()
            r_name = removed.get("name") or f"群{removed.get('group1')}"
            yield event.plain_result(f"🗑️ 已删除规则 [{index}]【{r_name}】: 监控群 {removed.get('group1')} -> 目标群 {removed.get('group2')}")
        else:
            yield event.plain_result(f"❌ 序号 [{index}] 不存在。")

    @filter.command("设置惩罚")
    async def set_punish(self, event: AstrMessageEvent, index: int, punish: str, duration_or_delay: str = "0", delay: str = "0"):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 权限不足：该命令仅限群主、群管理员或 Bot 管理员使用。")
            return

        idx = int(index) - 1
        if not (0 <= idx < len(self.rules)):
            yield event.plain_result(f"❌ 序号 [{index}] 不存在。")
            return
        p = punish.strip()
        dod = self._parse_time_to_sec(duration_or_delay)
        dly = self._parse_time_to_sec(delay)
        if p in ["踢出", "kick"]:
            self.rules[idx]["punish"] = "kick"
            if dod > 0:
                self.rules[idx]["delay"] = dod
            elif self.rules[idx].get("delay", 60) == 60:
                self.rules[idx]["delay"] = 600
        elif p in ["禁言", "mute"]:
            self.rules[idx]["punish"] = "mute"
            if dod > 0:
                self.rules[idx]["mute_duration"] = dod
            if dly > 0:
                self.rules[idx]["delay"] = dly
        else:
            yield event.plain_result("❌ 惩罚只支持：禁言 或 踢出")
            return
        self.save_rules()
        m_dur = self.rules[idx].get("mute_duration", 86400)
        p_text = "踢出" if self.rules[idx]["punish"] == "kick" else f"禁言({self._fmt_duration(m_dur)})"
        yield event.plain_result(f"✅ 已将规则 [{index}]【{self.rules[idx].get('name')}】修改为: 惩罚={p_text}, 缓冲={self._fmt_duration(self.rules[idx].get('delay', 60))}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def monitor_join(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", {})
        if not isinstance(raw, dict):
            return

        # 诊断：原始事件日志（群监控调试 开 后生效）
        if self.debug_raw:
            logger.info(f"[群监控调试] 收到原始事件: {json.dumps(raw, ensure_ascii=False)}")

        notice_type = raw.get("notice_type")
        post_type = raw.get("post_type")

        # 检查是否是入群通知 (兼容各类 sub_type: approve, invite 等)
        is_increase = False
        if notice_type == "group_increase":
            is_increase = True
        elif post_type == "notice" and (raw.get("notice_type") == "group_increase" or raw.get("sub_type") in ["approve", "invite", "set"]):
            is_increase = True

        if is_increase:
            g_id = str(raw.get("group_id", ""))
            u_id = str(raw.get("user_id", ""))
            if not g_id or not u_id:
                return

            bot = event.bot

            # 场景 1：用户加入群一（监控群），检查群二
            for r in self.rules:
                if str(r.get("group1")) == g_id:
                    target_g = str(r.get("group2"))
                    punish_type = r.get("punish", "mute")
                    delay_sec = int(r.get("delay", 60 if punish_type == "mute" else 600))
                    mute_sec = int(r.get("mute_duration", 86400))

                    asyncio.create_task(self._send_join_notice(bot, g_id, target_g, u_id, punish_type, delay_sec))
                    asyncio.create_task(self._check_and_punish(bot, g_id, target_g, u_id, punish_type, delay_sec, mute_sec))

            # 场景 2：用户加入群二（目标群），记录到自维护名单，并解除群一禁言
            # 直接 await，不 create_task 延后，确保 event.bot 在有效生命周期内
            for r in self.rules:
                if str(r.get("group2")) == g_id:
                    monitored_g = str(r.get("group1"))
                    self.target_members.setdefault(g_id, set()).add(u_id)
                    await self._unban_if_joined_target(bot, monitored_g, g_id, u_id)

        # 目标群(g2) 退群：从缓存移除，避免误判为仍在目标群
        if post_type == "notice" and raw.get("notice_type") == "group_decrease":
            g_id = str(raw.get("group_id", ""))
            u_id = str(raw.get("user_id", ""))
            if g_id and u_id:
                for r in self.rules:
                    if str(r.get("group2")) == g_id:
                        self.target_members.get(g_id, set()).discard(u_id)

    async def _unban_if_joined_target(self, bot, g1: str, g2: str, uid: str):
        """当用户加入目标群 g2 时，解除在监控群 g1 的禁言"""
        try:
            await bot.api.call_action("set_group_ban", group_id=int(g1), user_id=int(uid), duration=0)
            logger.info(f"用户 {uid} 已成功加入目标群 {g2}，已自动解除群 {g1} 的禁言。")
        except Exception as e:
            # 用户原本未在群1被禁言、或 bot 无权限属正常情况，用 warning 便于排查
            logger.warning(f"尝试解除群 {g1} 用户 {uid} 禁言失败: {e}")

    async def _send_join_notice(self, bot, g_id: str, target_g: str, u_id: str, punish: str, delay: int):
        try:
            d_str = self._fmt_duration(delay)
            punish_text = "禁言" if punish == "mute" else "踢出群聊"
            msg = f" 欢迎入群！\n⚠️ 本群已开启主从群关联验证，请在【{d_str}】内加入目标群【{target_g}】，逾期未加将自动执行【{punish_text}】处罚！处罚为禁言时加入目标群则自动解除禁言！！"

            try:
                await bot.api.call_action("send_group_msg", group_id=int(g_id), message=[
                    {"type": "at", "data": {"qq": str(u_id)}},
                    {"type": "text", "data": {"text": msg}}
                ])
            except Exception:
                await bot.api.call_action("send_group_msg", group_id=int(g_id), message=f"[CQ:at,qq={u_id}]{msg}")
        except Exception as e:
            logger.error(f"发送入群提示失败: {e}")

    async def _seed_target_members(self, bot=None):
        """预热目标群(g2)成员名单，解决「冷启动」盲区：

        用户早于插件启动前就已在目标群，后加入监控群时，事件缓存里没有他，
        会被误判为「未加目标群」而错误惩罚。此函数通过 get_group_member_list
        初始化缓存。bot 为 None 时尝试从 aiocqhttp 适配器取 client；传入
        event.bot 则直接用（诊断指令场景下保证能拿到可用 client）。
        """
        try:
            if bot is None:
                platform = self.context.get_platform("aiocqhttp")
                if not platform:
                    return
                bot = platform.get_client()
            if not bot:
                return
            call_action = bot.call_action if hasattr(bot, "call_action") else bot.api.call_action
            for r in self.rules:
                g2 = str(r.get("group2", ""))
                if not g2:
                    continue
                try:
                    res = await call_action("get_group_member_list", group_id=int(g2))
                    members = res.get("data", res) if isinstance(res, dict) else res
                    uids = set()
                    if isinstance(members, list):
                        for m in members:
                            uid = m.get("user_id") if isinstance(m, dict) else None
                            if uid is not None:
                                uids.add(str(uid))
                    self.target_members[g2] = uids
                    logger.info(f"[群监控] 已预热目标群 {g2} 成员名单，共 {len(uids)} 人。")
                except Exception as e:
                    logger.warning(f"[群监控] 预热目标群 {g2} 成员列表失败（可能 SnowLuma 未实现 get_group_member_list）: {e}")
        except Exception as e:
            logger.warning(f"[群监控] 预热目标群成员缓存失败: {e}")

    async def _check_and_punish(self, bot, g1: str, g2: str, uid: str, punish: str, delay: int, mute_sec: int = 86400):
        await asyncio.sleep(delay)
        # 不再依赖 SnowLuma 损坏的 get_group_member_info（报 robot classification unavailable）。
        # 改用本插件自行维护的「目标群成员名单」（见 monitor_join 的入群/退群事件）。
        if str(uid) in self.target_members.get(str(g2), set()):
            logger.info(f"用户 {uid} 已在目标群 {g2}，跳过惩罚。")
            # 等待时间已过，用户此前已加入目标群：在监控群 @ 提醒本人未执行惩罚
            try:
                msg = "你在此之前已加入目标群，未执行任何惩罚！"
                try:
                    await bot.api.call_action("send_group_msg", group_id=int(g1), message=[
                        {"type": "at", "data": {"qq": str(uid)}},
                        {"type": "text", "data": {"text": msg}}
                    ])
                except Exception:
                    await bot.api.call_action("send_group_msg", group_id=int(g1), message=f"[CQ:at,qq={uid}]{msg}")
            except Exception as e:
                logger.error(f"发送「未执行惩罚」提醒失败: {e}")
            return

        try:
            if punish == "mute":
                await bot.api.call_action("set_group_ban", group_id=int(g1), user_id=int(uid), duration=int(mute_sec))
                logger.info(f"用户 {uid} 未加入目标群 {g2}，已在群 {g1} 禁言 {self._fmt_duration(mute_sec)}。")
            else:
                await bot.api.call_action("set_group_kick", group_id=int(g1), user_id=int(uid), reject_add_request=False)
                logger.info(f"用户 {uid} 未加入目标群 {g2}，已在群 {g1} 踢出。")
        except Exception as e:
            logger.error(f"执行惩罚失败: {e}")
