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
                'extension': {'reminderTitle': '小明', 'senderNick': '小明'},
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
