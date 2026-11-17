"""跨日水位衔接口径（唯一来源）。

全局只允许这一条写法：甲口径 · 昨日现行水位硬闸。
放行侧、水位侧、策略条、衔接目录、处置页全部引用本文件常量，
禁止任何一处另写一套判断，禁止甲乙两口径混用。
口径一旦钉死当天不得再换：改动常量即改动 DOCTRINE_SHA，
验收会以 policy_pinned=False 当场拒绝放行。
"""
import hashlib

DOCTRINE_CODE = "甲"
DOCTRINE_LABEL = "甲口径 · 昨日现行水位硬闸"
DOCTRINE_PINNED_ON = "2026-11-17"
DOCTRINE_RULE = (
    "跨日推进放行前，必须核到该消费方昨日（放行切片业务日的前一自然日）的现行水位；"
    "核不到则今日成功放行不落库、水位推进一并卡死。水位账本只追加不覆盖，"
    "禁止静默吞掉昨日水位，禁止用今日水位覆盖昨日现行。衔接并发串行，"
    "后手记衔接争用且不得写成成功。口径偏离一律拒绝并记衔接口径偏了。"
)
DOCTRINE_REJECTED_RULE = (
    "乙规则（允许今日放行但水位页并列展示缺口告警且不得把今日水位写成连续无断）"
    "本次未启用，仅登记备查；凡按乙规则写放行或写连续水位的，一律判衔接口径偏了。"
)

# 判定码：写进 releases.status / handoff_checks.verdict，页面与账本共用同一组字面量
VERDICT_PASS = "通过"
VERDICT_GAP = "缺口"
VERDICT_CONTENTION = "衔接争用"
VERDICT_DEVIATION = "衔接口径偏了"
VERDICT_ROLLBACK = "水位回拨"
VERDICT_BASELINE = "基线补登"
VERDICT_PENDING = "未核"

CLAIM_STATE_ACTIVE = "核验中"
CLAIM_STATE_DONE = "已完成"


def doctrine_sha(code: str = DOCTRINE_CODE, label: str = DOCTRINE_LABEL,
                 rule: str = DOCTRINE_RULE) -> str:
    return hashlib.sha256(f"{code}|{label}|{rule}".encode("utf-8")).hexdigest()


DOCTRINE_SHA = doctrine_sha()
