"""第 61 批·区域码（division）协议层测试 —— 只打 httpx.MockTransport，不访问真实平台。

盯四件事：

1. api 名 / `v` / `valueType` / 入参**逐字**（官方 bundle 偏移 3,387,049 起 `DivisionSelect`）；
2. **嵌套信封解包**：真机数据在 `env['data']['data']`（2026-10-16 实测，`logs/probe61_division/raw.json`）；
3. 归一化字段与 `value` 格式（官方口径 `divisionId/名称`）；
4. 「平台没给」≠「空 / 否」：缺键是 None，空数组才是 `[]`；真机在区级（level=4）上
   `divisionLeaf` 是 **false**，所以级联终止判据只能用 `level >= 4`（`LEAF_LEVEL`），
   不能用平台 `divisionLeaf`。

跑法（仓库根目录）：
    XIANYU_APP_DIR=$TEMP/xywb_div61 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_xianyu_division.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.xianyu import division as div_proto  # noqa: E402
from backend.app.xianyu.client import XianyuClient, XianyuError  # noqa: E402

#: 真机 L3 首条原文（2026-10-16，`logs/probe61_division/raw.json`）——**divisionLeaf 是 false**。
REAL_DISTRICT = {
    'divisionId': 110101,
    'divisionLeaf': False,
    'divisionToken': 'CN^^^北京^^^北京市^^^东城区',
    'extAttrs': {'pinyin': 'dong cheng qu', 'abbName': '东城'},
    'level': 4,
    'names': {'EN': 'Dongcheng District', 'CN': '东城区'},
    'sensitive': False,
}


class Stub:
    """假的 mtop 端点：记 api / v / valueType / data，回一份平台形状的信封。"""

    def __init__(self, business=None, envelope=None):
        self.calls = []
        self.business = {} if business is None else business
        self.envelope = envelope

    def __call__(self, request):
        qs = parse_qs(request.content.decode())
        payload = json.loads(qs['data'][0]) if 'data' in qs else None
        params = request.url.params
        self.calls.append({
            'api': params.get('api'),
            'v': params.get('v'),
            'value_type': params.get('valueType'),
            'data': payload,
        })
        body = self.envelope if self.envelope is not None else {
            'ret': ['SUCCESS::调用成功'], 'data': self.business,
        }
        return httpx.Response(200, json=body)


class DivisionProtocolTest(unittest.TestCase):
    def setUp(self):
        self.client = XianyuClient('unb=1; _m_h5_tk=abc_123; cookie2=x')
        self.stub = Stub()
        self.client._http = lambda: None

        async def http():
            return httpx.AsyncClient(transport=httpx.MockTransport(self.stub))

        self.client._http = http

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # -- 请求形状 --------------------------------------------------------

    def test_api_name_is_verbatim(self):
        self.assertEqual(div_proto.DIVISIONS_API,
                         'mtop.cainiao.geography.frontend.divisionsinfo.get')
        self.assertEqual(div_proto.DEFAULT_VERSION, '1.0')
        self.assertEqual(div_proto.VALUE_TYPE, 'division')
        self.assertEqual(div_proto.ROOT_TOKEN, 'CN')
        self.assertEqual(div_proto.LEAF_LEVEL, 4)

    def test_request_payload_is_verbatim(self):
        api, data = div_proto.divisions_request('CN^^^北京', 2)
        self.assertEqual(api, div_proto.DIVISIONS_API)
        self.assertEqual(data, {'divisionToken': 'CN^^^北京', 'pageIndex': 2,
                                'bizEntrance': 'idle_fish'})

    def test_blank_token_falls_back_to_root(self):
        for blank in ('', '   ', None):
            _, data = div_proto.divisions_request(blank)
            self.assertEqual(data['divisionToken'], 'CN')
            self.assertEqual(data['pageIndex'], 1)

    def test_page_index_must_be_a_positive_integer(self):
        for bad in ('abc', '0', '-1', '1.5', '1e3'):
            with self.assertRaises(ValueError):
                div_proto.divisions_request('CN', bad)

    def test_token_length_guard(self):
        div_proto.divisions_request('x' * div_proto.MAX_TOKEN_LENGTH)
        with self.assertRaises(ValueError):
            div_proto.divisions_request('x' * (div_proto.MAX_TOKEN_LENGTH + 1))

    def test_read_only_whitelist(self):
        self.assertEqual(div_proto.READ_APIS, (div_proto.DIVISIONS_API,))
        self.assertEqual(div_proto.assert_read(div_proto.DIVISIONS_API), div_proto.DIVISIONS_API)
        with self.assertRaises(div_proto.UnknownApi):
            div_proto.assert_read('mtop.taobao.idle.division.all.get')

    # -- 真机调用：api / v / valueType / 入参 ----------------------------

    def test_mtop_call_locks_api_version_and_value_type(self):
        self._run(div_proto.divisions(self.client))
        call = self.stub.calls[-1]
        self.assertEqual(call['api'], 'mtop.cainiao.geography.frontend.divisionsinfo.get')
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['value_type'], 'division')
        self.assertEqual(call['data'], {'divisionToken': 'CN', 'pageIndex': 1,
                                       'bizEntrance': 'idle_fish'})

    def test_nested_envelope_is_unwrapped(self):
        """真机数据在 `env['data']['data']`（两层），不是 `env['data']`。"""
        self.stub.business = {
            'data': {'divisions': [REAL_DISTRICT], 'totalCount': 17, 'totalPage': 1,
                     'pageIndex': 1, 'pageSize': 100, 'parentToken': 'CN^^^北京^^^北京市'},
            'message': 'SUCCESS', 'status': 0,
        }
        parsed = self._run(div_proto.divisions(self.client, 'CN^^^北京^^^北京市'))
        self.assertEqual(parsed['total_count'], 17)
        self.assertEqual(parsed['parent_token'], 'CN^^^北京^^^北京市')
        self.assertEqual(len(parsed['divisions']), 1)
        self.assertEqual(parsed['divisions'][0]['division_id'], 110101)

    # -- 归一化 ----------------------------------------------------------

    def test_normalized_fields_and_value_format(self):
        row = div_proto.normalize_division(REAL_DISTRICT)
        self.assertEqual(row['division_id'], 110101)
        self.assertEqual(row['division_token'], 'CN^^^北京^^^北京市^^^东城区')
        self.assertEqual(row['label'], '东城区')
        self.assertEqual(row['value'], '110101/东城区')
        self.assertEqual(row['level'], 4)
        self.assertEqual(row['abb_name'], '东城')
        self.assertEqual(row['pinyin'], 'dong cheng qu')

    def test_leaf_flag_is_the_platform_field_not_the_bundle_guess(self):
        """真机：区级（level=4）的 `divisionLeaf` 是 **false** —— 与 bundle 的 `level>=4` 不等价。"""
        row = div_proto.normalize_division(REAL_DISTRICT)
        self.assertIs(row['is_leaf'], False)
        self.assertEqual(row['level'], div_proto.LEAF_LEVEL)
        self.assertIs(div_proto.normalize_division(
            dict(REAL_DISTRICT, divisionLeaf=True))['is_leaf'], True)
        self.assertIsNone(div_proto.normalize_division({})['is_leaf'])

    def test_label_falls_back_to_en_and_value_needs_both_halves(self):
        row = div_proto.normalize_division({'divisionId': 1, 'names': {'EN': 'Beijing'}})
        self.assertEqual(row['label'], 'Beijing')
        self.assertEqual(row['value'], '1/Beijing')
        self.assertEqual(div_proto.normalize_division({'names': {'CN': '北京'}})['value'], '')
        self.assertEqual(div_proto.normalize_division({'divisionId': 7})['value'], '')

    def test_missing_keys_are_none_not_zero_or_blank(self):
        parsed = div_proto.parse_divisions({'data': {}})
        self.assertIsNone(parsed['divisions'])
        for key in ('total_count', 'total_page', 'page_index', 'page_size', 'parent_token'):
            self.assertIsNone(parsed[key], key)
        row = div_proto.normalize_division({})
        self.assertIsNone(row['division_id'])
        self.assertEqual(row['division_token'], '')
        self.assertEqual(row['label'], '')
        self.assertEqual(row['value'], '')
        self.assertIsNone(row['level'])

    def test_empty_divisions_is_an_empty_list(self):
        """平台明确给空数组 -> `[]`（这个上级确实没有下级）；没给键 -> None。"""
        self.assertEqual(div_proto.parse_divisions({'data': {'divisions': []}})['divisions'], [])

    def test_parse_reads_the_platform_envelope_directly(self):
        parsed = div_proto.parse_divisions({
            'ret': ['SUCCESS::调用成功'],
            'data': {'data': {'divisions': [REAL_DISTRICT], 'totalCount': 1}},
        })
        self.assertEqual(parsed['total_count'], 1)
        self.assertEqual(parsed['divisions'][0]['label'], '东城区')

    # -- 平台失败 --------------------------------------------------------

    def test_platform_failure_raises_with_the_platform_text(self):
        self.stub.envelope = {'ret': ['FAIL_SYS_BIZPARAM_MISSED::缺少业务参数divisionToken'],
                              'data': {}}
        with self.assertRaises(XianyuError) as ctx:
            self._run(div_proto.divisions(self.client))
        self.assertIn('FAIL_SYS_BIZPARAM_MISSED', str(ctx.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
