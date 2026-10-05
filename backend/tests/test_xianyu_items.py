"""协议层商品接口契约测试（`backend/app/xianyu/client.py`）。

**不打真实闲鱼平台**：`httpx.MockTransport` 按真实 mtop 响应结构回话，
逐条锁死「用哪个接口、哪个版本、spm 用哪一族、payload 顶层带什么」。
这些正是原始缺陷的形态 —— 方法不存在、参数个数不符、版本号写错、
payload 零构造 —— 所以每条断言都直接对着一份真机响应形状写。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_xianyu_items.py"
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from backend.app.xianyu import mtop  # noqa: E402
from backend.app.xianyu import risk  # noqa: E402
from backend.app.xianyu.client import (  # noqa: E402
    CATEGORY_RECOMMEND_API,
    CARDS_API,
    DOWNSHELF_API,
    EDIT_API,
    EDIT_DETAIL_API,
    ITEM_LIST_API,
    ITEM_LIST_SPM,
    ITEM_LIST_VERSION,
    PUBLISH_API,
    XianyuClient,
    XianyuError,
    _extract_list_cards,
    _local_changes_to_edit,
    build_edit_patch,
    build_edit_submit,
    build_image_info,
    build_label_ext,
    build_publish_payload,
    canonical_status,
    normalize_list_card,
    to_cent,
)

#: 带 unb 的假 Cookie（没有 unb 时 XianyuClient 直接 ValueError）
COOKIE = 'unb=2200123456; _m_h5_tk=abcdef123456_1712345678901; cookie2=xyz'


class MtopStub:
    """按 api 名回话的假 mtop 服务端。记录每一次请求的 (api, version, spm, payload)。"""

    def __init__(self, responses: Dict[str, Any], fail_ret: Optional[Dict[str, str]] = None):
        self.responses = responses
        #: api → 要回的非成功 ret（用来模拟「接口报错 / 命中违禁词」）
        self.fail_ret = fail_ret or {}
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        query = parse_qs(request.content.decode('utf-8'))
        payload = json.loads(query['data'][0]) if query.get('data') else {}
        api = params.get('api', '')
        self.calls.append({
            'api': api,
            'version': params.get('v', ''),
            'spm_cnt': params.get('spm_cnt', ''),
            'payload': payload,
            'url': str(request.url).split('?')[0],
        })
        if api in self.fail_ret:
            return httpx.Response(200, json={'ret': [self.fail_ret[api]], 'data': None})
        responder = self.responses.get(api)
        if responder is None:
            return httpx.Response(200, json={'ret': ['FAIL_SYS_API_NOT_FOUND::接口不存在'], 'data': {}})
        data = responder(payload) if callable(responder) else responder
        return httpx.Response(200, json={'ret': ['SUCCESS::调用成功'], 'data': data})

    def apis(self) -> List[str]:
        return [c['api'] for c in self.calls]


def make_client(responses: Dict[str, Any]):
    stub = MtopStub(responses)
    client = XianyuClient(COOKIE, transport=httpx.MockTransport(stub))
    return client, stub


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def list_card(item_id: str, title: str, price: str, status: str = 'onsale',
              image: str = '//img.alicdn.com/a.jpg') -> Dict[str, Any]:
    """真机列表卡片：字段套了三层（cardData → itemInfo → detailParams）。"""
    return {
        'cardData': {
            'itemInfo': {
                'itemId': item_id,
                'title': title,
                'priceInfo': {'price': price},
                'itemStatus': status,
                'picInfo': [{'url': image}],
                'detailParams': {'itemId': item_id},
            }
        }
    }


class ProtocolCase(unittest.TestCase):
    """协议层用例基类：**每个用例前清空风控熔断**。

    ``risk.registry`` 是进程级单例，按 cookie_id 分桶并带 60s 冷却。只要有一个
    用例让假服务端回了带风控标记的 ret，同一个 cookie_id 的后续用例就全被
    ``RiskControlError`` 拦下，看起来像「实现坏了」其实是测试互相污染。
    """

    def setUp(self):
        risk.registry.reset()


# --------------------------------------------------------------------------- 纯函数


class PublishPayloadTest(ProtocolCase):
    """`build_publish_payload` / label / 图片：字段类型错一个发布就报参数不合法。"""

    def test_to_cent_is_string(self):
        self.assertEqual(to_cent(2500), '250000')
        self.assertEqual(to_cent('12.5'), '1250')
        self.assertEqual(to_cent(0.01), '1')
        with self.assertRaises(XianyuError):
            to_cent('abc')
        with self.assertRaises(XianyuError):
            to_cent(-1)

    def test_image_info_extra_info_are_strings(self):
        info = build_image_info('https://img.alicdn.com/a.jpg', is_major=True)
        # 平台只认字符串 'false'，传布尔会被判参数不合法
        self.assertEqual(info['extraInfo'], {'isH': 'false', 'isT': 'false', 'raw': 'false'})
        self.assertTrue(info['major'])
        self.assertEqual(info['status'], 'done')
        self.assertEqual(info['type'], 0)

    def test_label_ext_user_specified_requires_id_and_name(self):
        label = build_label_ext(None, '201459411', '其他闲置', '50014945')
        self.assertEqual(label['channelCateId'], '201459411')
        self.assertEqual(label['channelCateName'], '其他闲置')
        self.assertIn('201459411', label['properties'])

    def test_label_ext_half_specified_id_falls_back_to_default(self):
        """只给 ID 不给名字 → 整份用默认，绝不拼出「用户 ID + 其他闲置名字」的怪标签。"""
        label = build_label_ext(None, '999999', None, None)
        self.assertEqual(label['channelCateId'], '201459411')

    def test_label_ext_uses_recommend_transport(self):
        transport = {'channelCateId': '123', 'channelCateName': '手机', 'tbCatId': '1512'}
        label = build_label_ext(transport)
        self.assertEqual(label['channelCateId'], '123')
        self.assertEqual(label['channelCateName'], '手机')
        self.assertEqual(label['text'], '手机')

    def test_publish_payload_shape(self):
        images = [build_image_info('https://img/a.jpg', is_major=True)]
        payload = build_publish_payload('iPhone 13', 2500, '成色好', images=images,
                                        quantity=2, free_shipping=False)
        self.assertEqual(payload['publishScene'], 'mainPublish')
        self.assertEqual(payload['itemTypeStr'], 'b')
        self.assertEqual(payload['quantity'], '2')          # 字符串
        self.assertEqual(payload['simpleItem'], 'true')     # 字符串
        self.assertEqual(payload['itemPriceDTO'], {'origPriceInCent': '250000',
                                                   'priceInCent': '250000'})
        self.assertEqual(payload['itemTextDTO']['title'], 'iPhone 13')
        self.assertEqual(payload['itemTextDTO']['desc'], '成色好')
        self.assertFalse(payload['itemPostFeeDTO']['canFreeShipping'])
        self.assertTrue(payload['itemPostFeeDTO']['supportFreight'])
        self.assertEqual(payload['imageInfoDOList'], images)
        self.assertIn('itemCatDTO', payload)

    def test_publish_payload_requires_image(self):
        with self.assertRaises(XianyuError):
            build_publish_payload('无图商品', 10)


class EditPayloadTest(ProtocolCase):
    """编辑必须「取回 → 打补丁 → 提交」，不能零构造（否则丢类目属性/地址）。"""

    DETAIL = {
        'itemId': '9001',
        'itemStatus': 'onsale',
        'quantity': '1',
        'itemPriceDTO': {'priceInCent': '100000', 'origPriceInCent': '120000'},
        'itemTextDTO': {'title': '旧标题', 'desc': '旧描述', 'titleDescSeparate': False},
        'itemPostFeeDTO': {'canFreeShipping': True, 'supportFreight': True, 'onlyTakeSelf': False},
        # 没显式传的字段：类目属性 / 地址 / 议价设置 —— 必须原样保留
        'itemCatDTO': {'catId': '50014945', 'channelCatId': '201459411'},
        'itemAddrDTO': {'city': '杭州', 'area': '西湖区'},
        'userRightsProtocols': [{'enable': True, 'serviceCode': 'X'}],
    }

    def test_patch_only_overrides_given_fields(self):
        payload = build_edit_patch(self.DETAIL, price=3000)
        self.assertEqual(payload['itemPriceDTO']['priceInCent'], '300000')
        # 没传的字段一个没动
        self.assertEqual(payload['itemPriceDTO']['origPriceInCent'], '120000')
        self.assertEqual(payload['itemTextDTO']['title'], '旧标题')
        self.assertEqual(payload['quantity'], '1')
        self.assertEqual(payload['itemStatus'], 'onsale')
        self.assertEqual(payload['itemCatDTO']['catId'], '50014945')
        self.assertEqual(payload['itemAddrDTO']['city'], '杭州')
        self.assertEqual(payload['userRightsProtocols'], [{'enable': True, 'serviceCode': 'X'}])

    def test_patch_does_not_mutate_detail(self):
        build_edit_patch(self.DETAIL, title=None, price=1)
        self.assertEqual(self.DETAIL['itemPriceDTO']['priceInCent'], '100000')

    def test_patch_multiple_fields(self):
        payload = build_edit_patch(self.DETAIL, quantity=5, title='新标题', desc='新描述',
                                   item_status='downshelf', free_shipping=False)
        self.assertEqual(payload['quantity'], '5')
        self.assertEqual(payload['itemTextDTO']['title'], '新标题')
        self.assertEqual(payload['itemTextDTO']['desc'], '新描述')
        self.assertEqual(payload['itemStatus'], 'downshelf')
        self.assertFalse(payload['itemPostFeeDTO']['canFreeShipping'])

    def test_submit_adds_edit_family_required_fields(self):
        payload = build_edit_submit(self.DETAIL, {'price': 3000})
        self.assertEqual(payload['bizcode'], 'pcMainPublish')
        self.assertEqual(payload['publishScene'], 'pcMainPublish')
        self.assertTrue(payload['uniqueCode'])
        self.assertEqual(payload['itemPriceDTO']['priceInCent'], '300000')

    def test_local_changes_mapping(self):
        changes = _local_changes_to_edit({'price': 1, 'original_price': 2, 'stock': 3,
                                          'title': 't', 'description': 'd',
                                          'status': 'on_sale', 'free_shipping': False})
        self.assertEqual(changes['price'], 1)
        self.assertEqual(changes['orig_price'], 2)
        self.assertEqual(changes['quantity'], 3)
        self.assertEqual(changes['desc'], 'd')
        self.assertEqual(changes['item_status'], 'onsale')
        self.assertFalse(changes['free_shipping'])

        self.assertEqual(_local_changes_to_edit({'status': 'off_shelf'})['item_status'], 'downshelf')

    def test_local_changes_rejects_unknown_field(self):
        """不认识的字段必须**报错**，静默丢掉会变成「已同步」但字段没变（假通）。"""
        with self.assertRaises(XianyuError):
            _local_changes_to_edit({'不存在的字段': 1})


class ListParseTest(ProtocolCase):
    """列表响应解析：真机字段名套了三层，版本一变就得跟着兜底。"""

    def test_canonical_status(self):
        self.assertEqual(canonical_status('onsale'), 'on_sale')
        self.assertEqual(canonical_status('downshelf'), 'off_shelf')
        self.assertEqual(canonical_status(None), 'on_sale')
        self.assertEqual(canonical_status(1), 'on_sale')
        self.assertEqual(canonical_status(0), 'off_shelf')
        # 认不出来就原样返回，绝不硬猜成「在售」
        self.assertEqual(canonical_status('sold'), 'sold')

    def test_extract_cards_prefers_item_topic_list(self):
        self.assertEqual(_extract_list_cards({'itemTopicList': [1, 2]}), [1, 2])
        self.assertEqual(_extract_list_cards({'cardList': [3]}), [3])
        self.assertEqual(_extract_list_cards({'data': {'itemTopicList': [4]}}), [4])
        self.assertEqual(_extract_list_cards({'nothing': 1}), [])

    def test_normalize_real_card(self):
        item = normalize_list_card(list_card('9001', 'iPhone 13', '2500.00'))
        self.assertEqual(item['item_id'], '9001')
        self.assertEqual(item['title'], 'iPhone 13')
        self.assertEqual(item['price'], 2500.0)
        self.assertEqual(item['status'], 'on_sale')
        self.assertEqual(item['stock'], 1)
        self.assertEqual(item['images'], ['//img.alicdn.com/a.jpg'])

    def test_normalize_down_shelf_card(self):
        item = normalize_list_card(list_card('9002', '旧手机', '100', status='downshelf'))
        self.assertEqual(item['status'], 'off_shelf')
        self.assertEqual(item['item_status'], 'downshelf')

    def test_normalize_drops_card_without_item_id(self):
        self.assertIsNone(normalize_list_card({'cardData': {'itemInfo': {'title': '没有 id'}}}))


# --------------------------------------------------------------------------- 真类调接口


class ListItemsCallTest(ProtocolCase):
    """I1 的根因：服务层调 `list_items`，真类里**根本没有这个方法**。"""

    def test_list_items_uses_personal_spm_and_v1(self):
        client, stub = make_client({
            ITEM_LIST_API: {'itemTopicList': [list_card('9001', 'iPhone 13', '2500.00')],
                            'totalCount': 1, 'nextPage': 0},
        })
        result = run(client.list_items())
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['item_id'], '9001')
        self.assertEqual(result['total'], 1)

        call = stub.calls[0]
        self.assertEqual(call['api'], ITEM_LIST_API)
        self.assertEqual(call['version'], '1.0')
        # 列表族 spm：用发布族的默认 spm 打这个接口会返回空
        self.assertEqual(call['spm_cnt'], ITEM_LIST_SPM)
        self.assertEqual(call['spm_cnt'], 'a21ybx.personal.0.0')
        self.assertEqual(call['version'], ITEM_LIST_VERSION)
        # 首屏必须带 needGroupInfo 才会回分组信息
        self.assertTrue(call['payload']['needGroupInfo'])
        self.assertEqual(call['payload']['userId'], client.cookie_id)

    def test_list_items_pages_until_next_page_zero(self):
        pages: List[Dict[str, Any]] = []

        def responder(payload):
            pages.append(payload)
            number = int(payload['pageNumber'])
            if number == 1:
                return {'itemTopicList': [list_card('9001', 'A', '1')],
                        'itemGroupList': [{'groupId': 5, 'groupName': '在售', 'defaultGroup': True}],
                        'totalCount': 2, 'nextPage': 2}
            return {'itemTopicList': [list_card('9002', 'B', '2')], 'totalCount': 2, 'nextPage': 0}

        client, stub = make_client({ITEM_LIST_API: responder})
        result = run(client.list_items())
        self.assertEqual([i['item_id'] for i in result['items']], ['9001', '9002'])
        self.assertEqual(len(stub.calls), 2)
        # 第二页要带上分组字段、并关掉 needGroupInfo
        second = stub.calls[1]['payload']
        self.assertFalse(second['needGroupInfo'])
        self.assertEqual(second['groupId'], '5')
        self.assertEqual(second['groupName'], '在售')

    def test_list_items_deduplicates_same_item(self):
        client, _stub = make_client({
            ITEM_LIST_API: {'itemTopicList': [list_card('9001', 'A', '1')],
                            'totalCount': 1, 'nextPage': 2},
        })
        result = run(client.list_items())
        self.assertEqual([i['item_id'] for i in result['items']], ['9001'])


class PublishItemCallTest(ProtocolCase):
    """I2：发布是**多步**流程，漏掉注册分类路径会报
    FAIL_BIZ_CHANNEL_CAT_ID_PATH_QUERY_ERROR。"""

    def stub(self, published_id: str = '700001'):
        return {
            'mtop.taobao.idleitem.badwords.prepubcheck': {'pass': True},
            CATEGORY_RECOMMEND_API: {
                'transportData': {'channelCateId': '201459411', 'channelCateName': '其他闲置',
                                  'tbCatId': '50014945'},
                'cardList': [{'cardId': 'c1'}],
            },
            CARDS_API: {'ok': True},
            PUBLISH_API: {'itemId': published_id},
        }

    def test_publish_runs_all_steps_in_order(self):
        client, stub = make_client(self.stub())
        result = run(client.publish_item({
            'title': 'iPhone 13', 'price': 2500, 'description': '成色好',
            'stock': 2, 'images': ['https://img.alicdn.com/a.jpg'],
        }))
        self.assertEqual(result['item_id'], '700001')
        self.assertEqual(stub.apis(), [
            'mtop.taobao.idleitem.badwords.prepubcheck',
            CATEGORY_RECOMMEND_API,
            CARDS_API,
            PUBLISH_API,
        ])
        by_api = {c['api']: c for c in stub.calls}
        self.assertEqual(by_api[CATEGORY_RECOMMEND_API]['version'], '2.0')
        self.assertEqual(by_api[CARDS_API]['version'], '1.0')
        self.assertEqual(by_api[PUBLISH_API]['version'], '1.0')

        body = by_api[PUBLISH_API]['payload']
        self.assertEqual(body['publishScene'], 'mainPublish')
        self.assertEqual(body['quantity'], '2')
        self.assertEqual(body['itemPriceDTO']['priceInCent'], '250000')
        self.assertEqual(body['itemTextDTO']['title'], 'iPhone 13')
        self.assertTrue(body['uniqueCode'])

    def test_register_cards_payload_is_double_encoded(self):
        client, stub = make_client(self.stub())
        run(client.publish_item({'title': 'x', 'price': 1,
                                 'images': ['https://img/a.jpg']}))
        body = [c for c in stub.calls if c['api'] == CARDS_API][0]['payload']
        # cpvList / itemInfoJson 是「对象再 JSON 序列化一层」塞进字符串
        self.assertIsInstance(body['cpvList'], str)
        self.assertIsInstance(body['itemInfoJson'], str)
        self.assertEqual(json.loads(body['cpvList']), [{'cardId': 'c1'}])
        param = json.loads(body['param'])
        self.assertEqual(param['multiSkuEditingMode'], 'false')

    def test_publish_requires_title_and_image(self):
        client, _stub = make_client(self.stub())
        with self.assertRaises(XianyuError):
            run(client.publish_item({'price': 1, 'images': ['https://img/a.jpg']}))
        with self.assertRaises(XianyuError):
            run(client.publish_item({'title': 'x', 'price': 1}))

    def test_bad_words_hit_blocks_publish(self):
        """预检**明确命中违禁词**才拦，且必须在发布前就拦下（不能白打发布接口）。"""
        stub = MtopStub(self.stub(), fail_ret={
            'mtop.taobao.idleitem.badwords.prepubcheck':
                'FAIL_BIZ_BADWORD::标题含有违禁词',
        })
        client = XianyuClient(COOKIE, transport=httpx.MockTransport(stub))
        with self.assertRaises(XianyuError) as ctx:
            run(client.publish_item({'title': '违禁词', 'price': 1,
                                     'images': ['https://img/a.jpg']}))
        self.assertIn('违禁词', str(ctx.exception))
        # 发布接口一次都没被打
        self.assertNotIn(PUBLISH_API, stub.apis())
        self.assertEqual(stub.apis(), ['mtop.taobao.idleitem.badwords.prepubcheck'])

    def test_bad_words_service_error_does_not_block_publish(self):
        """预检**自身报错**（不是命中）按通过处理 —— 预检服务抖动不该拦住发布。"""
        stub = MtopStub(self.stub(), fail_ret={
            'mtop.taobao.idleitem.badwords.prepubcheck':
                'FAIL_SYS_SERVICE_UNAVAILABLE::系统繁忙',
        })
        client = XianyuClient(COOKIE, transport=httpx.MockTransport(stub))
        result = run(client.publish_item({'title': '正常商品', 'price': 1,
                                          'images': ['https://img/a.jpg']}))
        self.assertEqual(result['item_id'], '700001')
        self.assertEqual(stub.apis(), [
            'mtop.taobao.idleitem.badwords.prepubcheck',
            CATEGORY_RECOMMEND_API,
            CARDS_API,
            PUBLISH_API,
        ])


class EditItemCallTest(ProtocolCase):
    """I3：编辑真签名 `edit_item(item_id, payload)`，且必须取回→打补丁→提交。"""

    DETAIL = {
        'itemId': '9001',
        'itemStatus': 'onsale',
        'quantity': '1',
        'itemPriceDTO': {'priceInCent': '100000', 'origPriceInCent': '120000'},
        'itemTextDTO': {'title': '旧标题', 'desc': '旧描述'},
        'itemCatDTO': {'catId': '50014945'},
        'itemAddrDTO': {'city': '杭州'},
        'itemPostFeeDTO': {'canFreeShipping': True},
    }

    def test_edit_fetches_detail_then_submits_patch(self):
        client, stub = make_client({
            EDIT_DETAIL_API: dict(self.DETAIL),
            EDIT_API: {'itemId': '9001', 'success': True},
        })
        result = run(client.edit_item('9001', {'price': 3000}))
        self.assertEqual(result['itemId'], '9001')
        self.assertEqual(stub.apis(), [EDIT_DETAIL_API, EDIT_API])

        edit_body = stub.calls[1]['payload']
        self.assertEqual(stub.calls[1]['version'], '1.0')
        # 目标字段改了
        self.assertEqual(edit_body['itemPriceDTO']['priceInCent'], '300000')
        # 没传的字段**原样保留**（这就是「不丢类目属性/地址」的证据）
        self.assertEqual(edit_body['itemCatDTO']['catId'], '50014945')
        self.assertEqual(edit_body['itemAddrDTO']['city'], '杭州')
        self.assertEqual(edit_body['itemTextDTO']['title'], '旧标题')
        self.assertEqual(edit_body['itemPriceDTO']['origPriceInCent'], '120000')
        # 编辑族必填
        self.assertEqual(edit_body['bizcode'], 'pcMainPublish')
        self.assertEqual(edit_body['publishScene'], 'pcMainPublish')
        self.assertEqual(edit_body['itemId'], '9001')
        self.assertEqual(edit_body['sourceId'], '9001')
        self.assertTrue(edit_body['uniqueCode'])

    def test_edit_rejects_unsupported_field(self):
        client, stub = make_client({EDIT_DETAIL_API: dict(self.DETAIL), EDIT_API: {}})
        with self.assertRaises(XianyuError):
            run(client.edit_item('9001', {'images': ['x']}))
        self.assertEqual(stub.calls, [])

    def test_edit_raises_when_detail_empty(self):
        client, _stub = make_client({EDIT_DETAIL_API: {}, EDIT_API: {}})
        with self.assertRaises(XianyuError):
            run(client.edit_item('9001', {'price': 1}))


class StatusCallTest(ProtocolCase):
    """I4：下架是独立接口 **v2.0**；上架没有独立接口，是编辑流程改 itemStatus。"""

    DETAIL = {
        'itemId': '9001',
        'itemStatus': 'downshelf',
        'quantity': '1',
        'itemPriceDTO': {'priceInCent': '100000'},
        'itemTextDTO': {'title': '旧标题', 'desc': '旧描述'},
        'itemCatDTO': {'catId': '50014945'},
    }

    def test_downshelf_uses_dedicated_v2_api(self):
        client, stub = make_client({DOWNSHELF_API: {'success': True}})
        run(client.downshelf('9001'))
        self.assertEqual(stub.apis(), [DOWNSHELF_API])
        self.assertEqual(stub.calls[0]['version'], '2.0')
        self.assertEqual(stub.calls[0]['payload'], {'itemId': '9001'})
        self.assertEqual(DOWNSHELF_API, 'mtop.taobao.idle.item.downshelf')

    def test_reshelf_goes_through_edit(self):
        client, stub = make_client({
            EDIT_DETAIL_API: dict(self.DETAIL),
            EDIT_API: {'itemId': '9001'},
        })
        run(client.reshelf('9001'))
        self.assertEqual(stub.apis(), [EDIT_DETAIL_API, EDIT_API])
        body = stub.calls[1]['payload']
        self.assertEqual(body['itemStatus'], 'onsale')
        self.assertEqual(body['itemCatDTO']['catId'], '50014945')

    def test_set_status_dispatch(self):
        client, stub = make_client({DOWNSHELF_API: {'success': True}})
        run(client.set_status('9001', 'off_shelf'))
        self.assertEqual(stub.apis(), [DOWNSHELF_API])

        client, stub = make_client({
            EDIT_DETAIL_API: dict(self.DETAIL), EDIT_API: {'itemId': '9001'},
        })
        run(client.set_status('9001', 'on_sale'))
        self.assertEqual(stub.apis(), [EDIT_DETAIL_API, EDIT_API])


class MtopStubSelfTest(ProtocolCase):
    """保证假服务端本身没骗人：api/version/spm 都能从请求里取出来。"""

    def test_stub_records_request(self):
        client, stub = make_client({ITEM_LIST_API: {'itemTopicList': []}})
        run(client.mtop(ITEM_LIST_API, {'pageNumber': 1}, v='1.0', spm_cnt='a21ybx.personal.0.0'))
        self.assertEqual(stub.calls[0]['api'], ITEM_LIST_API)
        self.assertEqual(stub.calls[0]['version'], '1.0')
        self.assertEqual(stub.calls[0]['spm_cnt'], 'a21ybx.personal.0.0')
        self.assertTrue(stub.calls[0]['url'].endswith(f'{ITEM_LIST_API}/1.0/'))

    def test_unknown_api_returns_fail(self):
        client, _stub = make_client({})
        with self.assertRaises(XianyuError):
            run(client.mtop('mtop.not.exist.api', {}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
