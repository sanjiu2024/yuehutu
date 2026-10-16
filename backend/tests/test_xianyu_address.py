"""第 64 批·退货地址协议层解析测试 —— 只跑纯函数，不打平台。

盯三件事：

1. **双层信封**：2026-10-16 真机 address.list.query 的数据在 data.data 数组里（原文
   logs/write64/b1e_address_list_raw.json），旧 items_of 只认顶层 data 是数组 ⇒ 真机
   有地址却解析出 0 条（B 组真机验证抓到的真 bug）；
2. **null ≠ []**：账号没有地址时 data.data 是 null，不是空数组；
3. **不造字段**：平台没给的键一律不出现（contact_id 等只在平台给了才出现）。

跑法（仓库根目录）：
    PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_xianyu_address.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.xianyu import trade as trade_proto  # noqa: E402

#: 真机原文（2026-10-16，账号 2217970177348；只去掉 cookies_str，其余逐字保留）。
REAL_RAW = {
    'success': True,
    'data': {
        'code': 'success',
        'data': [
            {'areaId': 330106, 'cityName': '杭州市', 'contactId': 3941114028,
             'contactName': '第64批探针', 'defaultAddr': False,
             'detailAddress': '文一西路 969 号 1 幢 101 室', 'districtName': '西湖区',
             'mobilePhone': '13800000000', 'provinceName': '浙江省', 'zipCode': '000000'},
            {'areaId': 330106, 'cityName': '杭州市', 'contactId': 3943370028,
             'contactName': '第64批探针B', 'defaultAddr': False,
             'detailAddress': '文一西路 969 号 3 幢 303 室', 'districtName': '西湖区',
             'mobilePhone': '13800000002', 'provinceName': '浙江省', 'zipCode': '000000'},
        ],
        'msg': '成功',
    },
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}


class ParseAddressesTest(unittest.TestCase):
    """parse_addresses 的信封解包与字段归一化。"""

    def test_real_double_envelope_yields_both_addresses(self):
        parsed = trade_proto.parse_addresses(REAL_RAW)
        self.assertEqual(len(parsed['addresses']), 2)
        first = parsed['addresses'][0]
        self.assertEqual(first['address_id'], '3941114028')
        self.assertEqual(first['name'], '第64批探针')
        self.assertEqual(first['phone'], '13800000000')
        self.assertIs(first['is_default'], False)
        self.assertEqual(first['contact_id'], '3941114028')
        self.assertEqual(first['province_name'], '浙江省')
        self.assertEqual(first['city_name'], '杭州市')
        self.assertEqual(first['district_name'], '西湖区')
        self.assertEqual(first['detail_address'], '文一西路 969 号 1 幢 101 室')

    def test_null_inner_data_is_empty_not_an_error(self):
        raw = {'success': True, 'data': {'code': 'success', 'data': None, 'msg': '成功'}}
        self.assertEqual(trade_proto.parse_addresses(raw)['addresses'], [])

    def test_legacy_flat_array_still_parses(self):
        raw = {'success': True, 'data': [{'contactId': 7, 'contactName': '张三'}]}
        parsed = trade_proto.parse_addresses(raw)['addresses']
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['address_id'], '7')

    def test_missing_keys_are_not_invented(self):
        raw = {'success': True, 'data': {'code': 'success', 'data': [{'contactId': 7}]}}
        item = trade_proto.parse_addresses(raw)['addresses'][0]
        self.assertEqual(item['address_id'], '7')
        self.assertNotIn('contact_name', item)
        self.assertNotIn('detail_address', item)
        self.assertIsNone(item['is_default'])

    def test_no_address_at_all_is_empty_list(self):
        self.assertEqual(trade_proto.parse_addresses({'success': True})['addresses'], [])


class ItemsOfTest(unittest.TestCase):
    """items_of 的四种容器（回归：双层信封那一支）。"""

    def test_nested_data_array(self):
        self.assertEqual(len(trade_proto.items_of(REAL_RAW)), 2)

    def test_module_items_wins_over_nested(self):
        raw = {'data': {'module': {'items': [1, 2]}, 'data': [3]}}
        self.assertEqual(trade_proto.items_of(raw), [1, 2])

    def test_top_level_array(self):
        self.assertEqual(trade_proto.items_of({'data': [1, 2, 3]}), [1, 2, 3])

    def test_data_items(self):
        self.assertEqual(trade_proto.items_of({'data': {'items': ['a']}}), ['a'])

    def test_unknown_shape_is_empty(self):
        self.assertEqual(trade_proto.items_of({'data': {'code': 'success'}}), [])
        self.assertEqual(trade_proto.items_of(None), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
