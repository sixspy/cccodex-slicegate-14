# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10624 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 跨日水位衔接（钉死口径）

- 口径册：`docs/跨日水位衔接说明册.md`；代码唯一来源 `backend/doctrine.py`，唯一实现 `backend/handoff.py`。
- 当天口径为**甲 · 昨日现行水位硬闸**：昨日现行水位不可核 → 今日成功放行不落库、水位推进一并卡死；水位账本只追加不覆盖。
- 乙规则仅登记备查，未启用；任何按乙规则放行或把缺口写成连续无断的，判「衔接口径偏了」并拒绝。
- 放行、水位、衔接检查在同一 `BEGIN IMMEDIATE` 事务内完成，两名值班并发只会有一笔成功，后手记「衔接争用」。
- 四份视图（衔接目录 / 策略条 / 水位详情 / 切片列表）与处置页全部读同一个 `data/app.db`，`GET /api/handoff/catalog` 与 `GET /api/handoff/audit` 做同库对账。

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-14
docker compose up -d --build
`

浏览器：http://127.0.0.1:10624/  
健康检查：GET http://127.0.0.1:10624/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10624
python -m pytest -q
`
