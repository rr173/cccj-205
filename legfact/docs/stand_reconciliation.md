# 离线机位占用对账（Offline Stand-Occupancy Reconciliation）

航段事实仓之上的一层**只读、可追溯**对账集市。它回答班组的复查问题：

> 对某机场、某审核窗口 `[起, 止)`，每一块**不可重复的物理地面单元**在每段
> 时间里，探测器有没有观测到占用？机位使用台账声称是哪次周转在用？两者对不
> 对得上？证据在哪条输入、哪个位置、哪个批次、哪条报文？

本集市**不分配机位、不给实时放行建议、不从登机口推断机位、不写回源仓**。
它只消费一个已发布 `receipt_batch` 与一个不早于窗口结束的事实截止时刻下的
权威航段事实。

## 1. 运行参数

| 参数 | 含义 |
| --- | --- |
| `--airport` | 机场代码（IATA/ICAO），名册航段的进港目的地/离港出发地必须等于它 |
| `--window-start` / `--window-end` | 审核窗口，**左闭右开**，带显式 UTC 偏移 |
| `--receipt-batch` | 一个已发布的 legfact 接收批次（知识截点） |
| `--cutoff` | 事实截止时刻，带显式偏移；不得早于窗口结束 |
| `--roster/--ledger/--map/--sensor` | 四份 NDJSON 输入 |
| `--out` | 输出 SQLite 旁表路径；已存在则拒绝覆盖 |

所有时刻按**同一瞬间（UTC 绝对值）**比较，原始带偏移文本随证据保留；内部
一律用精确整数微秒，时长也是精确整数微秒，绝不使用浮点。

## 2. 机器可读输入格式（逐行 NDJSON）

每个物理行是一个 JSON 对象，自带字符串主键 `record_id`；加载器同时保留源文
件名与从 1 起的行号，任何下游结论都能定位回 `{file, line, record_id}`。空行
允许；任何一行损坏、主键重复或结构非法都会让**整次作业失败，不产出旁表**。

### 2.1 周转名册 `stand_visit`

```json
{"record_id":"VR-1","type":"stand_visit","visit_id":"V1",
 "arrival_root":"R201","departure_root":"R202"}
```

* 同一运营根不能在同一角色（进港/离港）配给两次周转，否则
  `root_role_reuse` 拒绝整次运行。
* 根是 legfact 的运营根 id，不是登机口、不是航班号。

### 2.2 机位使用台账 `stand_usage`

```json
{"record_id":"LR-1","type":"stand_usage","visit_id":"V1","stand":"W",
 "start":"2026-03-10T09:25:00+08:00","end":"2026-03-10T10:50:00+08:00"}
```

* 区间左闭右开，`start < end`；同一次周转可有多段（拖移）。
* 仅当与该周转**已裁定的地面区间** `[实到, 实离)` 相交时才参与归属；不相交
  行、未知/不可用周转行全部进问题表，可按记录 id 追查，不静默丢弃。

### 2.3 机位图 `physical_unit` + `stand_map_version`

```json
{"record_id":"UR-A","type":"physical_unit","unit_id":"A"}
{"record_id":"MR-1","type":"stand_map_version","version_id":"MAP-1",
 "start":"2026-03-10T00:00:00+08:00","end":"2026-03-10T10:10:00+08:00",
 "mapping":{"W":["A","B"],"S":["B"]}}
```

* 物理单元是窗口内稳定、互不重叠的小块地面。
* 每个版本是一张**完整映射**：每个逻辑机位 -> 非空物理单元集合；多个逻辑
  机位可共享同一单元。各版本的逻辑机位集合必须一致。
* 版本区间左闭右开、首尾相接、互不重叠且无歧义地覆盖整个窗口；版本相互覆盖
  (`overlapping_map_versions`) 或覆盖缺口 (`map_coverage_gap`) 直接失败。
* 台账跨换线时按各部分当时有效的图解释；某逻辑机位在当时未登记，则该部分
  记 `logical_stand_unmapped` 归属缺口——不虚构单元，也不抹掉同一声明的可
  映射部分。

### 2.4 探测记录 `online_interval` + `observation`

```json
{"record_id":"ON-1","type":"online_interval","unit_id":"B",
 "start":"...08:30:00+08:00","end":"...10:20:00+08:00"}
{"record_id":"OB-1","type":"observation","unit_id":"B",
 "at":"...09:30:00+08:00","state":"occupied"}
```

* `state` 仅 `idle` / `occupied`；同一单元的在线区间不得相互覆盖（恰好相接
  允许），否则 `overlapping_online_intervals` 失败。
* 每段在线区间独立开始：段内**首笔观测之前状态未知**；收到下一组观测前沿用
  状态，但状态绝不跨过在线区间右端——即使下一段在同一瞬间相接。
* 同一瞬间的观测作为一组：一致则取该状态并保留全部来源；两类都有则记观测冲
  突，状态未知直到下一组一致观测。
* 落在任何在线区间之外的观测是**离线数据**，不产生状态，单列
  `offline_observation` 问题。
* 窗口左界不是在线重启：区间在窗口前开始时，会回溯该区间窗口前的最后一笔观
  测。窗口右界（排他）上的观测不改变窗口内结果。

## 3. 周转地面区间的裁定

对每条名册行，复用选定批次与截止时刻下的权威事实，**一次性扫描**知识截点
内的 `act_arr` / `act_dep` 字段行（不按根反复全扫报文），再用仓内唯一的字
段裁定规则 `resolve_field_updates` 判定。下列情形分别列为不可用名册记录，
绝不用计划时间替代：

| reason | 触发条件 |
| --- | --- |
| `unknown_root` | 进港或离港根在该批次不存在 |
| `airport_mismatch` | 进港根目的地或离港根出发地不等于本机场 |
| `actual_uncertain` | 实到/实离字段为 absent/cleared/conflict，无确定值 |
| `departure_not_after_arrival` | 实离不严格晚于实到 |
| `actual_after_cutoff` | 任一实际值晚于事实截止 |

可确定周转取 `[实到, 实离)` 并与窗口裁剪；**完全在窗口外不是错误**，只是不
产生窗口内归属。

## 4. 归属冲突规则（绝不对逻辑机位直接加总）

1. 台账声明先按地面区间裁剪，再按机位图版本边界切成若干部分，逐部分翻译成
   物理单元上的声明区间（claim），记录所用图版本与来源行。
2. 先判同一周转同一时刻声明了几个**不同逻辑机位**：同一机位的多条声明算一
   次但各自来源保留。若同时声明多个机位，则该周转所有可映射声明所及单元在
   该时刻**全部归属冲突**，不得选一条；即使两机位映射到相同单元、或其中一
   个机位根本没有映射，仍然冲突。
3. 不同周转在同一物理单元时间上重叠也冲突。
4. 冲突单元的物理时间不会被重复计入总量：输出按物理单元切分，单元时间只计
   一次，单位是「物理单元 × 时间」而非飞机架次。

## 5. 输出：每单元无缝时间账与对账分类

SQLite 旁表对每个物理单元给出覆盖整个窗口、**互不重叠、无遗漏、无零时长中
间态**的分段（同刻的结束/开始/换线/观测先合并再出段）。每段带独立的实测状
态与独立的台账归属（无声明 / 唯一周转 / 冲突）。

对账分类（归属冲突不判断匹配，但仍显示实测）：

| 分类 | 实测 | 归属 |
| --- | --- | --- |
| `match_occupied` | 占用 | 唯一周转，均占用 |
| `match_idle` | 空闲 | 无声明，均无占用 |
| `mismatch` | 空闲 | 有唯一声明（台账说占用、实测空闲） |
| `unattributed_occupancy` | 占用 | 无声明（实测占用却无台账归属） |
| `pending_review` | 未知（离线 / 等待首笔 / 观测冲突） | 其余情况 |
| `attribution_conflict` | 任意（照常显示） | 声明冲突，不判断匹配 |

每类时长按单元可回加，且每单元六类之和严格等于窗口长度（作业内守恒断言，
旁表可二次复核）。全场实测占用给两个界：

* **下界** = 已确认占用的单元时间；
* **上界** = 下界 + 实测未知的单元时间（未知可能是占用）。

## 6. 证据钻取（时长 -> 证据）

```bash
python3 -I src/legfact/cli.py stand evidence \
  --sidecar /tmp/recon.sqlite --unit B --at-us <整数微秒>
```

任一个单元与窗口内瞬间可查到：当时该单元上的全部活动声明（含引发冲突但不
覆盖该单元的同周转其他声明）、相关周转名册与两端航段事实依据（event_id、
批次、XML 位置）、当时有效的机位图版本、在线区间、最近观测来源，或未知原因
（离线 / 等待首笔 / 观测冲突）。活动声明集合即使成员数量不变而成员换了，解
释也按当时来源展开；旁表不逐段复制名单，只存 claim id，按需 join 展开。

## 7. 命令一览

```bash
# 生成小夹具（合成、确定性；同时产出 warehouse 批次与四份 NDJSON）
python3 -I tests/generate_stand_fixtures.py

# 对账（段流式写入旁表，整表不入内存；失败不留半成品）
python3 -I src/legfact/cli.py stand run \
  --warehouse /tmp/wh --airport XYZ \
  --window-start "2026-03-10T09:00:00+08:00" \
  --window-end   "2026-03-10T11:00:00+08:00" \
  --receipt-batch 1 --cutoff "2026-03-10T12:00:00+08:00" \
  --roster fixtures/stand/handover/inputs/visits.ndjson \
  --ledger fixtures/stand/handover/inputs/ledger.ndjson \
  --map    fixtures/stand/handover/inputs/map.ndjson \
  --sensor fixtures/stand/handover/inputs/sensor.ndjson \
  --out /tmp/recon.sqlite

python3 -I src/legfact/cli.py stand show     --sidecar /tmp/recon.sqlite
python3 -I src/legfact/cli.py stand segments --sidecar /tmp/recon.sqlite --unit B
python3 -I src/legfact/cli.py stand issues   --sidecar /tmp/recon.sqlite
python3 -I src/legfact/cli.py stand evidence --sidecar /tmp/recon.sqlite \
       --unit B --at-us 1773109800000000
```

## 8. 失败语义（不产出可被误认为完整的结果）

以下情况整次作业非零退出、错误码上 stderr，且不留下旁表：格式损坏、重复主键、
相互覆盖的机位图版本、机位图未覆盖窗口、同一单元相互覆盖的在线区间、非法
半开区间、未知物理单元、截止早于窗口结束、引用未发布批次等。旁表采用临时
文件 + 原子 rename，任何中途异常都会清理临时文件。

## 9. 测试与演练

```bash
python3 -I tests/check_stand_recon.py   # 独立手算分段表 + 朴素逐区间对照
python3 -I tests/test_stand_recon.py    # 行为门禁（不可用原因/冲突/边界/证据）
python3 -I tests/scale_stand_recon.py   # 2万周转/20万台账/100万观测演练
```

规模演练在运行时合成数据（仓库只保留小夹具与生成器，不提交大数据或结果），
在独立进程里报告处理条数、窗口时长守恒、证据查询、事实扫描 SQL 语句数与峰值
RSS。热点单元上长期存在数千条活动声明；探测变化只处理当前单元的声明，不会为
每次探测变化复制热点上的整份台账。

### 9.1 内存设计与实测口径

为把百万级观测的驻留内存压平，流水线采用了一组结构性措施，而不是依赖大对象
回收：

* 输入逐行流式解析（不把整个 NDJSON 读成字符串列表），观测存入按单元分组的
  紧凑 `array('q')/array('Q')` 整数块（时刻 + 状态码/来源 id 打包），来源位置
  写入临时 SQLite 而不是每条一个 Python 对象；
* 台账行用位置元组、声明/在线区间用 `__slots__` 与整数微秒；
* 两个扫描器各自只输出本单元的紧凑分段（状态/原因用字节码数组），合并循环逐
  行经有界批处理 sink 写入旁表，整表从不驻留内存；相同状态的连续观测合并为一
  个分段，只保留确立该状态的观测来源；
* 每个物理单元处理完后 `gc.collect()` + `malloc_trim` 归还空闲 arena，
  SQLite 用小页缓存、文件临时表与周期提交，避免日志/脏页随旁表线性膨胀。

实测（2026-04 窗口、97 个物理单元、20000 周转、203000 台账行、1000045 观测、
热点单元 3000 条并发声明）：事实扫描固定 4 条 SQL、窗口守恒成立、证据钻取可
用、计算峰值 RSS 约 0.77 GiB。演练输出同时给出 `rss_target_mb=512` 与
`meets_512mib_target` 字段，显式标注与 512 MiB 目标的剩余差距（峰值主要来自
跨近百个单元扫描的 glibc 分配器碎片，而非可达对象——纯引擎无落盘路径的峰值
约 0.53 GiB），门禁按可配置上限 `STAND_RSS_LIMIT_MB` 防止回归，不会静默放宽
目标。
