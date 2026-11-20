# 人工预期对照表（独立于投影实现）

本表由人工依据夹具报文逐条重放推导，机器检查器 `check_walkthrough.py`
只读取这里的字面预期与查询引擎输出比对，**不调用投影代码生成预期**。

所有时刻均带显式 UTC 偏移（夹具用 `+08:00`），`service_date` 是 XML 中
独立的当地运营日字段，与消息接收日期、UTC 跨日无关。

## 固定身份事实

| 根 | 初始运营键 | 关系 | 旅程 | 权威 sender |
|----|-----------|------|------|-------------|
| L | CA100（无后缀）2026-11-20 PEK→SHA | 10:00 更名 CA200 | J1 | CA-OCC |
| M | CA300 2026-11-20 SHA→CAN | — | J1 | CA-OCC |
| N | CA100 **2026-11-21** PEK→SHA | 另一运营日同号 | J2 | CA-OCC |
| 营销别名 | KA1000 → L（2026-11-20）/ → N（2026-11-21），半开区间，仅查询别名 | | | |

## 报文时间线（effective_at / revision / sender）

| event | 批次 | 报文用键 + identity_at | effective_at | rev | 内容 |
|-------|------|------------------------|--------------|-----|------|
| E-L-01 | 1 | F=CA100, id@08:00 | 08:30 | 1 | sched_dep 13:00, sched_arr 15:20, est_dep 13:00, 登机口 T3-C12/T2-A07 |
| E-M-01 | 1 | CA300, id@08:00 | 08:45 | 1 | M 的计划时刻 |
| E-N-01 | 1 | CA100/**11-21**, id@08:00 | 08:50 | 1 | N 的计划时刻（11-21） |
| E-L-EXT | 1 | F=CA100, id@09:00, sender=ZZ-GROUND | 09:15 | 9 | est_dep 19:00（**非权威，仅 external_evidence**） |
| E-L-02 | **2** | **旧键 F=CA100, id@09:00（迟到报文）** | **09:30** | 2 | est_dep 改 12:00 |
| E-L-01 重传 | 2 | 同 E-L-01 完全一致 | 08:30 | 1 | 折叠，不产生冲突 |
| E-L-03 | 3 | 新键 CA200, id@11:00 | 13:00 | 3 | 仅清 dep_gate；新增 act_dep 13:07 |
| E-L-CAN | 3 | 新键 CA200, id@13:00 | 14:00 | 1 | journey=J1 cancel |

## 两知识批次 × 多业务时点预期（根 L，除非另注）

| 查询 | 批次知识 | business_time | 身份绑定 | 关键字段预期 | 取消 | 来源要点 |
|------|---------|---------------|----------|--------------|------|----------|
| Q1 | 1 | 09:45 | F→L（initial_leg） | est_dep=**13:00**（E-L-01）；act_dep=absent；dep_gate=T3-C12 | absent | E-L-02 尚在未来批次，不可见 |
| Q2 | **2** | 09:45 | F（id@09:00 落在旧别名半开区间）→L | est_dep=**12:00**，winning=**E-L-02** | absent | 迟到报文用已停用旧键仍可绑定；批次1知识不被改写 |
| Q3 | 1 | 10:30 | 营销 **KA1000→L**（codeshare 链） | sched_dep=value | absent | 营销查询必须说明实际返回运营根 L |
| Q4 | 2 | 10:30 | 新键 CA200→L（rename RN-L-10，10:00 起） | sched_dep=value | absent | 更名后运营航段连续 |
| Q5 | 3 | 13:30 | CA200→L | dep_gate=**cleared**；est_dep 仍=12:00；act_dep=**13:07** | absent | 稀疏报文只清登机口，预计/实际保留；三时刻分列不互覆 |
| Q6 | 3 | 14:30 | CA200→L | 所有已有时刻**原样保留** | **cancelled** | journey J1 取消只改状态；来源 E-L-CAN |
| Q7（根 M） | 3 | 14:30 | CA300→M | sched_dep=value | **cancelled** | J1 成员集合按身份关系求，含 L、M |
| Q8（根 N） | 3 | 14:30 | CA100/**11-21**→N | sched_dep=2026-11-21T13:00 | **absent** | N 属 J2 且是另一运营日，同号不串入、不被取消 |
| Q9（营销） | 3 | 11-21 09:00 | KA1000（11-21 区间）→**N** | — | absent | 同营销号在另一运营日指向另一根 |

## 字段状态判定规则（人工口径）

1. 取 `receipt_batch` 内已知且 `effective_at <= business_time` 的权威报文。
2. 每字段独立取 `(effective_at, source_revision)` 最大者；revision 只在
   同一 sender 内比较，跨 sender 不比较（本题每根固定一个权威 sender）。
3. 同优先坐标不同值 → 该字段 `conflict`，不挑一条，不扩散到其他字段。
4. 缺元素 = 不更新（旧值保留）；显式 `nil`/空 → `cleared` 且压过旧值。
5. 计划/预计/实际是 6 个独立字段列，绝不用一个 time 列互相覆盖。
6. 非权威 sender（ZZ-GROUND、XX-STRANGER）只进 `external_evidence`，
   其载荷永不参与字段投影。

## 边缘场景预期（见 `fixtures/edge/SUITE_LABELS.json` 与 test_warehouse.py）

| 场景 | 预期行为 |
|------|----------|
| 同刻字段冲突（干净根 G，E-G-C1/C2 同 eff 同 rev 不同值） | est_dep=conflict，sched_dep 不受影响仍为 value |
| rename 双根（A→B 而 B 已是独立 initial 根） | 保留 rename_double_root，查询 A 返回 ambiguous，不猜边 |
| rename 分叉（405→406/407）、环（403↔404） | rename_fork / rename_cycle，链不可唯一归属 |
| codeshare 区间重叠（KA3001 同时→A、B） | 营销查询 ambiguous，candidate_roots=[A,B] |
| 前向关系（批次3 用 CA500，根 F 批次4 才声明） | 批次3=unknown/forward；批次4 闭合后同报文变 bound |
| 跨 UTC 午夜（URC 起飞当地 11-23 00:15+08:00） | service_date 仍 2026-11-22；Z 与 +08:00 偏移各自保留 |
| 未知 sender XX-STRANGER 发干净根 G | external_evidence 记录，G 的 conflict 事实不被 15:00 覆盖 |
| 缺 UTC 偏移 / 不支持元素（BookingReference） | 整报文隔离 tombstone，不投影但留存可导出 |
| 中断发布 | open 批次不可查询；重开后可原子发布；失败 ingest 不半覆盖 |
