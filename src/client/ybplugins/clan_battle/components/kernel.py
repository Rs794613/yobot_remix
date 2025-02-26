import asyncio
import logging
import os
import re
from typing import Any, Dict
from urllib.parse import urljoin

from aiocqhttp.api import Api
from apscheduler.triggers.cron import CronTrigger

from ...ybdata import Clan_group, Clan_member, User
from ..exception import ClanBattleError
from ..util import atqq
from .define import Commands, Server

_logger = logging.getLogger(__name__)


#初始化
def init(self,
		 glo_setting:Dict[str, Any],
		 bot_api:Api,
		 boss_id_name:Dict[str, Any],
		 *args, **kwargs):
	self.setting = glo_setting
	self.boss_id_name = boss_id_name
	self.bossinfo = glo_setting['boss']
	self.api = bot_api

	# log
	if not os.path.exists(os.path.join(glo_setting['dirname'], 'log')):
		os.mkdir(os.path.join(glo_setting['dirname'], 'log'))

	formater = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
	filehandler = logging.FileHandler(
		os.path.join(glo_setting['dirname'], 'log', '公会战日志.log'),
		encoding='utf-8',
	)
	filehandler.setFormatter(formater)
	consolehandler = logging.StreamHandler()
	consolehandler.setFormatter(formater)
	_logger.addHandler(filehandler)
	_logger.addHandler(consolehandler)
	_logger.setLevel(logging.INFO)

	for group in Clan_group.select().where(Clan_group.deleted == False):
		self._boss_status[group.group_id] = asyncio.get_event_loop().create_future()

	# super-admin initialize
	User.update({User.authority_group: 100}).where(
		User.authority_group == 1
	).execute()
	User.update({User.authority_group: 1}).where(
		User.qqid.in_(self.setting['super-admin'])
	).execute()


#定时任务
def jobs(self):
	trigger = CronTrigger(hour=5)

	def ensure_future_update_all_group_members():
		asyncio.ensure_future(self._update_group_list_async())

	return ((trigger, ensure_future_update_all_group_members),)

#匹配
def match(self, cmd):
	if self.setting['clan_battle_mode'] != 'web':
		return 0
	if len(cmd) < 2:
		return 0
	return Commands.get(cmd[0:2], 0)


#执行
def execute(self, match_num, ctx):
	if ctx['message_type'] != 'group': return None
	cmd = ctx['raw_message']
	group_id = ctx['group_id']
	user_id = ctx['user_id']
	url = urljoin(
		self.setting['public_address'],
		'{}clan/{}/'.format(self.setting['public_basepath'],
		group_id))

	if match_num == 1:  # 创建
		match = re.match(r'^创建(?:([日台韩国])服)?[公工行]会$', cmd)
		if not match: return
		game_server = Server.get(match.group(1), 'cn')
		try:
			self.create_group(group_id, game_server)
		except ClanBattleError as e:
			_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
			return str(e)
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return ('公会创建成功，请登录后台查看，公会战成员请发送“加入公会”，'
				'或管理员发送“加入全部成员”')


	elif match_num == 2:  # 加入
		if cmd == '加入全部成员':
			if ctx['sender']['role'] == 'member':
				return '只有管理员才可以加入全部成员'
			_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
			asyncio.ensure_future(self._update_all_group_members_async(group_id))
			return '本群所有成员已添加记录'
		match = re.match(r'^加入[公工行]会 *(?:\[CQ:at,qq=(\d+)\])? *$', cmd)
		if match:
			if match.group(1):
				if ctx['sender']['role'] == 'member':
					return '只有管理员才可以加入其他成员'
				user_id = int(match.group(1))
				nickname = None
			else:
				nickname = (ctx['sender'].get('card') or ctx['sender'].get('nickname'))
			asyncio.ensure_future(self.bind_group(group_id, user_id, nickname))
			_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
			return '{}已加入本公会'.format(atqq(user_id))


	elif match_num == 3:  # 状态
		if cmd != '状态': return
		try: boss_summary = self.boss_status_summary(group_id)
		except ClanBattleError as e: return str(e)
		return boss_summary


	elif match_num == 4:  # 报刀
		match = re.match(r'^报刀 ?(?:[\-\=]([1-5]))? ?(\d+)?([Ww万Kk千])? *(?:\[CQ:at,qq=(\d+)\])? *(昨[日天])?$', cmd)
		if not match: return '报刀格式:\n报刀 100w（需先申请出刀）\n报刀 -1 100w（-1表示报在1王）'
		unit = {
			'W': 10000,
			'w': 10000,
			'万': 10000,
			'k': 1000,
			'K': 1000,
			'千': 1000,
		}.get(match.group(3), 1)
		boss_num = match.group(1)
		damage = int(match.group(2) or 0) * unit
		behalf = match.group(4) and int(match.group(4))
		previous_day = bool(match.group(5))
		try:
			boss_status = self.challenge(group_id, user_id, False, damage, behalf,
				boss_num = boss_num, previous_day = previous_day)
			# if behalf:
			# 	sender = self._get_nickname_by_qqid(user_id)
			# 	self.behelf_remind(behalf, f'{sender}使用您的账号打出{damage*unit}伤害')
		except ClanBattleError as e:
			_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
			return str(e)
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return boss_status


	elif match_num == 5:  # 尾刀
		match = re.match(r'^尾刀 ?([1-5])? ?(?:\[CQ:at,qq=(\d+)\])? *(昨[日天])?$', cmd)
		if not match: return
		behalf = match.group(2) and int(match.group(2))
		boss_num = match.group(1)

		previous_day = bool(match.group(3))
		try:
			boss_status = self.challenge(group_id, user_id, True, None, behalf,
				boss_num = boss_num, previous_day = previous_day)
			# if behalf:
			# 	sender = self._get_nickname_by_qqid(user_id)
			# 	self.behelf_remind(behalf, f'{sender}使用您的账号收了个尾刀')
		except ClanBattleError as e:
			_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
			return str(e)
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return boss_status

	elif match_num == 6:  # 撤销
		if cmd != '撤销': return
		try:
			boss_status = self.undo(group_id, user_id)
		except ClanBattleError as e:
			_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
			return str(e)
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return boss_status

	elif match_num == 7:  # 预约
		match = re.match(r'^预约([1-5]|表) *$', cmd)
		if not match: return
		msg = match.group(1)
		try:
			back_msg = self.subscribe(group_id, user_id, msg)
		except ClanBattleError as e:
			_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
			return str(e)
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return back_msg

	elif match_num == 8:  # 业绩
		match = re.match(r'^业绩(表) *$', cmd)
		if not match: return
		try:
			back_msg = self.score_table(group_id)
		except ClanBattleError as e:
			_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
			return str(e)
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return back_msg

	elif match_num == 11:  # 挂树
		match = re.match(r'^挂树 *(?:[\:：](.*))? *(?:\[CQ:at,qq=(\d+)\])? *$', cmd)
		if not match: return
		extra_msg = match.group(1)
		behalf = match.group(2) and int(match.group(2))
		behalf = behalf or user_id
		if isinstance(extra_msg, str):
			extra_msg = extra_msg.strip()
			if not extra_msg: extra_msg = None
		try:
			msg = self.put_on_the_tree(group_id, behalf, extra_msg)
			# if behalf:
			# 	sender = self._get_nickname_by_qqid(user_id)
			# 	self.behelf_remind(behalf, f'您的号被{sender}挂树上了。')
		except ClanBattleError as e:
			_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
			return str(e)
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return msg

	elif match_num == 12:  # 申请
		match = re.match(r'^申请出刀(| )([1-5]) *(补偿|补|b|bc)? *(?:\[CQ:at,qq=(\d+)\])? *$', cmd)
		if not match: return '申请出刀格式错误惹(っ °Д °;)っ\n如：申请出刀1 or 申请出刀1补偿@xxx'
		boss_num = match.group(2)
		is_continue = match.group(3) and True or False
		behalf = match.group(4) and int(match.group(4))
		try:
			boss_info = self.apply_for_challenge(is_continue, group_id, user_id, boss_num, behalf)
			# if behalf:
			# 	sender = self._get_nickname_by_qqid(user_id)
			# 	self.behelf_remind(behalf, f'{sender}正在帮您代刀，请注意不要登录您的账号。')
		except ClanBattleError as e:
			_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
			return str(e)
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return boss_info

	elif match_num == 13:  # 取消
		match = re.match(r'^取消 *([1-5]|挂树|申请出刀|申请|出刀|出刀all|报伤害|sl|SL|预约) *([1-5])? *(?:\[CQ:at,qq=(\d+)\])? *$', cmd)
		if not match: return
		b = match.group(1)
		boss_num = match.group(2) and match.group(2)
		behalf = match.group(3) and int(match.group(3))
		msg = ''
		if behalf:
			user_id = behalf
		if b == '挂树':
			msg = self.take_it_of_the_tree(group_id, user_id)
		elif b == '出刀' or b == '申请' or b == '申请出刀':
			msg =  self.cancel_blade(group_id, user_id)
		elif b == '出刀all':
			msg =  self.cancel_blade(group_id, user_id, cancel_type=0)
		elif b == '报伤害':
			msg =  self.report_hurt(0, 0, group_id, user_id, 1)
		elif b == 'sl' or b == 'SL':
			msg =  self.save_slot(group_id, user_id, clean_flag = True)
		elif b == '预约':
			msg = self.subscribe_cancel(group_id, boss_num, user_id)
		else: return
		_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
		return msg

	elif match_num == 15:  # 面板
		if len(cmd) != 2: return
		return f'公会战面板：\n{url}\n建议添加到浏览器收藏夹或桌面快捷方式'

	elif match_num == 16:  # SL
		match = re.match(r'^(?:SL|sl) *([\?？])? *(?:\[CQ:at,qq=(\d+)\])? *([\?？])? *$', cmd)
		if not match: return
		behalf = match.group(2) and int(match.group(2))
		only_check = bool(match.group(1) or match.group(3))
		if behalf: user_id = behalf
		# if not self.check_blade(group_id, user_id) and not only_check:
		# 	return '你都没申请出刀，S啥子L啊 (╯‵□′)╯︵┻━┻'
		if only_check:
			sl_ed = self.save_slot(group_id, user_id, only_check=True)
			if sl_ed: return '今日已使用SL'
			else: return '今日未使用SL'
		else:
			back_msg = ''
			try: back_msg = self.save_slot(group_id, user_id)
			except ClanBattleError as e:
				_logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
				return str(e)
			_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
			return back_msg

	elif match_num == 17:  # 报伤害
		match = re.match(r'^报伤害(?:剩| |)(?:(\d+(?:s|S|秒))?(?:打了| |)(\d+)(?:w|W|万))? *(?:\[CQ:at,qq=(\d+)\])? *$', cmd)
		if not match: return '格式出错(O×O)，如“报伤害 2s200w”或“报伤害 3s300w@xxx”'
		s = match.group(1) or 1
		if s != 1: s = re.sub(r'([a-z]|[A-Z]|秒)', '', s)
		hurt = match.group(2) and int(match.group(2))
		behalf = match.group(3) and int(match.group(3))
		if behalf: user_id = behalf
		if not self.check_blade(group_id, user_id):
			return '你都没申请出刀，报啥子伤害啊 (╯‵□′)╯︵┻━┻'
		return self.report_hurt(int(s), hurt, group_id, user_id)
	
	#TODO 权限申请封装func调用
	elif match_num == 18:  #权限，设置意外无权限用户有权限
		match = re.match(r'^更改权限 *(?:\[CQ:at,qq=(\d+)\])? *$', cmd)
		if match:
			if match.group(1):
				if ctx['sender']['role'] == 'member':
					return '只有管理员才可以申请权限'
				user_id = int(match.group(1))
				nickname = None
			else:
				nickname = (ctx['sender'].get('card') or ctx['sender'].get('nickname'))
			user = User.get_or_create(qqid=user_id)[0]
			membership = Clan_member.get_or_create(group_id = group_id, qqid = user_id)[0]
			user.nickname = nickname
			user.clan_group_id = group_id
			if user.authority_group >= 10:
				user.authority_group = (100 if ctx['sender']['role'] == 'member' else 10)					
				membership.role = user.authority_group
			user.save()
			membership.save()
			_logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
			return '{}已成功申请权限'.format(atqq(user_id))
	
	elif match_num == 19:  #更改预约模式
	#TODO 19:更改预约模式
		print("完成度0%")




