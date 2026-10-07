"""IM 协议层回归：**设备 ID 必须与取 token 时的一致，回帧错误码不许被吞**。

**为什么需要这个文件**：真机上是这样暴露的 —— IM 连接建起来了、日志写着「已挂载」，
但买家一条消息都收不到，会话列表永远空。抓帧才发现第一帧（``/reg`` 的响应）就是
``{"code": 401, "body": {"reason": "device id or appkey is not equal"}}``：取 token 用的
``deviceId`` 与 ``/reg`` 帧里的 ``did`` 是两处各自 ``uuid4()`` 出来的**随机值**，永远不相等。

于是这里锁三件事：

1. **设备 ID 确定性**：同一个账号每次都算出同一个值，且 token 请求与会话构造共用它。
2. **回帧错误码不被静默吞掉**：``code >= 400`` 一律抛 ``ImError`` 并带上平台原文 ——
   以前 ``request()`` 不看 ``code``，服务端回 400 时调用方拿到 ``{}``，
   与「平台确实没有会话」不可区分，失败被伪装成空列表。
3. **真实回帧形状能被解析**：会话列表在 ``body['userConvs']`` + ``singleChatUserConversation``
   里，消息内容还有 base64 的 ``content.custom.data``；读错键名的结果同样是「什么都没有」。

**不打真实闲鱼平台**：全部是纯函数与本地构造，不发任何网络请求。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_xianyu_im.py"
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.xianyu import im, mtop  # noqa: E402
from backend.app.xianyu.client import XianyuClient  # noqa: E402

ACCOUNT_ID = '2217970177348'
COOKIE = f'_m_h5_tk=abc_123; unb={ACCOUNT_ID}; cookie2=deadbeef; _tb_token_=xyz'


# --------------------------------------------------------------------------- 设备 ID


class DeviceIdTest(unittest.TestCase):
    def test_same_account_always_gets_same_device_id(self):
        first = mtop.generate_device_id(ACCOUNT_ID)
        second = mtop.generate_device_id(ACCOUNT_ID)
        self.assertEqual(first, second, '设备 ID 每次重新随机 = /reg 必然 401')
        self.assertIn(ACCOUNT_ID, first, '带上账号便于排查，也让不同账号天然不同')

    def test_different_accounts_get_different_device_ids(self):
        self.assertNotEqual(
            mtop.generate_device_id('acc-1'), mtop.generate_device_id('acc-2')
        )

    def test_client_shares_one_device_id_between_token_and_session(self):
        """核心断言：取 token 与会话构造必须用**同一个** device id。"""
        client = XianyuClient(COOKIE)
        self.assertEqual(client._im_device_id(), client._im_device_id())
        self.assertEqual(client._im_device_id(), mtop.generate_device_id(ACCOUNT_ID))

    def test_session_falls_back_to_the_same_value(self):
        """单独构造会话（不传 device_id）时也必须算出一致的值。"""
        session = im.ImSession(COOKIE, 'tok', ACCOUNT_ID)
        self.assertEqual(session.device_id, mtop.generate_device_id(ACCOUNT_ID))
        explicit = im.ImSession(COOKIE, 'tok', ACCOUNT_ID, device_id='did-from-token')
        self.assertEqual(explicit.device_id, 'did-from-token')


# --------------------------------------------------------------------------- 回帧错误码


class ReplyErrorTest(unittest.TestCase):
    def test_reg_401_raises_with_platform_reason(self):
        frame = {'lwp': '/reg', 'headers': {'mid': 'm1'}}
        response = {
            'code': 401,
            'body': {
                'reason': 'device id or appkey is not equal',
                'code': '4000001',
                'developerMessage': '#reg error : refers to auth',
            },
        }
        with self.assertRaises(im.ImError) as ctx:
            im.ImSession._raise_on_error(frame, response)
        message = str(ctx.exception)
        self.assertIn('device id or appkey is not equal', message)
        self.assertIn('deviceId', message, '要把「did 必须与取 token 的 deviceId 相同」说出来')

    def test_other_request_400_raises_with_lwp(self):
        frame = {'lwp': '/r/Conversation/listNewestPagination', 'headers': {'mid': 'm2'}}
        with self.assertRaises(im.ImError) as ctx:
            im.ImSession._raise_on_error(frame, {'code': 400, 'body': {}})
        self.assertIn('/r/Conversation/listNewestPagination', str(ctx.exception))

    def test_success_codes_do_not_raise(self):
        frame = {'lwp': '/reg', 'headers': {'mid': 'm3'}}
        for code in (0, 200, 399, None):
            im.ImSession._raise_on_error(frame, {'code': code, 'body': {}})


class RegisterCodeTest(unittest.TestCase):
    """``/reg`` 的成功码是 **200**，不是 0。

    真机回帧（2026-10-06）：``{"headers": {"reg-uid": "2217970177348@goofish"},
    "code": 200, "body": {"unitName": "PNM", "timestamp": ...}}``。
    曾经这里写 ``if code: raise``，把 200 当失败 —— 结果是 IM 明明注册成功却报
    「IM 注册未成功(code=200)」、入站一行帧都收不到。
    """

    def _register_with(self, response):
        session = im.ImSession(COOKIE, 'tok', ACCOUNT_ID)

        async def fake_request(frame):
            return response

        session.request = fake_request  # type: ignore[assignment]
        return asyncio.run(session._register())

    def test_200_is_success(self):
        # 真机形状：注册成功。不许抛。
        self._register_with({'code': 200, 'body': {'unitName': 'PNM'}})

    def test_zero_and_2xx_are_success(self):
        for code in (0, 200, 201, 299, None):
            with self.subTest(code=code):
                self._register_with({'code': code, 'body': {}})

    def test_401_still_raises_with_reason(self):
        with self.assertRaises(im.ImError) as ctx:
            self._register_with({
                'code': 401,
                'body': {'reason': 'device id or appkey is not equal'},
            })
        message = str(ctx.exception)
        self.assertIn('IM 注册未成功(code=401)', message)
        self.assertIn('device id or appkey is not equal', message, '平台原文不能丢')

    def test_5xx_raises_without_body(self):
        with self.assertRaises(im.ImError) as ctx:
            self._register_with({'code': 503, 'body': {}})
        self.assertIn('503', str(ctx.exception))


# --------------------------------------------------------------------------- 会话解析


def _real_conversation_payload() -> dict:
    """照真机抓帧形状造的会话条目（外面裹 ``singleChatUserConversation``）。"""
    return {
        'singleChatUserConversation': {
            'cid': f'2103ec{ACCOUNT_ID}@goofish',
            'pairFirst': f'{ACCOUNT_ID}@goofish',
            'pairSecond': '2200000001@goofish',
            'extension': {
                'itemId': 'item-9001@goofish',
                'itemTitle': '泰拉瑞亚模组定制',
                'avatar': 'https://img.example/avatar.jpg',
            },
        },
        'lastMessage': {
            'message': {
                'content': {'custom': {'summary': '在吗？'}},
                # 发送者 uid（真机都有）：最后一条是**买家**发的，`reminderTitle` 才是买家名。
                # 少了它就无法判定发送者是谁，昵称必须留空（见 task-21 的新用例）。
                'extension': {
                    'reminderTitle': '小明',
                    'senderNick': '小明',
                    'senderUserId': '2200000001@goofish',
                },
            }
        },
        'modifyTime': 1791245258792,
        'redPoint': 3,
    }


class ConversationParseTest(unittest.TestCase):
    def test_real_shape_is_parsed(self):
        one = im.parse_conversation(_real_conversation_payload(), ACCOUNT_ID)
        self.assertIsNotNone(one, '真机形状解析不出来 = 会话列表永远空')
        assert one is not None
        self.assertEqual(one['buyer_id'], '2200000001', '买家只能是「不是我的那一半」')
        self.assertEqual(one['cid'], f'2103ec{ACCOUNT_ID}', '@goofish 后缀不该入库')
        self.assertEqual(one['item_id'], 'item-9001')
        self.assertEqual(one['item_title'], '泰拉瑞亚模组定制')
        self.assertEqual(one['last_message'], '在吗？')
        self.assertEqual(one['last_time'], 1791245258792)
        self.assertEqual(one['unread'], 3)
        self.assertEqual(one['buyer_name'], '小明')

    def test_my_id_in_second_slot_still_finds_the_buyer(self):
        payload = _real_conversation_payload()
        conv = payload['singleChatUserConversation']
        conv['pairFirst'] = '2200000001@goofish'
        conv['pairSecond'] = f'{ACCOUNT_ID}@goofish'
        one = im.parse_conversation(payload, ACCOUNT_ID)
        assert one is not None
        self.assertEqual(one['buyer_id'], '2200000001')

    def test_missing_cid_or_buyer_is_not_an_empty_conversation(self):
        self.assertIsNone(im.parse_conversation({'singleChatUserConversation': {'pairFirst': 'x'}}))
        self.assertIsNone(im.parse_conversation(
            {'singleChatUserConversation': {'cid': 'c-1', 'pairFirst': '0', 'pairSecond': '0'}}
        ))
        self.assertIsNone(im.parse_conversation('not-a-dict'))


# --------------------------------------------------------------------------- 昵称归属


def _last_message_payload(
    nick: str,
    sender_uid: str = '',
    *,
    sender_dict: bool = False,
    avatar: str = '',
) -> dict:
    """照真机形状造一条会话条目：``lastMessage`` 里只有「发送者」的信息。

    ``sender_uid`` 给空 = 老版本那样没有发送者字段（无从判定是谁发的）。
    """
    extension: dict = {'reminderTitle': nick}
    if avatar:
        extension['senderAvatar'] = avatar
    if sender_uid and not sender_dict:
        extension['senderUserId'] = f'{sender_uid}@goofish'
    message: dict = {'content': {'custom': {'summary': '在吗？'}}, 'extension': extension}
    if sender_uid and sender_dict:
        message['sender'] = {'uid': sender_uid}
    return {
        'singleChatUserConversation': {
            'cid': '2103ec' + ACCOUNT_ID + '@goofish',
            'pairFirst': f'{ACCOUNT_ID}@goofish',
            'pairSecond': '2200000002@goofish',
            'extension': {'itemId': 'item-7777@goofish', 'itemTitle': '泰拉瑞亚模组定制'},
        },
        'lastMessage': {'message': message},
        'modifyTime': 1791245258792,
        'redPoint': 1,
    }


class BuyerNicknameOwnershipTest(unittest.TestCase):
    """会话列表里的「客户名称」只能是**买家**的昵称。

    真机上报：用户会话列表整列显示成他自己的昵称。原因是没有买家昵称字段 ——
    `lastMessage.message.extension.reminderTitle` 是「最后一条消息**发送者**」的昵称：
    买家发最后一条时是买家名，**我发最后一条时就是我自己的名字**。
    """

    def test_buyer_last_message_keeps_the_nickname(self):
        one = im.parse_conversation(
            _last_message_payload('一蓑烟雨任平生', '2200000002'), ACCOUNT_ID
        )
        assert one is not None
        self.assertEqual(one['buyer_id'], '2200000002')
        self.assertEqual(one['buyer_name'], '一蓑烟雨任平生')

    def test_my_own_last_message_never_becomes_the_buyer_name(self):
        # 真机原样：账号自己（月狐兔_）发了最后一条，reminderTitle 就是账号自己的昵称。
        one = im.parse_conversation(_last_message_payload('月狐兔_', ACCOUNT_ID), ACCOUNT_ID)
        assert one is not None
        self.assertEqual(one['buyer_id'], '2200000002', '买家仍然是「不是我的那一半」')
        self.assertEqual(one['buyer_name'], '', '自己的昵称绝不能写进买家名')

    def test_sender_in_nested_sender_dict_is_recognized(self):
        one = im.parse_conversation(
            _last_message_payload('一蓑烟雨任平生', '2200000002', sender_dict=True),
            ACCOUNT_ID,
        )
        assert one is not None
        self.assertEqual(one['buyer_name'], '一蓑烟雨任平生', 'sender.uid 也是真机字段')

    def test_self_sender_in_nested_sender_dict_is_rejected(self):
        one = im.parse_conversation(
            _last_message_payload('月狐兔_', ACCOUNT_ID, sender_dict=True), ACCOUNT_ID
        )
        assert one is not None
        self.assertEqual(one['buyer_name'], '')

    def test_unknown_sender_keeps_the_name_empty(self):
        one = im.parse_conversation(_last_message_payload('小明'), ACCOUNT_ID)
        assert one is not None
        self.assertEqual(one['buyer_name'], '', '判定不了发送者就留空，宁可显示「买家 <id>」')

    def test_without_my_id_only_a_sender_other_than_buyer_is_trusted(self):
        # my_id 判不了时 buyer_id 本身就可能是账号自己，所以「发送者 == buyer_id」不作数
        same = im.parse_conversation(_last_message_payload('小明', ACCOUNT_ID), '')
        assert same is not None
        self.assertEqual(same['buyer_name'], '', 'my_id 未知时不拿 buyer_id 当证据')
        other = im.parse_conversation(_last_message_payload('小明', '2200000002'), None)
        assert other is not None
        self.assertEqual(other['buyer_name'], '小明', '发送者明确不是 buyer_id 才认')

    def test_digit_placeholder_is_not_a_nickname(self):
        one = im.parse_conversation(_last_message_payload('12345678', '2200000002'), ACCOUNT_ID)
        assert one is not None
        self.assertEqual(one['buyer_name'], '')

    def test_my_own_avatar_is_not_used_as_buyer_avatar(self):
        mine = im.parse_conversation(
            _last_message_payload('月狐兔_', ACCOUNT_ID, avatar='https://img.example/me.jpg'),
            ACCOUNT_ID,
        )
        assert mine is not None
        self.assertEqual(mine['buyer_avatar'], '', '头像与昵称同一归属，别把自己的头像给买家')
        buyer = im.parse_conversation(
            _last_message_payload('小明', '2200000002', avatar='https://img.example/buyer.jpg'),
            ACCOUNT_ID,
        )
        assert buyer is not None
        self.assertEqual(buyer['buyer_avatar'], 'https://img.example/buyer.jpg')

    # -------------------------------------------------------------- 单条消息 / 入站帧

    def test_message_item_exposes_a_buyer_nickname_only_for_buyer(self):
        mine = im.parse_message_item(
            {'message': {'messageId': 'm-1', 'content': {'text': {'text': '在的'}},
                         'extension': {'senderUserId': f'{ACCOUNT_ID}@goofish',
                                       'reminderTitle': '月狐兔_'}}},
            ACCOUNT_ID,
        )
        assert mine is not None
        self.assertEqual(mine['sender_nick'], '', '自己的消息不能给出买家昵称')
        self.assertEqual(mine['sender_nick_raw'], '月狐兔_', '原样的昵称要留给「学自己的名字」')

        buyer = im.parse_message_item(
            {'message': {'messageId': 'm-2', 'content': {'text': {'text': '在吗'}},
                         'extension': {'senderUserId': '2200000002@goofish',
                                       'reminderTitle': '一蓑烟雨任平生'}}},
            ACCOUNT_ID,
        )
        assert buyer is not None
        self.assertEqual(buyer['sender_id'], '2200000002')
        self.assertEqual(buyer['sender_nick'], '一蓑烟雨任平生')

    def test_message_item_without_my_id_does_not_guess_the_nickname(self):
        one = im.parse_message_item(
            {'message': {'messageId': 'm-3', 'content': {'text': {'text': '在吗'}},
                         'extension': {'senderUserId': '2200000002@goofish',
                                       'reminderTitle': '一蓑烟雨任平生'}}},
            '',
        )
        assert one is not None
        self.assertEqual(one['sender_nick'], '', '连自己是谁都不知道时不许猜')

    def test_incoming_frame_carries_the_sender_nickname(self):
        payload = {
            'lwp': '/r/MessageManager/listUserMessages',
            'body': [{
                'msgId': 'im-1', 'cid': 'conv-1@goofish',
                'senderId': '2200000002@goofish',
                'content': {'text': {'text': '这个还在吗'}},
                'extension': {'reminderTitle': '一蓑烟雨任平生'},
            }],
        }
        one = im.parse_incoming_message(payload)
        assert one is not None
        self.assertEqual(one['sender_nick'], '一蓑烟雨任平生')


# --------------------------------------------------------------------------- 消息解析


class MessageParseTest(unittest.TestCase):
    def _message(self, content: dict, sender: str = '2200000001', **extra) -> dict:
        payload = {
            'message': {
                'messageId': 'msg-1',
                'cid': 'conv-1@goofish',
                'createAt': 1791245258000,
                'content': content,
                'extension': {'senderUserId': f'{sender}@goofish'},
            }
        }
        payload['message'].update(extra)
        return payload

    def test_base64_custom_data_is_decoded(self):
        raw = json.dumps({'contentType': 1, 'text': '这个还在吗'}, ensure_ascii=False)
        encoded = base64.b64encode(raw.encode('utf-8')).decode()
        one = im.parse_message_item(
            self._message({'custom': {'data': encoded}}), ACCOUNT_ID
        )
        assert one is not None
        self.assertEqual(one['content'], '这个还在吗')
        self.assertEqual(one['msg_type'], 'text')
        self.assertFalse(one['is_self'])
        self.assertEqual(one['msg_id'], 'msg-1')
        self.assertEqual(one['cid'], 'conv-1', '@goofish 后缀不该入库')

    def test_image_message_keeps_urls(self):
        raw = json.dumps(
            {'contentType': 2, 'image': {'pics': [{'url': 'https://img.example/p.jpg'}]}},
            ensure_ascii=False,
        )
        encoded = base64.b64encode(raw.encode('utf-8')).decode()
        one = im.parse_message_item(self._message({'custom': {'data': encoded}}), ACCOUNT_ID)
        assert one is not None
        self.assertEqual(one['images'], ['https://img.example/p.jpg'])
        self.assertEqual(one['msg_type'], 'image')

    def test_my_own_message_is_marked_self(self):
        raw = json.dumps({'contentType': 1, 'text': '在的'}, ensure_ascii=False)
        encoded = base64.b64encode(raw.encode('utf-8')).decode()
        one = im.parse_message_item(
            self._message({'custom': {'data': encoded}}, sender=ACCOUNT_ID), ACCOUNT_ID
        )
        assert one is not None
        self.assertTrue(one['is_self'], '自己发的消息要认出来，否则会重复当买家消息处理')


# --------------------------------------------------------------------------- 回帧形状


class FetchShapesTest(unittest.TestCase):
    """``fetch_conversations`` / ``fetch_messages`` 必须吃真实回帧结构。"""

    def _session(self, response: dict) -> im.ImSession:
        session = im.ImSession(COOKIE, 'tok', ACCOUNT_ID)

        async def fake_request(frame):
            return response

        session.request = fake_request  # type: ignore[assignment]
        return session

    def test_conversations_read_from_user_convs(self):
        import asyncio

        response = {'code': 0, 'body': {'userConvs': [_real_conversation_payload()]}}
        session = self._session(response)
        items = asyncio.run(session.fetch_conversations())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['buyer_id'], '2200000001')

    def test_empty_user_convs_is_empty_list(self):
        import asyncio

        session = self._session({'code': 0, 'body': {'userConvs': []}})
        self.assertEqual(asyncio.run(session.fetch_conversations()), [])

    def test_messages_read_from_body_data(self):
        import asyncio

        raw = json.dumps({'contentType': 1, 'text': '你好'}, ensure_ascii=False)
        encoded = base64.b64encode(raw.encode('utf-8')).decode()
        response = {
            'code': 0,
            'body': {
                'data': {
                    'messages': [
                        {
                            'message': {
                                'messageId': 'm-1',
                                'cid': 'conv-1@goofish',
                                'createAt': 1791245258000,
                                'content': {'custom': {'data': encoded}},
                                'extension': {'senderUserId': '2200000001@goofish'},
                            }
                        }
                    ]
                }
            },
        }
        session = self._session(response)
        items = asyncio.run(session.fetch_messages('conv-1'))
        self.assertEqual([one['content'] for one in items], ['你好'])

    def test_messages_read_from_user_message_models(self):
        """真机键名是 ``body['userMessageModels']``（不是 ``messages``）。

        真机抓帧（2026-10-06）：``{"code": 200, "body": {"nextCursor": ...,
        "hasMore": ..., "userMessageModels": [{"message": {...}}, ...]}}``。
        我们曾经只认 ``messages``/``list``/``data`` —— 会话列表能出来，
        点开却一条消息都没有，用户看到的就是「消息一个没读取过来」。
        """
        import asyncio

        raw = json.dumps({'contentType': 1, 'text': '在的'}, ensure_ascii=False)
        encoded = base64.b64encode(raw.encode('utf-8')).decode()
        response = {
            'code': 200,
            'body': {
                'nextCursor': 1791136339472,
                'hasMore': False,
                'userMessageModels': [
                    {
                        'readStatus': 2,
                        'message': {
                            'messageId': '4340883858291.PNM',
                            'cid': '67420662141@goofish',
                            'createAt': 1791275602627,
                            'content': {'custom': {'data': encoded}},
                            'extension': {'senderUserId': '2222886507942@goofish'},
                        },
                    }
                ],
            },
        }
        session = self._session(response)
        items = asyncio.run(session.fetch_messages('67420662141'))
        self.assertEqual([one['content'] for one in items], ['在的'])
        self.assertEqual(items[0]['buyer_id'], '2222886507942')


class RealThreeLayerShapeTest(unittest.TestCase):
    """真机会话条目的**三层嵌套**：字段分散在三层里，只认一层就整列解析成空。"""

    def _item(self) -> dict:
        return {
            'type': 2,
            'singleChatUserConversation': {
                'visible': 1,
                'modifyTime': 1791275531921,
                'redPoint': 0,
                'joinTime': 1791135943288,
                # 最后一条消息在**中间层**
                'lastMessage': {
                    'message': {
                        'content': {'custom': {'summary': '[请确认微信收款]'}},
                        'extension': {
                            'reminderTitle': 'x***2',
                            'senderUserId': '2222886507942',
                        },
                    }
                },
                # cid / pairFirst / pairSecond / extension 在**最内层**
                'singleChatConversation': {
                    'cid': '67420662141@goofish',
                    'pairFirst': '2222886507942@goofish',
                    'pairSecond': f'{ACCOUNT_ID}@goofish',
                    'bizType': '1',
                    'createAt': 1791135930000,
                    'extension': {
                        'itemId': '1087247101297@goofish',
                        'itemTitle': '我的世界MC开服一条龙',
                    },
                },
            },
        }

    def test_all_three_layers_are_read(self):
        one = im.parse_conversation(self._item(), ACCOUNT_ID)
        self.assertIsNotNone(one, '三层结构解析不出来 = 真机会话列表永远空')
        self.assertEqual(one['cid'], '67420662141')
        self.assertEqual(one['buyer_id'], '2222886507942', 'pairFirst 是买家')
        self.assertEqual(one['buyer_name'], 'x***2')
        self.assertEqual(one['item_id'], '1087247101297')
        self.assertEqual(one['item_title'], '我的世界MC开服一条龙')
        self.assertEqual(one['last_message'], '[请确认微信收款]')
        self.assertEqual(one['last_time'], 1791275531921, 'modifyTime 在中间层')
        self.assertEqual(one['unread'], 0)

    def test_my_id_in_first_slot_still_finds_the_buyer(self):
        item = self._item()
        inner = item['singleChatUserConversation']['singleChatConversation']
        inner['pairFirst'] = f'{ACCOUNT_ID}@goofish'
        inner['pairSecond'] = '2222886507942@goofish'
        one = im.parse_conversation(item, ACCOUNT_ID)
        self.assertEqual(one['buyer_id'], '2222886507942')

    def test_group_conversation_is_skipped(self):
        group = {'type': 1, 'groupChatUserConversation': {'cid': 'g-1@goofish'}}
        self.assertIsNone(im.parse_conversation(group, ACCOUNT_ID))


# --------------------------------------------------------------------------- 媒体消息

BUYER_ID = '2200000001'


class MediaMessageTest(unittest.TestCase):
    """task-43：入站图片/语音/卡片消息不许再被丢掉、也不许再变成「(空消息)」。

    **为什么需要**：真机库里 806 条消息全是 ``msg_type='text'``，其中 18 条 content 是空的
    （界面显示「(空消息)」）。原因是 ``parse_incoming_message`` 只取文本，图片/语音帧在
    协议层就被当成「不是消息」扔了。

    **不打真实闲鱼平台**：全部是本地构造的帧。
    """

    def _frame(self, inner: dict, *, encoded: bool = True, **extra) -> dict:
        data = json.dumps(inner, ensure_ascii=False)
        if encoded:
            data = base64.b64encode(data.encode('utf-8')).decode('ascii')
        item = {
            'msgId': 'im-9',
            'cid': 'conv-9@goofish',
            'senderId': f'{BUYER_ID}@goofish',
            'content': {'custom': {'type': 1, 'data': data}},
            'extension': {'itemId': '1087247101297'},
        }
        item.update(extra)
        return {'lwp': '/r/MessageManager/listUserMessages', 'body': [item]}

    def _item(self, inner: dict, **extra) -> dict:
        """``parse_message_item`` 吃的那层 ``{'message': {...}}``。"""
        return {'message': self._frame(inner, **extra)['body'][0]}

    # ------------------------------------------------------------------ 图片

    def test_image_frame_parses_from_base64_and_plain_json(self):
        inner = {'contentType': 2, 'image': {'pics': [{'url': 'https://img.example/a.jpg'}]}}
        for encoded in (True, False):
            with self.subTest(encoded=encoded):
                one = im.parse_incoming_message(self._frame(inner, encoded=encoded))
                assert one is not None
                self.assertEqual(one['images'], ['https://img.example/a.jpg'])
                self.assertEqual(one['msg_type'], 'image')
                self.assertEqual(one['image_url'], 'https://img.example/a.jpg')
                self.assertEqual(one['content'], '', '没有正文就不要编造正文')

    def test_pics_accepts_str_and_dict_and_dedupes(self):
        inner = {'contentType': 2, 'image': {'pics': [
            'https://img.example/plain.jpg',
            {'url': 'https://img.example/dict.jpg'},
            {'major': 'https://img.example/major.jpg'},
            {'picUrl': 'https://img.example/picurl.jpg'},
            'https://img.example/plain.jpg',   # 重复的要去掉
            {'url': ''}, {'thumb': '   '}, '', None,
        ]}}
        one = im.parse_incoming_message(self._frame(inner))
        assert one is not None
        self.assertEqual(one['images'], [
            'https://img.example/plain.jpg',
            'https://img.example/dict.jpg',
            'https://img.example/major.jpg',
            'https://img.example/picurl.jpg',
        ])

    def test_image_only_frame_still_carries_the_message(self):
        """回归：修之前这里返回 None（「不是可处理的买家文本消息」）。"""
        one = im.parse_incoming_message(self._frame(
            {'contentType': 2, 'image': {'pics': ['https://img.example/x.jpg']}}))
        self.assertIsNotNone(one, '图片帧被丢掉 = 库里漏消息 / 多一条「(空消息)」')
        self.assertEqual(one['buyer_id'], BUYER_ID, '认出来了还得认得出是谁发的')
        self.assertEqual(one['item_id'], '1087247101297')

    def test_image_without_pics_is_still_typed_as_image(self):
        """一张 URL 都抠不出来时也得有句可读文案，否则界面上就是「(空消息)」。"""
        one = im.parse_incoming_message(self._frame({'contentType': 2, 'image': {}}))
        assert one is not None
        self.assertEqual(one['images'], [])
        self.assertEqual(one['msg_type'], 'image')
        self.assertEqual(one['content'], '[图片]')

    def test_explicit_image_type_wins_over_a_stray_text_key(self):
        """`contentType=2` 的帧里带一个空的 `text` 键时，图片不能被丢掉（审查发现的坑）。"""
        inner = {'contentType': 2, 'text': {'text': ''},
                 'image': {'pics': ['https://img.example/stray.jpg']}}
        one = im.parse_incoming_message(self._frame(inner))
        assert one is not None
        self.assertEqual(one['images'], ['https://img.example/stray.jpg'])
        self.assertEqual(one['msg_type'], 'image')
        two = im.parse_message_item(self._item(inner), ACCOUNT_ID)
        assert two is not None
        self.assertEqual(two['images'], ['https://img.example/stray.jpg'])
        self.assertEqual(two['msg_type'], 'image')

    def test_card_frame_does_not_get_a_voice_url(self):
        """卡片帧顶层也有通用 `url`/`length`，不能当成语音地址（前端会渲染出播放器）。"""
        inner = {'contentType': 5, 'title': '订单已发货',
                 'url': 'https://item.taobao.com/item.htm?id=1', 'length': 12}
        for one in (im.parse_incoming_message(self._frame(inner)),
                    im.parse_message_item(self._item(inner), ACCOUNT_ID)):
            assert one is not None
            self.assertEqual(one['msg_type'], 'card')
            self.assertEqual(one['content'], '订单已发货')
            self.assertEqual((one['audio_url'], one['audio_duration']), ('', None))

    def test_whitespace_text_frame_is_still_not_a_message(self):
        """回归 HEAD：纯空白正文不算消息（base64 形状以前会漏过去）。"""
        one = im.parse_incoming_message(
            self._frame({'contentType': 1, 'text': {'text': '   '}}))
        self.assertIsNone(one)
        self.assertIsNone(im.parse_incoming_message(
            self._frame({'contentType': 1, 'text': {'text': ''}})))

    # ------------------------------------------------------------------ 语音

    def test_voice_frame_is_typed_and_not_empty(self):
        inner = {'contentType': 3, 'audio': {'url': 'https://audio.example/v.m4a',
                                             'duration': 3}}
        one = im.parse_incoming_message(self._frame(inner))
        assert one is not None
        self.assertEqual(one['msg_type'], 'voice')
        self.assertEqual(one['audio_url'], 'https://audio.example/v.m4a')
        self.assertEqual(one['audio_duration'], 3)
        self.assertEqual(one['images'], [])
        self.assertEqual(one['content'], '[语音] 3″',
                         '入站语音也得有 content：服务层拿它当「是不是消息」的闸门')

    def test_voice_summary_carries_seconds(self):
        item = self._item({'contentType': 3, 'audio': {'url': 'u', 'duration': '7'}})
        one = im.parse_message_item(item, ACCOUNT_ID)
        assert one is not None
        self.assertEqual(one['msg_type'], 'voice')
        self.assertEqual(one['audio_duration'], 7)
        self.assertEqual(one['content'], '[语音] 7″',
                         '历史同步靠 content 非空才不会被服务层当空消息丢掉')
        self.assertEqual(im.extract_message_summary(item), '[语音] 7″')

    def test_voice_without_audio_field_still_typed_voice(self):
        one = im.parse_incoming_message(self._frame({'contentType': 3}))
        assert one is not None
        self.assertEqual(one['msg_type'], 'voice')
        self.assertEqual((one['audio_url'], one['audio_duration']), ('', None))
        self.assertEqual(one['content'], '[语音]', '没有可信时长就不显示数字')

    def test_audio_duration_unit_is_not_guessed(self):
        """时长单位没有真机证据（0 条语音样本），所以只在「像秒」时显示数字。"""
        for duration, expected in ((3, '[语音] 3″'), (0, '[语音]'),
                                   (800, '[语音]'), ('abc', '[语音]'), (None, '[语音]')):
            with self.subTest(duration=duration):
                audio = {'url': 'u'}
                if duration is not None:
                    audio['duration'] = duration
                one = im.parse_message_item(self._item({'contentType': 3, 'audio': audio}))
                self.assertEqual(one['content'], expected)

    # ------------------------------------------------------------------ 卡片 / 系统

    def test_unknown_content_type_becomes_readable_text(self):
        for inner, expected in (
            ({'contentType': 14}, '[系统消息]'),            # 旧项目注释里的 tip 类型
            ({'contentType': 9, 'cardData': {}}, '[卡片消息]'),
            ({'contentType': 2, 'image': {}}, '[图片]'),
        ):
            with self.subTest(inner=inner):
                one = im.parse_incoming_message(self._frame(inner))
                assert one is not None
                self.assertTrue(one['content'] or one['images'], '空串+空图 = 「(空消息)」')
                if expected:
                    self.assertEqual(one['content'], expected)

    def test_card_with_title_keeps_using_the_title(self):
        one = im.parse_incoming_message(self._frame({'contentType': 5, 'title': '订单已发货'}))
        assert one is not None
        self.assertEqual(one['content'], '订单已发货')
        self.assertEqual(one['msg_type'], 'card')

    # ------------------------------------------------------------------ 发图帧

    def test_send_image_frame_round_trips(self):
        frame = im.build_send_image_frame('conv-9', BUYER_ID, ACCOUNT_ID,
                                          'https://img.example/send.jpg')
        self.assertEqual(frame['lwp'], '/r/MessageSend/sendByReceiverScope')
        self.assertEqual(frame['body'][1]['actualReceivers'],
                         [f'{BUYER_ID}@goofish', f'{ACCOUNT_ID}@goofish'])
        item = frame['body'][0]
        self.assertEqual(item['cid'], 'conv-9@goofish')
        self.assertEqual(item['content']['contentType'], 101)
        self.assertTrue(item['uuid'])
        decoded = im._load_content(item['content']['custom']['data'])
        self.assertEqual(decoded, {'contentType': 2,
                                   'image': {'pics': ['https://img.example/send.jpg']}})
        text, images, msg_type = im._interpret_content(decoded)
        self.assertEqual((text, images, msg_type),
                         ('', ['https://img.example/send.jpg'], 'image'),
                         '自己发出去的帧必须能被自己的解析器读回来')

    def test_send_image_frame_rejects_empty_url(self):
        with self.assertRaises(ValueError):
            im.build_send_image_frame('c', 't', 'm', '   ')

    # ------------------------------------------------------------------ 回归

    def test_text_frame_shape_is_unchanged(self):
        frame = im.build_send_frame('conv-9', BUYER_ID, ACCOUNT_ID, '在的')
        encoded = frame['body'][0]['content']['custom']['data']
        self.assertEqual(im._load_content(encoded),
                         {'contentType': 1, 'text': {'text': '在的'}})
        one = im.parse_incoming_message({'body': [{
            'msgId': 'm-1', 'cid': 'conv-9@goofish',
            'senderId': f'{BUYER_ID}@goofish',
            'content': {'custom': {'data': encoded}},
        }]})
        assert one is not None
        self.assertEqual(one['content'], '在的')
        self.assertEqual(one['msg_type'], 'text')
        self.assertEqual((one['images'], one['audio_url'], one['audio_duration']),
                         ([], '', None))

    def test_plain_text_content_shape_still_parses(self):
        one = im.parse_incoming_message({'body': [{
            'cid': 'c', 'senderId': BUYER_ID,
            'content': {'text': {'text': '这个还在吗'}},
        }]})
        assert one is not None
        self.assertEqual((one['content'], one['msg_type']), ('这个还在吗', 'text'))

    def test_non_message_frames_still_return_none(self):
        for payload in (
            None, '', 'pong', {}, {'headers': {}, 'body': []},
            {'body': {'message': {'cid': 'c', 'senderId': BUYER_ID, 'content': {}}}},
            {'body': {'message': {'cid': 'c', 'senderId': BUYER_ID,
                                 'content': {'text': {'text': '   '}}}}},
        ):
            self.assertIsNone(im.parse_incoming_message(payload), f'不该解析: {payload!r}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
