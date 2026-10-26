import asyncio
import json
import os
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger

@register("group_link_monitor", "南一", "进群关联监控插件，支持规则别名与群主/群管管理，未加关联群禁言/踢出，进群自动@提醒，加入目标群自动解除禁言", "v1.5.0")
class GroupLinkMonitor(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.rules = self.config.get("rules", [])
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
            "📖 【群互联/群监控指令帮助 v1.5.0】\n"
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
            "提示：已支持加入目标群自动解除群一禁言！"
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

            # 场景 2：用户加入群二（目标群），若群一中有禁言，自动解除群一禁言
            # 直接 await，不 create_task 延后，确保 event.bot 在有效生命周期内
            for r in self.rules:
                if str(r.get("group2")) == g_id:
                    monitored_g = str(r.get("group1"))
                    await self._unban_if_joined_target(bot, monitored_g, g_id, u_id)

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

    async def _check_and_punish(self, bot, g1: str, g2: str, uid: str, punish: str, delay: int, mute_sec: int = 86400):
        await asyncio.sleep(delay)
        try:
            res = await bot.api.call_action("get_group_member_info", group_id=int(g2), user_id=int(uid), no_cache=True)
            if res and res.get("user_id"):
                return
        except Exception:
            pass

        try:
            if punish == "mute":
                await bot.api.call_action("set_group_ban", group_id=int(g1), user_id=int(uid), duration=int(mute_sec))
                logger.info(f"用户 {uid} 未加入目标群 {g2}，已在群 {g1} 禁言 {self._fmt_duration(mute_sec)}。")
            else:
                await bot.api.call_action("set_group_kick", group_id=int(g1), user_id=int(uid), reject_add_request=False)
                logger.info(f"用户 {uid} 未加入目标群 {g2}，已在群 {g1} 踢出。")
        except Exception as e:
            logger.error(f"执行惩罚失败: {e}")