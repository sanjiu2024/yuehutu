"""客服（CS）服务：意图分类、快捷回复、自动问候 / 发货提醒 / 常见问答模板。

模板存哪儿？任务卡说「存 settings 或 ai_prompts（scope='global'）」。这里**两边都支持**：

- 模板正文存 `ai_prompts`（role='cs', scope='global', name=模板名）—— 与多 AI 编排的
  提示词体系共用一张表，UI 里能改、能给优先级。
- 模板的**元数据**（模板 id、标题、意图、启用）存 settings 的 `cs.templates` JSON ——
  这样一条模板 = 可编辑正文 + 可编辑元信息，而 `ai_prompts` 保持它原有的语义。

意图分类先用**规则**（关键词打分），够快够稳、不花钱；需要更聪明的判断时，编排层
可以拿 `classify_intent()` 的结果当兜底，或调 `cs` 角色 AI。这里不引入模型依赖。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..core import db
from . import common

TEMPLATES_KEY = 'cs.templates'
CS_ROLE = 'cs'

# 意图 -> 关键词（命中即加分，长关键词权重更高）
INTENT_KEYWORDS: Dict[str, List[str]] = {
    'price': ['多少钱', '什么价', '价格', '便宜', '优惠', '小刀', '刀一下', '少点', '能少', '议价', '最低多少', '包邮吗', '包邮不'],
    'logistics': ['发货', '物流', '快递', '什么时候到', '什么时候发', '几天到', '运单', '单号', '签收', '在路上'],
    'order': ['订单', '下单', '付款', '支付', '拍了', '改价', '取消订单'],
    'aftersale': ['退货', '退款', '换货', '售后', '坏了', '破损', '有问题', '不对', '少发', '漏发', '赔偿'],
    'quality': ['成色', '新旧', '几成新', '划痕', '瑕疵', '磨损', '正品', '真的吗', '验货', '拆过'],
    'spec': ['尺寸', '型号', '规格', '多大', '颜色', '配置', '参数', '重量', '材质'],
    'stock': ['还有吗', '有货', '现货', '库存', '在吗', '在不在', '还卖吗'],
    'greeting': ['你好', '您好', '在吗', '在么', 'hi', 'hello', '老板'],
    'other_item': ['别的', '其他', '另一个', '那件', '这款还有', '其他款'],
}

DEFAULT_QUICK_REPLIES: Dict[str, List[str]] = {
    'greeting': ['您好，在的~ 有什么可以帮您？', '亲，在的，这件还在的。'],
    'price': ['这个价格已经是最低了哈，不刀了~', '可以给您包邮，价格就不动了。'],
    'stock': ['有货的，拍下就能发。', '现货还有，需要的话可以直接拍。'],
    'logistics': ['今天下午就发出，发中通。', '已经发货了，物流信息我帮您查一下。'],
    'order': ['好的，我这就帮您处理。', '麻烦您把订单号发我，我帮您看下。'],
    'aftersale': ['不好意思给您添麻烦了，我帮您确认一下怎么处理。', '可以退的，您申请一下退款我这边通过。'],
    'quality': ['成色如图，正常使用痕迹，功能都正常。', '是正品，可以放心，支持验货。'],
    'spec': ['规格我发您图片看下，稍等。', '尺寸是标准的，具体我量一下告诉您。'],
    'other_item': ['这件也有的，我帮您找找。', '您说的是哪一件？发个截图我看看。'],
    'unknown': ['稍等，我看一下再回您。', '这个问题我确认下，马上回复您。'],
}

DEFAULT_TEMPLATES: List[Dict[str, Any]] = [
    {'id': 'greeting', 'name': '自动问候', 'intent': 'greeting', 'enabled': True,
     'content': '您好，欢迎光临~ 这件商品还在的，有什么问题随时问我。'},
    {'id': 'ship_reminder', 'name': '发货提醒', 'intent': 'logistics', 'enabled': True,
     'content': '亲，您的订单今天已经发出啦，快递单号 {tracking_no}，注意查收哦~'},
    {'id': 'ship_delay', 'name': '延迟发货说明', 'intent': 'logistics', 'enabled': True,
     'content': '不好意思，您这单我明天一早给您发出去，请见谅。'},
    {'id': 'faq_package', 'name': '常见问答·包邮', 'intent': 'price', 'enabled': True,
     'content': '全国包邮的，不额外收运费。'},
    {'id': 'faq_invoice', 'name': '常见问答·发票', 'intent': 'aftersale', 'enabled': True,
     'content': '个人闲置，没有发票哦，介意的话请慎拍。'},
    {'id': 'faq_quality', 'name': '常见问答·成色', 'intent': 'quality', 'enabled': True,
     'content': '实物拍摄，成色如图，正常使用痕迹，功能一切正常。'},
]


# --------------------------------------------------------------------------- 意图


def classify_intent(text: str) -> str:
    """规则打分分类；没命中返回 'unknown'。"""
    content = (text or '').strip().lower()
    if not content:
        return 'unknown'
    scores: Dict[str, int] = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        score = 0
        for word in keywords:
            if word.lower() in content:
                score += len(word)
        if score:
            scores[intent] = score
    if not scores:
        return 'unknown'
    # 分数相同按 INTENT_KEYWORDS 声明顺序（价格/物流等业务优先级高于问候）
    order = {name: index for index, name in enumerate(INTENT_KEYWORDS)}
    return max(scores, key=lambda intent: (scores[intent], -order[intent]))


def quick_replies(intent: str) -> List[str]:
    """某个意图的快捷回复候选（先取模板库里同意图的，再补内置的）。"""
    intent = intent or 'unknown'
    out: List[str] = []
    for template in list_templates():
        if template.get('intent') == intent and template.get('enabled', True):
            content = str(template.get('content') or '').strip()
            if content and content not in out:
                out.append(content)
    for content in DEFAULT_QUICK_REPLIES.get(intent, DEFAULT_QUICK_REPLIES['unknown']):
        if content not in out:
            out.append(content)
    return out


def suggest(text: str) -> Dict[str, Any]:
    """给人工回复台用：一句话 -> 意图 + 候选回复。"""
    intent = classify_intent(text)
    return {'intent': intent, 'replies': quick_replies(intent)}


# --------------------------------------------------------------------------- 模板


def _seed_templates() -> None:
    if db.query_one('SELECT key FROM settings WHERE key = ?', (TEMPLATES_KEY,)) is None:
        common.set_setting(TEMPLATES_KEY, json.dumps(DEFAULT_TEMPLATES, ensure_ascii=False))


def list_templates() -> List[Dict[str, Any]]:
    """模板列表：settings 里的元数据 + ai_prompts 里的正文（正文优先）。"""
    _seed_templates()
    raw = common.get_setting(TEMPLATES_KEY, '[]')
    try:
        meta = json.loads(raw)
    except ValueError:
        meta = []
    if not isinstance(meta, list):
        meta = []
    # 用 ai_prompts(role='cs', scope='global') 的正文覆盖同名模板
    prompts = {
        row['name']: row['content']
        for row in db.query(
            "SELECT name, content FROM ai_prompts WHERE role = ? AND scope = 'global' AND enabled = 1",
            (CS_ROLE,),
        )
    }
    templates = []
    for entry in meta:
        if not isinstance(entry, dict):
            continue
        template = dict(entry)
        name = str(template.get('name') or template.get('id') or '')
        if name in prompts:
            template['content'] = prompts[name]
            template['source'] = 'ai_prompts'
        else:
            template['source'] = 'settings'
        template['enabled'] = bool(template.get('enabled', True))
        templates.append(template)
    return templates


def get_template(template_id: str) -> Any:
    for template in list_templates():
        if template.get('id') == template_id or template.get('name') == template_id:
            return template
    return common.fail(f'模板不存在: {template_id}', 'not_found')


def save_template(template_id: str, fields: Dict[str, Any]) -> Any:
    """新增或修改模板。正文同时写 ai_prompts（scope='global'）以便编排层复用。"""
    if not template_id:
        return common.fail('template_id 不能为空', 'bad_request')
    _seed_templates()
    raw = common.get_setting(TEMPLATES_KEY, '[]')
    try:
        meta = json.loads(raw)
    except ValueError:
        meta = []
    if not isinstance(meta, list):
        meta = []

    entry: Optional[Dict[str, Any]] = None
    for item in meta:
        if isinstance(item, dict) and (item.get('id') == template_id or item.get('name') == template_id):
            entry = item
            break
    if entry is None:
        entry = {'id': template_id, 'name': fields.get('name') or template_id, 'intent': fields.get('intent') or 'unknown', 'enabled': True, 'content': ''}
        meta.append(entry)

    for key in ('name', 'intent', 'content'):
        if key in fields and fields[key] is not None:
            entry[key] = str(fields[key])
    if 'enabled' in fields:
        entry['enabled'] = common.truthy(fields['enabled'], True)

    common.set_setting(TEMPLATES_KEY, json.dumps(meta, ensure_ascii=False))

    content = str(entry.get('content') or '')
    name = str(entry.get('name') or template_id)
    if content:
        existing = db.query_one(
            "SELECT id FROM ai_prompts WHERE role = ? AND scope = 'global' AND name = ?",
            (CS_ROLE, name),
        )
        if existing is None:
            db.execute(
                "INSERT INTO ai_prompts(role, scope, name, content, priority) VALUES(?, 'global', ?, ?, 0)",
                (CS_ROLE, name, content),
            )
        else:
            db.execute(
                'UPDATE ai_prompts SET content = ?, enabled = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (content, existing['id']),
            )
    common.publish('cs.templates_changed', {'template_id': template_id})
    return get_template(template_id)


def delete_template(template_id: str) -> Any:
    _seed_templates()
    raw = common.get_setting(TEMPLATES_KEY, '[]')
    try:
        meta = json.loads(raw)
    except ValueError:
        meta = []
    if not isinstance(meta, list):
        meta = []
    kept = [m for m in meta if not (isinstance(m, dict) and (m.get('id') == template_id or m.get('name') == template_id))]
    if len(kept) == len(meta):
        return common.fail(f'模板不存在: {template_id}', 'not_found')
    common.set_setting(TEMPLATES_KEY, json.dumps(kept, ensure_ascii=False))
    common.publish('cs.templates_changed', {'template_id': template_id, 'deleted': True})
    return {'deleted': template_id}


def render(template_id: str, **values: Any) -> Any:
    """渲染模板：`{tracking_no}` 之类占位符用 values 填。缺的占位符原样保留。"""
    template = get_template(template_id)
    if common.is_failure(template):
        return template
    content = str(template.get('content') or '')

    class _Keep(dict):
        def __missing__(self, key):  # 缺参数时保留占位符，不炸
            return '{' + key + '}'

    return {'template_id': template.get('id'), 'content': content.format_map(_Keep(**values))}


def greeting(item_title: str = '') -> Dict[str, Any]:
    """自动问候：买家第一次来的时候用。"""
    result = render('greeting', item_title=item_title)
    if common.is_failure(result):
        return {'content': DEFAULT_TEMPLATES[0]['content']}
    return result


def shipping_reminder(tracking_no: str = '', company: str = '') -> Dict[str, Any]:
    """发货提醒：填了单号用发货模板，没填用延迟说明。"""
    template_id = 'ship_reminder' if tracking_no else 'ship_delay'
    result = render(template_id, tracking_no=tracking_no, company=company)
    if common.is_failure(result):
        fallback = next(t for t in DEFAULT_TEMPLATES if t['id'] == template_id)
        return {'content': fallback['content']}
    return result


def faq(keyword: str) -> Dict[str, Any]:
    """常见问答：按关键词找意图，返回该意图下的模板/快捷回复。"""
    intent = classify_intent(keyword)
    replies = quick_replies(intent)
    return {'intent': intent, 'content': replies[0] if replies else '', 'replies': replies}
