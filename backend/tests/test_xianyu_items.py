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
import copy
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
from backend.app.xianyu import client as client_mod  # noqa: E402
from backend.app.xianyu.client import (  # noqa: E402
    CATEGORY_RECOMMEND_API,
    CARDS_API,
    DOWNSHELF_API,
    EDIT_API,
    EDIT_DETAIL_API,
    ITEM_LIST_API,
    ITEM_LIST_SPM,
    ITEM_LIST_VERSION,
    ITEM_STATS_API,
    ITEM_STATS_SPM,
    ITEM_STATS_VERSION,
    PUBLISH_API,
    RiskControlError,
    SessionExpiredError,
    XianyuClient,
    XianyuError,
    ItemStatsUnavailable,
    _extract_list_cards,
    _local_changes_to_edit,
    _looks_like_image_url,
    build_edit_patch,
    build_edit_submit,
    build_image_info,
    build_label_ext,
    build_publish_payload,
    canonical_status,
    images_to_image_infos,
    normalize_item,
    normalize_list_card,
    parse_item_stats,
    to_cent,
)

#: 带 unb 的假 Cookie（没有 unb 时 XianyuClient 直接 ValueError）
COOKIE = 'unb=2200123456; _m_h5_tk=abcdef123456_1712345678901; cookie2=xyz'

#: 真机抓帧（2026-10-06，`C:\Users\Administrator\.dsh-tmp\item_card.json`）里的原图 URL。
#: 注意是 **http**：前端跑在 127.0.0.1，http 图片会被浏览器按混合内容拦掉。
REAL_IMAGE = ('http://img.alicdn.com/bao/uploaded/i4/O1CN01Pap3jiOC8yK2XrKe_'
              '!!2217970177348-2-fleamarket.png')
REAL_IMAGE_HTTPS = REAL_IMAGE.replace('http://', 'https://')

#: 真机详情帧里混进图片字段的闲鱼「以图搜同款」**网页**（不是图片）。
#: 2026-10-07 排查真机库 `%APPDATA%\xianyu-ai-workbench\data\app.db`：45 件里 42 件
#: （恰好是 `stock_refreshed=42` 那批走**详情**补库存的）的 images 都是
#: 「真图, 网页, 真图, 网页, …」交替 —— 说明这条网页 URL 是**每个 imageInfo 条目旁边**
#: 一起回来的（详情路径），而 20 张真机列表卡（`.dsh-tmp/item_card.json`）里
#: `goofish.com` 出现 0 次、3 件只走列表同步的商品也干干净净。
#: query 的 `extra.imageInfo` 解码后是
#: `{"bizCode":"idledetail","reqFromPage":"idle_detail","source":"itempic","url":"http://.../xxx.png_640x640q90.jpg"}`
#: —— `idledetail`/`idle_detail` 直接坐实了它来自**详情**响应。
#: 路径以 `/pages/home` 结尾，没有图片后缀，必须被丢掉。
REAL_SEARCH_PAGE = (
    'https://h5.m.goofish.com/wow/moyu/moyu-project/idle-photo-search/pages/home'
    '?kun=true&wh_ttid=native&opaque=false&extra=%7B%22imageInfo%22%3A%7B%22bizCode%22'
    '%3A%22idledetail%22%2C%22reqFromPage%22%3A%22idle_detail%22%2C%22source%22%3A'
    '%22itempic%22%2C%22url%22%3A%22http%3A%2F%2Fimg.alicdn.com%2Fbao%2Fuploaded%2Fi4'
    '%2FO1CN01Pap3jiOC8yK2XrKe_!!2217970177348-2-fleamarket.png_640x640q90.jpg%22%7D%7D'
)

#: 真机列表卡的**原样**形状：`itemStatus` 是数字、图片挂三处、
#: `detailParams.imageInfos` 是 JSON 字符串、**没有库存字段**。
REAL_CARD: Dict[str, Any] = {
    'cardType': 1003,
    'cardData': {
        'auctionType': 'b',
        'categoryId': '50023975',
        'detailUrl': '//www.goofish.com/item?id=1087258469593',
        'id': '1087258469593',
        'itemLabelDataVO': {},
        'itemStatus': 0,
        'picInfo': {'hasVideo': False, 'height': 1254, 'picUrl': REAL_IMAGE, 'width': 1254},
        'priceInfo': {'preText': '¥', 'price': '49.90'},
        'title': 'iPhone 13 128G',
        'trackParams': {},
        'detailParams': {
            'itemId': '1087258469593',
            'picUrl': REAL_IMAGE,
            'picWidth': 1254,
            'picHeight': 1254,
            'postInfo': '包邮',
            'soldPrice': '49.90',
            'title': 'iPhone 13 128G',
            'imageInfos': json.dumps([{
                'heightSize': 1254, 'major': True, 'type': 0, 'url': REAL_IMAGE,
                'videoCover': False, 'widthSize': 1254,
            }]),
        },
    },
}


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
        # 真机（2026-10-06，账号 2217970177348）：45 件里 itemStatus 44×0、1×1，
        # 那个 1 的详情回 itemStatusStr="卖掉了" ⇒ 0=在售、1=已卖出。
        self.assertEqual(canonical_status(0), 'on_sale')
        self.assertEqual(canonical_status(1), 'sold')
        # 其它数字语义未证实 → 原样返回，绝不硬猜成「在售」
        self.assertEqual(canonical_status(2), '2')
        self.assertEqual(canonical_status(3), '3')
        # 认不出来就原样返回，绝不硬猜成「在售」
        self.assertEqual(canonical_status('sold'), 'sold')
        self.assertEqual(canonical_status('已下架'), '已下架')

    def test_canonical_status_bool_is_not_treated_as_number(self):
        """`bool` 是 `int` 的子类，真机没出现过布尔状态。

        不拦的话 `int(True)==1` 会被套上「已卖出」的语义 —— 一个布尔状态被说成
        「卖掉了」比说「不认识」更糟。原样 `str()` 返回，不猜。
        """
        self.assertEqual(canonical_status(True), 'True')
        self.assertEqual(canonical_status(False), 'False')

    def test_canonical_status_digit_strings_are_names_not_numbers(self):
        """字符串 `'0'`/`'1'` 走 `_STATUS_MAP` 查不到 → 原样返回。

        真机这两个字段都是**数字**类型，所以字符串分支不该被顺手改写成数字语义；
        真出现了 `'1'` 说明是另一个字段（比如以字符串形态回的状态名），
        硬套数字映射就是猜。
        """
        self.assertEqual(canonical_status('0'), '0')
        self.assertEqual(canonical_status('1'), '1')

    def test_normalize_card_numeric_status_not_swallowed(self):
        """`itemStatus=0` 不能被 `or` 吞成 `None`（真机 44/45 都是 0）。"""
        zero = normalize_list_card(list_card('9001', '在售的手机', '100', status=0))
        self.assertEqual(zero['status'], 'on_sale')
        self.assertEqual(zero['item_status'], 0)

        sold = normalize_list_card(list_card('9002', '卖掉的手机', '100', status=1))
        self.assertEqual(sold['status'], 'sold')
        self.assertEqual(sold['item_status'], 1)

    def test_normalize_item_numeric_status_matches_card(self):
        """详情路径与列表路径对同一件商品必须给出**同一个** status 口径。"""
        self.assertEqual(
            normalize_item({'itemDO': {'itemId': '9001', 'itemStatus': 0}})['status'],
            'on_sale',
        )
        self.assertEqual(
            normalize_item({'itemDO': {'itemId': '9002', 'itemStatus': 1}})['status'],
            'sold',
        )
        # 详情没报 itemStatus 时才退回 status 键
        self.assertEqual(
            normalize_item({'itemDO': {'itemId': '9003', 'status': 'downshelf'}})['status'],
            'off_shelf',
        )

    def test_real_frame_sold_card(self):
        """真机抓帧形状：`itemStatus=1` 的那件（1087247101297）是卖掉了。

        真机卡把 `itemStatus` 放在 **`cardData` 顶层**（不是 `itemInfo` 里），
        `normalize_list_card` 的 `inner` 兜底到 `node` 才能取到 —— 顺便钉住这条。
        """
        sold = json.loads(json.dumps(REAL_CARD))
        sold['cardData']['id'] = '1087247101297'
        sold['cardData']['detailParams']['itemId'] = '1087247101297'
        sold['cardData']['itemStatus'] = 1
        sold_item = normalize_list_card(sold)
        self.assertEqual(sold_item['item_id'], '1087247101297')
        self.assertEqual(sold_item['status'], 'sold')
        self.assertEqual(sold_item['item_status'], 1)

        on_sale = json.loads(json.dumps(REAL_CARD))
        on_sale['cardData']['itemStatus'] = 0
        item = normalize_list_card(on_sale)
        self.assertEqual(item['status'], 'on_sale')
        self.assertEqual(item['item_status'], 0)

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
        # 列表卡**没有库存字段**：真机 cardData 的键里没有 quantity/stock，
        # 拿不到就得给 None（服务层落库写 -1），绝不能写 1 冒充有货。
        self.assertIsNone(item['stock'])
        self.assertEqual(item['images'], ['//img.alicdn.com/a.jpg'])

    def test_normalize_real_frame_card_images(self):
        """真机抓帧形状（`C:\\Users\\Administrator\\.dsh-tmp\\item_card.json`）。

        三个来源都指着同一张图：`picInfo.picUrl`（dict）、
        `detailParams.picUrl`（dict 里的字符串）、`detailParams.imageInfos`
        （**JSON 字符串**）。旧实现只认 list → 真机一张图都拿不到。
        """
        item = normalize_list_card(REAL_CARD)
        self.assertEqual(item['item_id'], '1087258469593')
        self.assertEqual(item['title'], 'iPhone 13 128G')
        self.assertEqual(item['price'], 49.9)
        # 三个来源同一张图 -> 去重成 1 张；http -> https（前端在 127.0.0.1，
        # 混合内容会被浏览器拦掉，img.alicdn.com 支持 https）。
        self.assertEqual(item['images'], [REAL_IMAGE_HTTPS])
        self.assertIsNone(item['stock'])

    def test_normalize_real_frame_major_image_first(self):
        """imageInfos 里 `major: true` 的那张是主图，必须排第一位。"""
        card = json.loads(json.dumps(REAL_CARD))
        card['cardData']['detailParams']['imageInfos'] = json.dumps([
            {'major': False, 'url': 'http://img.alicdn.com/second.png'},
            {'major': True, 'url': 'http://img.alicdn.com/main.png'},
        ])
        card['cardData']['detailParams'].pop('picUrl')
        card['cardData']['picInfo'] = {'picUrl': 'http://img.alicdn.com/second.png'}
        item = normalize_list_card(card)
        self.assertEqual(item['images'],
                         ['https://img.alicdn.com/main.png', 'https://img.alicdn.com/second.png'])

    def test_normalize_card_image_infos_as_list_of_dicts(self):
        """同一字段也可能是真 list（其它版本），一样要认。"""
        card = json.loads(json.dumps(REAL_CARD))
        card['cardData']['picInfo'] = {}
        card['cardData']['detailParams'].pop('picUrl')
        card['cardData']['detailParams']['imageInfos'] = [
            {'url': 'http://img.alicdn.com/a.png'},
            {'url': 'http://img.alicdn.com/b.png'},
        ]
        item = normalize_list_card(card)
        self.assertEqual(item['images'],
                         ['https://img.alicdn.com/a.png', 'https://img.alicdn.com/b.png'])

    def test_image_urls_keep_only_image_extensions(self):
        """抽图只认图片后缀（忽略 query/fragment），认不出的 URL 一律丢。

        `.heic` 必须留着：真机库 62 张真图里有 3 张是 `..._xy_item.heic`。
        """
        cases = {
            'https://img.alicdn.com/a.png': True,
            'https://img.alicdn.com/a_640x640q90.jpg': True,
            'https://img.alicdn.com/a.JPG': True,
            'https://img.alicdn.com/a.webp?t=1': True,
            'https://img.alicdn.com/a.gif#frag': True,
            'https://img.alicdn.com/a_xy_item.heic': True,
            REAL_SEARCH_PAGE: False,
            'https://img.alicdn.com/uploads/a': False,          # 无后缀
            'https://h5.m.goofish.com/wow/moyu/x.html': False,  # 网页
            '': False,
        }
        for url, expected in cases.items():
            self.assertIs(_looks_like_image_url(url), expected, url)

    def test_detail_images_drop_search_page_next_to_each_image(self):
        """真机污染形态（详情路径）：每个 imageInfo 旁边挂一条搜同款网页。

        库里那条是「真图, 网页, 真图, 网页…」，编辑弹窗会多出打不开的卡片，
        用户一保存还会把网页 URL 当商品图推给平台。
        """
        detail = {'itemDO': {'itemId': '1', 'imageInfos': [
            {'major': True, 'url': REAL_IMAGE, 'searchUrl': REAL_SEARCH_PAGE},
            {'major': False, 'url': 'http://img.alicdn.com/x_xy_item.heic',
             'searchUrl': REAL_SEARCH_PAGE},
        ]}}
        self.assertEqual(normalize_item(detail)['images'],
                         [REAL_IMAGE_HTTPS, 'https://img.alicdn.com/x_xy_item.heic'])

    def test_images_empty_when_only_search_page(self):
        """三个来源全是网页 URL -> 返回空列表，**不拿网页凑数**。"""
        card = json.loads(json.dumps(REAL_CARD))
        card['cardData']['picInfo'] = {'picUrl': REAL_SEARCH_PAGE}
        card['cardData']['detailParams']['picUrl'] = REAL_SEARCH_PAGE
        card['cardData']['detailParams']['imageInfos'] = json.dumps(
            [{'major': True, 'url': REAL_SEARCH_PAGE}])
        self.assertEqual(normalize_list_card(card)['images'], [])
        self.assertEqual(
            normalize_item({'itemDO': {'itemId': '1', 'picDetailDO': REAL_SEARCH_PAGE}})['images'], [])

    def test_extract_list_cards_uses_card_list_when_topic_list_empty(self):
        """真机 `itemTopicList` 是**空 list**，真商品在 `cardList` 里。"""
        self.assertEqual(_extract_list_cards({'itemTopicList': [], 'cardList': [1, 2]}), [1, 2])
        self.assertEqual(_extract_list_cards({'itemTopicList': [], 'cardList': []}), [])

    def test_normalize_item_stock_is_real_quantity(self):
        """真库存只在这里：详情 `data.itemDO.quantity`（真机 1087258469593 -> 66）。"""
        item = normalize_item({'itemDO': {'itemId': '9001', 'quantity': 66}}, '9001')
        self.assertEqual(item['stock'], 66)
        # `editDetail` 回填给的是**字符串** "66"，一样要认
        self.assertEqual(normalize_item({'itemDO': {'quantity': '1'}})['stock'], 1)
        # 没有 quantity 就是未知，不能编数
        self.assertIsNone(normalize_item({'itemDO': {'title': 'x'}})['stock'])
        self.assertIsNone(normalize_item({'itemDO': {'quantity': 'abc'}})['stock'])
        self.assertIsNone(normalize_item({'itemDO': {'quantity': -3}})['stock'])

    def test_normalize_item_images_from_pic_detail(self):
        """详情图片结构没有真机帧（`picDetailDO` 形状未确认），三种形态都要认。"""
        raw_json = json.dumps([{'major': True, 'url': REAL_IMAGE}])
        item = normalize_item({'itemDO': {'itemId': '1', 'picDetailDO': raw_json}})
        self.assertEqual(item['images'], [REAL_IMAGE_HTTPS])
        item = normalize_item({'itemDO': {'itemId': '1',
                                          'picDetailDO': {'picUrl': 'http://img.alicdn.com/p.png'}}})
        self.assertEqual(item['images'], ['https://img.alicdn.com/p.png'])
        item = normalize_item({'itemDO': {'itemId': '1',
                                          'imageList': [{'url': 'http://img.alicdn.com/q.png'}]}})
        self.assertEqual(item['images'], ['https://img.alicdn.com/q.png'])
        self.assertEqual(normalize_item({'itemDO': {'itemId': '1'}})['images'], [])

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

    def test_list_items_pages_on_boolean_next_page(self):
        """真机形状：`nextPage` 是**布尔** `true`、`totalCount` 恒为 0。

        缺陷（2026-10-06 用户报「怎么只显示 20 个商品」）：旧实现
        `int(True) == 1` 且当前页正是 1，`next_page in (0, -1, number)` 立刻 break
        —— 一个账号有多少件商品都只入库第一页的 20 件。
        """
        calls: List[Dict[str, Any]] = []

        def responder(payload):
            calls.append(payload)
            number = int(payload['pageNumber'])
            if number == 1:
                return {'itemTopicList': [list_card('9001', 'A', '1')],
                        'itemGroupList': [{'groupId': 5, 'groupName': '在售', 'defaultGroup': True}],
                        'totalCount': 0, 'nextPage': True}
            if number == 2:
                return {'itemTopicList': [list_card('9002', 'B', '2')],
                        'totalCount': 0, 'nextPage': True}
            return {'itemTopicList': [list_card('9003', 'C', '3')],
                    'totalCount': 0, 'nextPage': False}

        client, _stub = make_client({ITEM_LIST_API: responder})
        result = run(client.list_items())
        self.assertEqual([i['item_id'] for i in result['items']], ['9001', '9002', '9003'])
        # totalCount 是 0，对外 total 用实际拿到的条数兜底
        self.assertEqual(result['total'], 3)
        self.assertEqual([int(p['pageNumber']) for p in calls], [1, 2, 3])
        self.assertEqual(result['next_page'], 0)

    def test_has_next_page_handles_both_shapes(self):
        """`nextPage` 两种形态都要认：布尔开关 / 数字页码（纯函数，离线可测）。"""
        from backend.app.xianyu.client import _has_next_page
        self.assertTrue(_has_next_page(True, 1))
        self.assertFalse(_has_next_page(False, 1))
        self.assertFalse(_has_next_page(0, 1))
        self.assertFalse(_has_next_page(-1, 1))
        self.assertFalse(_has_next_page(None, 1))
        self.assertFalse(_has_next_page('', 1))
        self.assertFalse(_has_next_page(1, 1))   # 页码没往前走 = 没有下一页
        self.assertFalse(_has_next_page(2, 3))
        self.assertTrue(_has_next_page(2, 1))
        self.assertTrue(_has_next_page('3', 2))
        self.assertFalse(_has_next_page('true', 1))  # 数字以外的字符串不当真


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


class EditStatusMappingTest(ProtocolCase):
    """编辑 `status` 的安全后果（task-27 第 4 条自查，**不改实现**，只钉住行为）。

    `_local_changes_to_edit` 是 `=='on_sale' ? ON_SALE : OFF_SHELF` 的二值判断，
    所以**任何非「在售」的入参都落 `downshelf`（下架）** —— 这是保守方向：
    失败模式是「多下架一件」，不是「把已卖出/已下架改成在售」。
    """

    ON_SALE = 'onsale'
    OFF_SHELF = 'downshelf'

    def test_numeric_zero_is_on_sale(self):
        """`itemStatus=0` 是真机 44/45 件的常态（在售）。"""
        self.assertEqual(_local_changes_to_edit({'status': 0})['item_status'], self.ON_SALE)

    def test_sold_never_becomes_on_sale(self):
        """**最关键的一条**：数字 1（真机 itemStatusStr="卖掉了"）不能改成在售。"""
        self.assertEqual(_local_changes_to_edit({'status': 1})['item_status'], self.OFF_SHELF)

    def test_sold_name_never_becomes_on_sale(self):
        self.assertEqual(_local_changes_to_edit({'status': 'sold'})['item_status'], self.OFF_SHELF)
        self.assertEqual(_local_changes_to_edit({'status': '卖掉了'})['item_status'], self.OFF_SHELF)

    def test_unknown_number_never_becomes_on_sale(self):
        """语义未证实的数字同样落 downsheld（宁可下架，不可误上架）。"""
        self.assertEqual(_local_changes_to_edit({'status': 2})['item_status'], self.OFF_SHELF)

    def test_explicit_names_still_map(self):
        self.assertEqual(_local_changes_to_edit({'status': 'on_sale'})['item_status'], self.ON_SALE)
        self.assertEqual(
            _local_changes_to_edit({'status': 'off_shelf'})['item_status'], self.OFF_SHELF
        )


class EditImagesTest(ProtocolCase):
    """I5：编辑商品改图片（``images`` → ``imageInfoDOList``）。

    真机原样字段来自只读抓帧（``edit_item_detail('1087258469593')``）：条目是
    **list of dict、全部字符串值**、第一张 ``major="true"``。这里逐字段钉死，
    因为形状错一个平台就回一个看不懂的参数错误，排查成本极高。
    """

    DETAIL = {
        'itemId': '9001',
        'itemStatus': 'onsale',
        'quantity': '66',
        'itemPriceDTO': {'priceInCent': '100000', 'origPriceInCent': '120000'},
        'itemTextDTO': {'title': '旧标题', 'desc': '旧描述'},
        'itemCatDTO': {'catId': '50014945'},
        'itemAddrDTO': {'city': '杭州'},
        'itemPostFeeDTO': {'canFreeShipping': True},
        'imageInfoDOList': [{'major': 'true', 'url': 'http://old.example.com/old.png'}],
    }

    def test_local_changes_accepts_images(self):
        """① 不再抛「协议层不支持修改字段」。"""
        changes = _local_changes_to_edit({'images': [REAL_IMAGE, REAL_IMAGE_HTTPS]})
        self.assertIn('image_infos', changes)
        self.assertEqual(len(changes['image_infos']), 2)

    def test_image_info_matches_real_frame(self):
        """② 字段名/类型与真机一致，第一张 major='true'。"""
        infos = images_to_image_infos([REAL_IMAGE])
        self.assertEqual(len(infos), 1)
        info = infos[0]
        self.assertEqual(set(info), {'major', 'widthSize', 'heightSize', 'type',
                                     'url', 'extraInfo'})
        # **全部字符串**：真机就是这么给的，传数字/布尔会被当参数非法。
        for key in ('major', 'widthSize', 'heightSize', 'type', 'url'):
            self.assertIsInstance(info[key], str, f'{key} 必须是字符串')
        self.assertEqual(info['major'], 'true')
        self.assertEqual(info['type'], '0')
        self.assertEqual(info['url'], REAL_IMAGE)
        self.assertEqual(info['extraInfo'], {'raw': 'false', 'isT': 'false', 'isH': 'false'})
        for value in info['extraInfo'].values():
            self.assertIsInstance(value, str)

    def test_only_first_image_is_major(self):
        infos = images_to_image_infos([REAL_IMAGE, REAL_IMAGE_HTTPS, REAL_IMAGE])
        self.assertEqual([i['major'] for i in infos], ['true', 'false', 'false'])

    def test_accepts_http_and_https(self):
        infos = images_to_image_infos([REAL_IMAGE_HTTPS, REAL_IMAGE])
        self.assertEqual([i['url'] for i in infos], [REAL_IMAGE_HTTPS, REAL_IMAGE])

    def test_sizes_from_dict_and_sizes_arg(self):
        infos = images_to_image_infos(
            [{'url': REAL_IMAGE, 'width': 1254, 'height': 1254}, REAL_IMAGE_HTTPS],
            sizes=[None, {'width': '800', 'height': 600}],
        )
        self.assertEqual(infos[0]['widthSize'], '1254')
        self.assertEqual(infos[0]['heightSize'], '1254')
        self.assertEqual(infos[1]['widthSize'], '800')
        self.assertEqual(infos[1]['heightSize'], '600')

    def test_missing_size_is_placeholder_not_invented(self):
        """尺寸未知时用占位 '0'，**不许硬编码真机那张图的 1254**。"""
        infos = images_to_image_infos([REAL_IMAGE])
        self.assertEqual(infos[0]['widthSize'], '0')
        self.assertEqual(infos[0]['heightSize'], '0')

    def test_empty_images_raises(self):
        """③ 空数组报可读错误（不许把商品图清成空）。"""
        with self.assertRaises(XianyuError) as ctx:
            images_to_image_infos([])
        self.assertIn('至少一张', str(ctx.exception))

        with self.assertRaises(XianyuError):
            _local_changes_to_edit({'images': []})
        with self.assertRaises(XianyuError):
            _local_changes_to_edit({'images': None})

    def test_non_url_rejected(self):
        """本地路径不能静默塞给平台：必须先上传换成图床 URL。"""
        with self.assertRaises(XianyuError) as ctx:
            images_to_image_infos(['C:\\tmp\\local.png'])
        self.assertIn('http', str(ctx.exception))

    def test_blank_entry_rejected(self):
        with self.assertRaises(XianyuError):
            images_to_image_infos([''])
        with self.assertRaises(XianyuError):
            images_to_image_infos([{'url': ''}])

    def test_patch_replaces_image_list_only(self):
        """打补丁整体替换图片，**其它字段一个都不动**。"""
        patched = build_edit_patch(self.DETAIL, image_infos=images_to_image_infos([REAL_IMAGE]))
        self.assertEqual(len(patched['imageInfoDOList']), 1)
        self.assertEqual(patched['imageInfoDOList'][0]['url'], REAL_IMAGE)
        self.assertEqual(patched['itemCatDTO']['catId'], '50014945')
        self.assertEqual(patched['itemAddrDTO']['city'], '杭州')
        self.assertEqual(patched['itemTextDTO']['title'], '旧标题')

    def test_patch_does_not_mutate_detail(self):
        original = copy.deepcopy(self.DETAIL)
        build_edit_patch(self.DETAIL, image_infos=images_to_image_infos([REAL_IMAGE]))
        self.assertEqual(self.DETAIL, original)

    def test_existing_branches_unchanged(self):
        """④ price/stock/title 分支行为不变（回归钉子）。"""
        changes = _local_changes_to_edit({'price': 1, 'stock': 3, 'title': 't'})
        self.assertEqual(changes['price'], 1)
        self.assertEqual(changes['quantity'], 3)
        self.assertEqual(changes['title'], 't')
        self.assertNotIn('image_infos', changes)

        with self.assertRaises(XianyuError) as ctx:
            _local_changes_to_edit({'不存在的字段': 1})
        self.assertIn('不支持修改字段', str(ctx.exception))

    def test_edit_item_submits_image_info_list(self):
        """端到端：``edit_item(id, {'images': [...]})`` 真的把图打进提交体。"""
        client, stub = make_client({
            EDIT_DETAIL_API: copy.deepcopy(self.DETAIL),
            EDIT_API: {'itemId': '9001', 'success': True},
        })
        result = run(client.edit_item('9001', {'images': [REAL_IMAGE_HTTPS, REAL_IMAGE]}))

        self.assertEqual(result['itemId'], '9001')
        self.assertEqual(stub.apis(), [EDIT_DETAIL_API, EDIT_API])
        body = stub.calls[1]['payload']
        self.assertEqual(len(body['imageInfoDOList']), 2)
        self.assertEqual([i['major'] for i in body['imageInfoDOList']], ['true', 'false'])
        self.assertEqual(body['imageInfoDOList'][0]['url'], REAL_IMAGE_HTTPS)
        # 没传的字段仍在（不丢类目/地址）
        self.assertEqual(body['itemCatDTO']['catId'], '50014945')
        self.assertEqual(body['itemAddrDTO']['city'], '杭州')
        self.assertEqual(body['quantity'], '66')
        # 编辑族必填还在
        self.assertEqual(body['bizcode'], 'pcMainPublish')
        self.assertEqual(body['itemId'], '9001')
        self.assertEqual(body['sourceId'], '9001')

    def test_edit_item_images_plus_price_together(self):
        client, stub = make_client({
            EDIT_DETAIL_API: copy.deepcopy(self.DETAIL),
            EDIT_API: {'itemId': '9001'},
        })
        run(client.edit_item('9001', {'price': 3000, 'images': [REAL_IMAGE]}))

        body = stub.calls[1]['payload']
        self.assertEqual(body['itemPriceDTO']['priceInCent'], '300000')
        self.assertEqual(body['imageInfoDOList'][0]['url'], REAL_IMAGE)

    def test_edit_item_bad_images_never_calls_platform(self):
        """参数不合法时**一次接口都不该打**（校验发生在取详情之前）。"""
        client, stub = make_client({EDIT_DETAIL_API: copy.deepcopy(self.DETAIL), EDIT_API: {}})
        with self.assertRaises(XianyuError):
            run(client.edit_item('9001', {'images': []}))
        with self.assertRaises(XianyuError):
            run(client.edit_item('9001', {'images': ['not-a-url']}))
        self.assertEqual(stub.calls, [])


class ItemStatsTest(ProtocolCase):
    """「商品数据」接口：`mtop.alibaba.idle.seller.pc.item.stats.query`。

    接口名 / 版本 / 参数 / 字段名都来自**卖家工作台**（seller.goofish.com）
    0.1.68 的打包产物：

        wP = function (eu) {
            return G({api: 'mtop.alibaba.idle.seller.pc.item.stats.query',
                      v: '1.0', data: {itemId: eu}});
        }
        // 组件渲染：'曝光：', eT?.exposureNum, '｜浏览：', eT?.showNum, '｜想要：', eT?.wantNum

    ⚠️ 夹具是**按产物推出来的响应形状**，不是抓到的成功真机包：同一个 Cookie 打
    `mtop.idle.web.user.page.head` 回 SUCCESS，打这个接口回
    `FAIL_SYS_SESSION_EXPIRED::Session过期`（真机 9 次只读调用，2026-10-07）。
    也就是说「接口存在、字段名确定」，但「能不能用这个 Cookie 调通」**未被验证**。
    """

    ITEM_ID = '1087247101297'

    def test_request_shape_and_parse(self):
        """请求打对了接口/版本/spm/data，响应按别名解析出三个指标。"""
        client, stub = make_client({
            ITEM_STATS_API: {'exposureNum': 123, 'showNum': 45, 'wantNum': 6},
        })
        result = run(client.item_stats(self.ITEM_ID))

        self.assertEqual(stub.apis(), [ITEM_STATS_API])
        self.assertEqual(stub.calls[0]['version'], ITEM_STATS_VERSION)
        self.assertEqual(stub.calls[0]['spm_cnt'], ITEM_STATS_SPM)
        self.assertEqual(stub.calls[0]['payload'], {'itemId': self.ITEM_ID})
        # 键名必须改写：协议层给服务层的是 `exposure`/`visits`/`wants` 这套别名
        # （服务层的别名表也认 exposureNum/showNum，但那只是第二道保险）
        self.assertEqual(result['exposure'], 123)
        self.assertEqual(result['visits'], 45)
        self.assertEqual(result['wants'], 6)
        self.assertEqual(result['raw'], {'exposureNum': 123, 'showNum': 45, 'wantNum': 6})

    def test_real_zero_is_kept_but_missing_is_not_zero(self):
        """平台真回 0 要如实显示 0；**没给**的字段不许补 0（拿不到就是拿不到）。"""
        client, _ = make_client({ITEM_STATS_API: {'exposureNum': 0, 'showNum': None}})
        result = run(client.item_stats(self.ITEM_ID))

        self.assertEqual(result, {'exposure': 0, 'raw': {'exposureNum': 0}})
        self.assertNotIn('visits', result)
        self.assertNotIn('wants', result)

    def test_wants_is_not_folded_into_visitors(self):
        """「想要人数」不是「访客数」：宁可多一个没人消费的键，也不许张冠李戴。"""
        client, _ = make_client({ITEM_STATS_API: {'wantNum': 6}})
        result = run(client.item_stats(self.ITEM_ID))

        self.assertEqual(result, {'wants': 6, 'raw': {'wantNum': 6}})
        self.assertNotIn('visits', result)
        self.assertNotIn('visitors', result)

    def test_metric_text_is_passed_through_not_guessed(self):
        """`'1,234'` 是干净数字就转 int；`'1.2万'` 带单位**原样透传**，不猜倍数。"""
        client, _ = make_client({ITEM_STATS_API: {'exposureNum': '1,234', 'showNum': '1.2万'}})
        result = run(client.item_stats(self.ITEM_ID))

        self.assertEqual(result['exposure'], 1234)
        self.assertEqual(result['visits'], '1.2万')

    def test_bool_is_not_a_count(self):
        """`True` 是 `int` 子类，不当计数（否则 1 次曝光的假数据会被当真）。

        认不出指标 = 这次没有平台数据 → 抛 `ItemStatsUnavailable`（带原因），
        不再是「成功但返回 None」。
        """
        client, stub = make_client({ITEM_STATS_API: {'exposureNum': True}})
        with self.assertRaises(ItemStatsUnavailable) as ctx:
            run(client.item_stats(self.ITEM_ID))
        self.assertIn('没有可识别的商品数据指标', str(ctx.exception))
        self.assertEqual(stub.apis(), [ITEM_STATS_API])

    def test_unparsable_data_returns_none(self):
        """空/形状不对一律返回 None，不是空 dict——服务层据此走降级。"""
        self.assertIsNone(parse_item_stats({}))
        self.assertIsNone(parse_item_stats(None))
        self.assertIsNone(parse_item_stats([]))
        self.assertIsNone(parse_item_stats({'somethingElse': 1}))
        self.assertIsNone(parse_item_stats({'exposureNum': ''}))

    def test_envelope_shape_is_unwrapped(self):
        """真机回归：平台回的 `data` 是**两层**的（外层信封 + 内层指标）。

        **2026-10-08 用户截图里的原话**就是「平台商品数据接口调用失败（协议层调用失败:
        平台响应里没有可识别的商品数据指标（data={'code': 'success', 'data':
        {'exposureNum': 367, 'showNum': 24, 'wantNum': 1}, 'msg': '成功'}）），已降级为本地统计」
        —— 明明有 367 次曝光，却被整条丢掉。这条用例锁住「两层都要能解析」。
        """
        envelope = {'code': 'success',
                    'data': {'exposureNum': 367, 'showNum': 24, 'wantNum': 1},
                    'msg': '成功'}
        parsed = parse_item_stats(envelope)
        assert parsed is not None
        self.assertEqual(parsed['exposure'], 367)
        self.assertEqual(parsed['visits'], 24)
        self.assertEqual(parsed['wants'], 1)
        self.assertEqual(parsed['raw'], {'exposureNum': 367, 'showNum': 24, 'wantNum': 1})
        # 裸指标 dict（旧调用方 / 单测直接传内层）行为不变
        self.assertEqual(parse_item_stats(envelope['data'])['exposure'], 367)
        # 信封里没指标时仍旧是 None，不是空 dict
        self.assertIsNone(parse_item_stats({'code': 'success', 'data': {}, 'msg': '成功'}))

    def test_envelope_on_the_wire_reaches_the_caller(self):
        """端到端：`mtop` 回两层信封时 `client.item_stats()` 要返回真数值。"""
        envelope = {'code': 'success',
                    'data': {'exposureNum': 41, 'showNum': 7, 'wantNum': 2},
                    'msg': '成功'}
        client, stub = make_client({ITEM_STATS_API: envelope})
        parsed = run(client.item_stats('ITEM-A'))
        self.assertEqual(parsed['exposure'], 41)
        self.assertEqual(parsed['visits'], 7)
        self.assertEqual(parsed['wants'], 2)
        self.assertEqual(len(stub.calls), 1)

    def test_deeper_envelope_is_also_unwrapped(self):
        """信封**不止两层**时也要认得（独立审查 D2）。

        只剥一层的话，平台哪天再包一层就会原样重现「明明有 367 次曝光却报没有可
        识别的指标」—— 那次事故的成本是整条降级成本地统计 + 用户看到一句错的
        原因。剥的层数有上限（`_STATS_ENVELOPE_DEPTH`），到顶就停，不无限往下钻。
        """
        inner = {'exposureNum': 367, 'showNum': 24, 'wantNum': 1}
        three = {'code': 'success', 'data': {'code': 'success', 'data': inner}, 'msg': '成功'}
        parsed = parse_item_stats(three)
        assert parsed is not None
        self.assertEqual((parsed['exposure'], parsed['visits'], parsed['wants']), (367, 24, 1))
        # 剥到上限之外就不再往下找：够深的一层里指标认不出来就该是 None，不猜
        deep = inner
        for _ in range(client_mod._STATS_ENVELOPE_DEPTH + 2):
            deep = {'code': 'success', 'data': deep}
        self.assertIsNone(parse_item_stats(deep))

    def test_missing_item_id_never_calls_platform(self):
        client, stub = make_client({ITEM_STATS_API: {'exposureNum': 1}})
        with self.assertRaises(ItemStatsUnavailable) as ctx:
            run(client.item_stats(''))
        self.assertIn('缺少 itemId', str(ctx.exception))
        self.assertEqual(stub.calls, [])

    def test_session_expired_becomes_unavailable_without_marking_relogin(self):
        """**这条是这个接口最重要的行为**：seller.pc.* 族对 web Cookie 天然
        `SESSION_EXPIRED`，此时必须**不累加熔断计数、也不把账号标成需重登** ——
        否则 `services/common.py` 会 `store.mark_need_relogin()`，把好账号标成需重登。

        但「吞掉」不等于「丢掉原因」：**不是**返回 None，而是换成
        `ItemStatsUnavailable`（**故意不继承 `SessionExpiredError`**），文案要能
        告诉用户是会话拿不到，而不是空字符串。名字里的 `becomes_unavailable`
        就是这条断言，别再写成 `degrades_to_none`。
        """
        stub = MtopStub({}, {ITEM_STATS_API: 'FAIL_SYS_SESSION_EXPIRED::Session过期'})
        client = XianyuClient(COOKIE, transport=httpx.MockTransport(stub))

        with self.assertRaises(ItemStatsUnavailable) as ctx:
            run(client.item_stats(self.ITEM_ID))
        self.assertIn('卖家工作台会话', str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, SessionExpiredError)
        guard = risk.registry.get('2200123456')
        self.assertEqual(guard.status()['consecutive_hits'], 0)
        self.assertFalse(guard.is_blocked)

    def test_unavailable_is_not_treated_as_session_expired_by_services(self):
        """`ItemStatsUnavailable` 必须**不**被服务层判成登录态过期。

        `services/common.py::_is_session_expired` 的第 3 路是**文案兜底**，而这个异常
        会把平台原文带在消息里；一旦判成过期，`mark_need_relogin()` 会把好账号标成
        需重登（用户被逼重新扫码）。所以类型声明要优先于文案。
        """
        from backend.app.services import common

        exc = ItemStatsUnavailable('需要卖家工作台会话：这个接口族不接受网页版 Cookie（账号本身没掉线）')
        self.assertFalse(common._is_session_expired(exc))
        # 反过来：真的 SessionExpiredError 仍然要判成过期（别把放行写成全局关闭）
        self.assertTrue(common._is_session_expired(SessionExpiredError('Session过期')))
        # 平台原文里带 `Session过期` 标记时也不许误判
        self.assertFalse(common._is_session_expired(
            ItemStatsUnavailable('接口不可用: FAIL_SYS_SESSION_EXPIRED::Session过期')))

    def test_risk_control_still_propagates(self):
        """真风控不能瞒：协议层照旧抛 `RiskControlError`（冷却已记进 registry）。"""
        stub = MtopStub({}, {ITEM_STATS_API: 'FAIL_SYS_USER_VALIDATE::哎哟喂,被挤爆啦，请稍后重试'})
        client = XianyuClient(COOKIE, transport=httpx.MockTransport(stub))

        with self.assertRaises(RiskControlError):
            run(client.item_stats(self.ITEM_ID))
        self.assertGreater(risk.registry.get('2200123456').status()['consecutive_hits'], 0)

    def test_unknown_api_failure_raises_unavailable_with_reason(self):
        """接口改名/下线（`FAIL_SYS_API_NOT_FOUND`）时不把整个商品页打挂；
        但**不是**返回 None —— 原因要带出来（和「会话拿不到」区分得开）。"""
        stub = MtopStub({}, {ITEM_STATS_API: 'FAIL_SYS_API_NOT_FOUND::接口不存在'})
        client = XianyuClient(COOKIE, transport=httpx.MockTransport(stub))
        with self.assertRaises(ItemStatsUnavailable) as ctx:
            run(client.item_stats(self.ITEM_ID))
        self.assertIn('接口不可用', str(ctx.exception))
        self.assertIn('FAIL_SYS_API_NOT_FOUND', str(ctx.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
