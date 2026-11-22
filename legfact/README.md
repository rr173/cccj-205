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
    stand_config.py    # 机位对账：四份 NDJSON 输入的逐行解析与结构校验
    stand_facts.py     # 机位对账：知识截点单次扫描，裁定实到/实离
    stand_engine.py    # 机位对账：物理单元时间账、归属冲突、实测状态机
    stand_store.py     # 机位对账：SQLite 旁表与证据钻取（段流式写入）
    stand_cli.py       # 机位对账：stand run/show/segments/evidence/issues
    backtest_config.py # 到达回测：冻结名册/可见时间表/运行参数的逐行解析与校验
    backtest_engine.py # 到达回测：流式两截点重放、归类、整数微秒误差与在线汇总
    backtest_cli.py    # 到达回测：backtest run/show/samples/checks
  tests/
    generate_fixtures.py  # 确定性合成夹具 + 人工预期表
    check_walkthrough.py  # 独立对照检查器（读人工预期，不读投影逻辑）
    test_warehouse.py     # 边缘语义/原子性/复制重开/乱序等价/内存
    test_cancellation.py  # cancel/restore 冲突与 journey 作用域
    generate_stand_fixtures.py  # 机位对账手算小夹具（含非法输入夹具）
    check_stand_recon.py  # 机位对账：手算分段表 + 朴素逐区间独立对照
    test_stand_recon.py   # 机位对账：不可用原因/冲突/边界/证据行为门禁
    scale_stand_recon.py  # 机位对账：2万周转/20万台账/100万观测演练
    generate_backtest_fixtures.py  # 到达回测：可手算小夹具（五类样本/边界全覆盖）
    check_backtest.py     # 到达回测：不导入回测实现的独立逐检查点对照器
    test_backtest.py      # 到达回测：冻结输入拒绝/陈旧/清空/冲突/证据/流式门禁
    scale_backtest.py     # 到达回测：6万根/72批/40万报文演练（峰值 RSS 测量）
    run_all.sh            # 一键全量门禁
  fixtures/               # 生成产物：main / edge / large / expected
  fixtures/landing/       # 落地使用费小夹具：walkthrough / edge / invalid
  fixtures/stand/         # 机位对账小夹具：handover / unmapped / invalid
  fixtures/backtest/      # 到达回测小夹具：wh / inputs / expected
  docs/landing_mart.md    # 落地使用费数据集市：接口、格式、记账与对账规则
  docs/stand_reconciliation.md  # 机位占用对账：格式、状态机、冲突与证据规则
  docs/arrival_backtest.md      # 到达预测回测：冻结输入、归类优先级、配对比较与规模口径
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

# 到达预测回测（只看历史；冻结名册/可见时间表，输出可逐行核查的 NDJSON）
python3 -I tests/generate_backtest_fixtures.py
python3 -I src/legfact/cli.py backtest run --warehouse /tmp/btwh \
  --spec fixtures/backtest/inputs/spec.json \
  --roster fixtures/backtest/inputs/roster.ndjson \
  --visibility fixtures/backtest/inputs/visibility.ndjson \
  --out /tmp/backtest_out
python3 -I src/legfact/cli.py backtest show    --out-dir /tmp/backtest_out
python3 -I src/legfact/cli.py backtest checks  --out-dir /tmp/backtest_out
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

- `check_backtest.py`：到达预测回测的**独立逐检查点对照器**，不导入回测引擎，
  直接重放仓表重建每行事实、归类、整数微秒误差与每个分母，并核验配对 -8 分钟
  vs 单侧均值相减 -13 分钟的区别。
- `test_backtest.py`：51 项行为门禁，覆盖冻结输入拒绝、五类样本优先级、
  可用/缺失/清空/冲突/陈旧状态、证据定位、不写回源仓与流式 SQL 线性边界。
- `scale_backtest.py`：运行时合成 6 万根 / 每根 4 检查点（24 万样本行）/
  72 批 / 40 万条稀疏报文（含反复更正热点根与延迟闭合关系），在独立进程报告
  分母守恒、抽样来源与峰值 RSS（< 512 MiB；默认跳过，
  `BACKTEST_SCALE=1 bash tests/run_all.sh` 或单独运行）。

仅依赖 Python 3.11 标准库（sqlite3、xml.parsers.expat、hashlib、tracemalloc、
zoneinfo、array）。

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

## 8. 离线机位占用对账

航段事实仓之上的物理单元时间账，详见 `docs/stand_reconciliation.md`。它按
不可重复的物理单元（而非逻辑机位名）切分审核窗口，让班组同时看到实测占用、
台账归属与两者的对账分类，并能从任意时长钻回探测来源、台账行、机位图版本与
航段事实依据；不分配机位、不做实时放行、不从登机口推断机位、不写回源仓。

```bash
# 生成手算小夹具（warehouse 批次 + 周转名册/台账/机位图/探测 四份 NDJSON）
python3 -I tests/generate_stand_fixtures.py

# 运行一次对账（段流式写入 SQLite 旁表，失败不留半成品）
python3 -I src/legfact/cli.py stand run \
  --warehouse /tmp/wh --airport XYZ \
  --window-start "2026-03-10T09:00:00+08:00" \
  --window-end   "2026-03-10T11:00:00+08:00" \
  --receipt-batch 1 --cutoff "2026-03-10T12:00:00+08:00" \
  --roster fixtures/stand/handover/inputs/visits.ndjson \
  --ledger fixtures/stand/handover/inputs/ledger.ndjson \
  --map    fixtures/stand/handover/inputs/map.ndjson \
  --sensor fixtures/stand/handover/inputs/sensor.ndjson \
  --out /tmp/stand_recon.sqlite

# 总量/分段/问题/证据钻取
python3 -I src/legfact/cli.py stand show     --sidecar /tmp/stand_recon.sqlite
python3 -I src/legfact/cli.py stand segments --sidecar /tmp/stand_recon.sqlite
python3 -I src/legfact/cli.py stand issues   --sidecar /tmp/stand_recon.sqlite
python3 -I src/legfact/cli.py stand evidence --sidecar /tmp/stand_recon.sqlite \
       --unit B --at-us 1773109800000000
```

时间账按物理单元无缝分段、时长为精确整数微秒并逐单元守恒；实测占用给确认
下界与含未知的上界（单位为物理单元 × 时间，不是飞机数）。结构损坏、重复主
键、相互覆盖的机位图版本或同单元相互覆盖的在线区间都会令作业失败。

- `check_stand_recon.py`：25 段手算分界 + 朴素逐区间混合端点网格对照；
- `test_stand_recon.py`：五类不可用名册原因、同周转多机位/跨周转冲突、
  半开区间相接、在线重启/离线/右界观测、证据定位与失败不留痕；
- `scale_stand_recon.py`（`STAND_SCALE=1`）：运行时合成 2 万周转、20 万台账、
  100 万观测，含数千活动声明的热点单元，报告处理条数、守恒、证据查询、事实
  扫描 SQL 数与峰值 RSS（门槛 512 MiB），只提交生成器不提交大数据。
