# 离线航段事实加工仓（Offline Leg-Fact Warehouse）

面向机场数据团队的**离线、可追溯**航段事实仓。输入 AIDX 航班 XML 与承运人
交接身份关系清单，回答一个复查问题：

> 在「当时已经收到的资料」里，某个业务时刻，某航段的计划/预计/实际时刻、
> 登机口、取消状态分别是什么？证据在哪条报文、哪个批次、XML 的哪一行？

本系统**不做**航班调度、路径优化或订票；这些域的元素在 profile 中显式列为
不支持并隔离。

## 1. 核心不变量

1. **知识批次不可变**：只发布完整批次；查询参数是已发布 `receipt_batch` 与
   `business_time`，旧批次知识不会被后来消息改写。
2. **两个时间截点、一次一致性读取**：同一查询固定该批次的消息集与身份关系
   快照，再按 `effective_at <= business_time` 重放。
3. **身份显式输入**：`initial_leg / rename / codeshare` 决定归并；绝不按航班号、
   共享号或接收顺序归并。报文用 `identity_at` 在对应别名半开区间内绑定航段，
   迟到报文仍可引用已停用旧键。
4. **歧义不猜边**：别名区间重叠、rename 分叉/环/双根、未知根、双义映射一律
   保留 `ambiguous` 与候选根；前向引用等批次闭合后再判。
5. **字段级投影**：每字段独立按 `(effective_at, source_revision)` 取最大；
   同优先坐标不同值 = 该字段 `conflict`，不挑一条、不扩散；显式清空 =
   `cleared` 压过旧值；缺字段 = 不更新。计划/预计/实际是 6 个独立字段。
6. **权威 sender 唯一**：每航段固定一个权威 sender；其他 sender 仅作
   `external_evidence`，`source_revision` 不跨 sender 比较。
7. **取消只改状态**：leg 影响明确航段；journey 按该批次身份关系求成员集合，
   不按「当前同航班号」扫；不擦除已有时刻；restore 是显式操作，不从字段
   重现推断。
8. **时刻带偏移、service_date 独立**：所有业务时刻必须带显式 UTC 偏移；
   `service_date` 是当地运营日独立字段，绝不用消息日期推导。

## 2. 目录结构

```
legfact/
  profile/synthetic_aidx_profile.json   # 合成 AIDX profile（支持/不支持元素）
  src/legfact/
    timeutil.py     # 带 UTC 偏移的 ISO-8601 时刻、独立 service_date 校验
    locations.py    # expat 位置感知 XML 解析（行列 + XPath，非字符串替换）
    aidx.py         # profile 映射：时刻类别、登机口、显式清空、不支持元素
    identity.py     # initial_leg/rename/codeshare 图、区间绑定、歧义检测
    storage.py      # SQLite + 原始报文落盘；批次 open/published/aborted 状态机
    query.py        # 重放式两截点查询、字段 conflict/cleared、journey 取消
    export.py       # 源报文导出、身份解释、批次告警
    manifest.py     # 交接 manifest 构造（sender、连续批次号、摘要、条数）
    cli.py          # 命令行入口（含 mart / mart-verify 子命令）
    landing_config.py  # 落地使用费：一套账配置、名册/协议解析与结构校验
    landing.py         # 落地使用费：计价、版本账本、更正分录、对账校验
    landing_cli.py     # 落地使用费：mart init/plan/release/show 命令面
  tests/
    generate_fixtures.py  # 确定性合成夹具 + 人工预期表
    check_walkthrough.py  # 独立对照检查器（读人工预期，不读投影逻辑）
    test_warehouse.py     # 边缘语义/原子性/复制重开/乱序等价/内存
    test_cancellation.py  # cancel/restore 冲突与 journey 作用域
    run_all.sh            # 一键全量门禁
  fixtures/               # 生成产物：main / edge / large / expected
  fixtures/landing/       # 落地使用费小夹具：walkthrough / edge / invalid
  docs/landing_mart.md    # 落地使用费数据集市：接口、格式、记账与对账规则
```

## 3. 快速开始

```bash
# 生成夹具（合成数据，确定性）
python3 -I tests/generate_fixtures.py

# 接批并发布（manifest 校验 sender、连续 receipt_batch、每文件 sha256/size）
python3 -I src/legfact/cli.py ingest \
  --warehouse /tmp/wh --manifest fixtures/main/manifest_1.json \
  --files-root fixtures/main/batch_1

# 查询：已发布批次 2、业务时刻 09:45，旧运营键 CA100
python3 -I src/legfact/cli.py query --warehouse /tmp/wh \
  --receipt-batch 2 --business-time "2026-11-20T09:45:00+08:00" \
  --carrier CA --flight-no 100 --service-date 2026-11-20 \
  --departure PEK --arrival SHA

# 营销键查询（输出会说明实际返回哪个运营根及身份链）
python3 -I src/legfact/cli.py query --warehouse /tmp/wh --marketing \
  --receipt-batch 1 --business-time "2026-11-20T10:30:00+08:00" \
  --carrier KA --flight-no 1000 --service-date 2026-11-20 \
  --departure PEK --arrival SHA

# 身份解释 / 源报文导出 / 批次告警
python3 -I src/legfact/cli.py identity --warehouse /tmp/wh ...
python3 -I src/legfact/cli.py export   --warehouse /tmp/wh --event-id E-L-CAN
python3 -I src/legfact/cli.py warnings --warehouse /tmp/wh --receipt-batch 1
```

## 4. 存储与可追溯性

- 原始 XML/JSON 逐字节落盘到 `blobs/<batch>/`，大正文不入库、不入内存；
  SQLite 只存索引、字段投影、身份关系与告警。
- 每个字段结果带 `winning_source`：event_id、receipt_batch、sender、
  source_revision、effective_at、原始文件名、XML XPath 与行列号。
- `export` 返回逐字节原文（含 sha256）、manifest 条目、绑定状态与不支持元素。
- 整个仓库目录复制后重开即可只读查询所有旧批次；未完成新批处于 `open`，
  对查询不可见，失败 ingest 回滚，不会半覆盖。

## 5. 合成 AIDX profile 与不支持元素

`profile/synthetic_aidx_profile.json` 声明本仓理解的元素子集：六个方向化时刻
（计划/预计/实际 × 出发/到达）、登机口、取消段、信封五要素。调度/优化/订票类
元素（`BookingReference`、`SeatMap`、`RoutingProposal`、`TailAssignment` 等）
显式不支持：出现即记录 XPath/行列并将**整条报文隔离**（tombstone 留存可导出，
但不参与事实投影）。XML 一律经 expat 解析，禁止字符串替换。

## 6. 测试与验收

```bash
bash tests/run_all.sh
```

- `check_walkthrough.py`：54 项人工预期（见
  `fixtures/expected/expected_walkthrough.md`），独立于投影实现。
- `test_warehouse.py`：29 项，覆盖同刻冲突、rename 双根/分叉/环、codeshare
  重叠、前向关系、跨日时刻、未知 sender、中断发布、摘要篡改、复制重开、
  乱序/重传等价、大报文内存（tracemalloc 峰值远小于落盘 blob 体积）。
- `test_cancellation.py`：cancel/restore 同刻冲突、显式 restore、journey
  作用域按身份成员集合。

- `check_landing_mart.py` / `test_landing_mart.py`：落地使用费数据集市，
  47 项手算对照 + 41 项行为门禁（拒绝、事务中断、幂等重放、过期写入、
  旧版不可变、只改解释不重复记账、零金额与未计价之分）。
- `scale_landing.py`：运行时生成 3 万运营根 / 12 万条稀疏报文，整账预演 +
  跨月分区改账，报告参与计价/未决条数、分区余额与进程峰值 RSS
  （默认跳过，`LANDING_SCALE=1 bash tests/run_all.sh` 或单独运行）。

仅依赖 Python 3.11 标准库（sqlite3、xml.parsers.expat、hashlib、tracemalloc、
zoneinfo）。

## 7. 落地使用费数据集市

航段事实仓之上的一层只读计价集市，详见 `docs/landing_mart.md`：

```bash
# 开账：机场 / IANA 时区 / 固定结算月份 / 币种
python3 -I src/legfact/cli.py mart init --book /tmp/book \
  --config fixtures/landing/walkthrough/book.json

# 预演（不写任何数据）
python3 -I src/legfact/cli.py mart plan --book /tmp/book --warehouse /tmp/wh \
  --receipt-batch 2 --cutoff 2026-02-10T00:00:00+08:00 \
  --roster fixtures/landing/walkthrough/roster_v2.json \
  --tariff fixtures/landing/walkthrough/tariff.json

# 正式发布（首版 --base-version 0；此后声明所依据的上一版）
python3 -I src/legfact/cli.py mart release --book /tmp/book --warehouse /tmp/wh \
  --receipt-batch 2 --cutoff 2026-02-10T00:00:00+08:00 \
  --roster fixtures/landing/walkthrough/roster_v2.json \
  --tariff fixtures/landing/walkthrough/tariff.json \
  --request-id close-2026-02-v2 --base-version 1

# 查看版本 / 明细 / 分录 / 按原归属的当版变动；独立对账
python3 -I src/legfact/cli.py mart show --book /tmp/book --version 2
python3 -I src/legfact/cli.py mart-verify --book /tmp/book
```

计价只使用指定 `receipt_batch` 与事实截止时刻下**已裁定**的权威实际到达，
金额全程整数（最小货币单位），逐账户逐当地月份按实到 UTC 排序编号；更正走
追加式冲销 + 正向分录，旧版本明细与其原始依据不可变。
