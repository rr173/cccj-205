# 离线到达预测回测（Offline Arrival-Prediction Backtest）

航段事实仓之上的一层**只读、只看历史**的回测集市。它回答值班组的复查问题，
不预测任何新航班、不向运行系统输出决策：

> 在**事先冻结的检查点**上，值班员当时实际能看到的计划到达与预计到达分别是
> 什么？哪些航班是事后才知道当时已经落地？在**同一批可评分样本**上，预计与
> 计划到底谁更准？证据在哪条报文、哪个批次、XML 的哪一行？

值班组最初的抽查偏差（只看当天回填后的记录、且有预计值的航班本身更好预测）
由两条机制消除：**检查点先于答案冻结**，以及**单侧指标与配对指标分开报告**。

## 1. 一次回测的冻结输入

一次回测只针对一个机场，输入三份机器可读、可逐行处理的文件。

### 1.1 运行参数 `spec.json`（单个 JSON 对象）

```json
{
  "airport": "CAN",
  "close_out_at": "2026-05-12T11:00:00+08:00",
  "lead_minutes": [30, 60],
  "coverage_start": "2026-05-12T07:30:00+08:00",
  "coverage_end":   "2026-05-12T12:00:00+08:00"
}
```

| 字段 | 含义与校验 |
| --- | --- |
| `airport` | 本场代码；运营根的**初始**目的地必须等于它 |
| `close_out_at` | 统一的结果封账时刻，带显式 UTC 偏移；不得早于任何检查点 |
| `lead_minutes` | 互不重复的正整数提前分钟数（拒绝 0、负数、浮点、布尔、重复） |
| `coverage_start/end` | 值班系统可见时间表声明的**完整覆盖**起止，含两端 |

### 1.2 观测名册 `roster.ndjson`（每行一个 JSON 对象）

```json
{"record_id":"RR-G1","type":"backtest_roster_row","root_id":"G1",
 "group":"G1","roster_sched_arr":"2026-05-12T10:30:00+08:00"}
```

* 每个运营根**只列一次**，重复 `root_id` 直接拒绝整次运行；未知根也保留为
  名册行，不会被删除。
* `roster_sched_arr` 是**名册原定计划到达**，只用于枚举检查点；仓里后来的计划
  改时不会重新分组或挪动检查点，也绝不拿它回填计划事实字段。
* 检查点 = 名册原定时间 − 提前分钟数，按精确 UTC 瞬间比较。

### 1.3 批次可见时间表 `visibility.ndjson`

```json
{"record_id":"VB-2","type":"batch_visibility","batch_no":2,
 "first_visible_at":"2026-05-12T10:00:00+08:00"}
```

* 从第一批起列出已发布批次的**连续前缀**及各批首次对值班员可见的时间；在覆盖
  起点之前已可见的批次也要列出，不能漏掉覆盖范围内已可见的批次。
* 批次号必须是 1,2,3,…，**无缺漏、无重复、不倒退**；可见时刻**不得逆序**，
  同一瞬间可见的多批都计入（取其中最大批次号为知识截点）。
* 所有检查点与封账必须落在声明覆盖范围内；表中每个批次都必须在仓内已发布。
* 所有时刻必须带显式偏移或 `Z`，格式或结构错误整次作业失败。

## 2. 重建两个当时基准，再单独查答案

每个检查点只取**当时最后一个可见批次**（知识截点），并在该批次自己的身份
关系快照上重放，只接受 `effective_at <= 检查点` 且属于该根权威 sender 的更新。

* **尚未生效的更新不进事实**：10:00 已收到但 10:01 才生效的预计，不进 10:00
  的成绩。
* **后来才可见的更新不进事实**：10:05 才可见、却追溯到 09:30 生效的预计，同样
  不污染 10:00；可见性与生效性是两道独立的门。
* **历史身份不被当前绑定替代**：某截点还不可用的根、初始目的地不是本场的根、
  后来才闭合的关系，都保留独立状态；后来的关系声明不能替过去的预测完成绑定。
* 沿用仓内唯一的字段裁定规则 `resolve_field_updates`：保留空缺（`absent`）、
  显式清空（`cleared`）、同优先坐标不同值（`conflict`，不挑边、不扩散）。
* 两个预测字段（`仓内 sched_arr / est_arr`）额外标注 `stale`：获胜报文的预测
  时刻不晚于检查点即视为陈旧。计划不用名册时间补，预计不用计划值补。
* 实到答案在**封账可见批次**上**另取一次**；封账保留未来生效的确切实到，以便
  区分「实到晚于封账」与「实到不确定」。

## 3. 先定分母，再比误差（固定归类优先级）

每个检查点按以下**顺序**归类，两个截点（检查点、封账）的状态各自保留：

1. `unobservable`：检查点无有效批次（`no_batch_visible_yet`），或本场运营根在
   该截点不可用（`operating_root_unavailable_at_cutoff`），或初始目的地不是本场
   （`initial_destination_not_this_airport`）。
2. `known_landed_at_checkpoint`：检查点已有不晚于检查点的确切实到。
3. `close_out_undetermined`：封账没有可用本场根
   （`no_airport_operating_root_at_close_out`）、实到不确定
   （`actual_arrival_uncertain`，含 absent/cleared/conflict）、或实到晚于封账
   （`actual_arrival_after_close_out`）。
4. `later_known_already_landed`：封账实到不晚于检查点（事后才知已落地）。
5. 其余才是 `target_population`（可评分目标人群）。

对每个「分组 × 提前分钟」以及每个提前分钟的全场总体，报告：名册点数、四类
排除数、目标人数、两个预测在目标人群中的单侧覆盖人数、两者同时可用的配对
人数。

* **单侧指标**分别按各自可用样本计算；**比较指标只在配对样本上给出**。
* 误差 = 预测 − 实际的**有符号整数微秒**；每个指标块给出可复算均值的
  `n`、有符号误差整数总和、绝对误差整数总和，以及绝对误差 P90
  （排序后第 `ceil(0.9*N)` 个，序号从 1 起；空集合不报成零误差）。
* 全场 P90 从全场样本直接算，**不平均**各分组 P90。
* 配对比较给出 `paired_abs_diff_est_minus_sched_us`，即
  `Σ(|预计误差| − |计划误差|)`；负值表示预计更准。**不能**用两个不同人群的
  平均误差相减来推断优劣。

## 4. 输出（逐行可核查，不写回源仓）

`backtest run` 原子地新建一个输出目录（临时目录 + rename，失败不留半成品）：

```
out_dir/
  header.json     # 机场、封账、提前分钟、覆盖、输入 sha256、流式统计
  summary.json    # 分组/全场结果块与分母守恒检查
  samples.ndjson  # 每个检查点一行：批次、字段状态、获胜/冲突报文原文定位、
                  # 归类与细分原因、有符号误差整数
```

`summary.json` 的每个分母都能在 `samples.ndjson` 上按行加总对上；守恒检查
`conservation.checks.all_conservation_ok` 必须为真。

```bash
python3 -I src/legfact/cli.py backtest run \
  --warehouse /tmp/wh \
  --spec fixtures/backtest/inputs/spec.json \
  --roster fixtures/backtest/inputs/roster.ndjson \
  --visibility fixtures/backtest/inputs/visibility.ndjson \
  --out /tmp/backtest_out

python3 -I src/legfact/cli.py backtest show    --out-dir /tmp/backtest_out
python3 -I src/legfact/cli.py backtest samples --out-dir /tmp/backtest_out --group G1
python3 -I src/legfact/cli.py backtest checks  --out-dir /tmp/backtest_out
```

## 5. 手算复查例子（夹具内置，独立对照器核验）

`tests/generate_backtest_fixtures.py` 生成可在纸面上复算的小夹具。核心例子：
同组两根在 10:00 风格的截点上——

* A：所见预计 10:18、计划 10:30，封账实到 10:20 →
  `|预计−实到|=2 分钟`，`|计划−实到|=10 分钟`；
* B：所见预计已**清空**、计划 10:45，封账实到 10:25 → 预计不可用，
  `|计划−实到|=20 分钟`。

两根都在目标人群：计划覆盖 2、预计覆盖 1、配对 1。

* 配对绝对误差之差（预计减计划）= `2 − 10 = −8 分钟`（`-480_000_000` 微秒）。
* 若错误地用两个单侧**平均**绝对误差相减：
  `2 − (10+20)/2 = −13 分钟`（`-780_000_000` 微秒）——人群不同，结论不可比。

同一夹具还让 A 同时拥有「10:00 已收到但 10:01 才生效」和「10:05 才可见却追溯
生效」的两条预计，证明二者都不污染 10:00 成绩；并覆盖恰在可见瞬间的批次、
后续才闭合的身份关联、事后发现已落地、封账冲突、带小数秒的整数微秒误差，以及
分组与全场分位数差异。`tests/check_backtest.py` 是**不导入回测实现**的独立逐
检查点对照器，直接重放仓表重建每行事实与每个分母。

## 6. 规模与内存

`tests/scale_backtest.py` 在运行时合成并演练：至少 6 万个运营根、每根 4 个
检查点（24 万样本行）、至少 72 个可见批次、至少 40 万条稀疏报文；多数批次只
改动少数根，另含反复更正的热点根与分散在不同批次才闭合的关系。

设计上**不逐样本扫描全仓、不按批次反复扫描全部报文、不展开报文×检查点笛卡尔
积**：每个事实字段在每个不同可见批次只开一条索引 SQL 流（共
`3 × 批次数 + 1` 条量级），按根保留紧凑的优先组，样本行通过 sink 直接流式
落盘，汇总在线累加。回测在独立进程中测量 Linux 峰值 RSS（VmHWM），单次进程
门槛 512 MiB；大数据与运行产物不提交，仓库只保留小夹具与可复跑生成器。

实测（`tests/scale_backtest.py`，60,000 根 / 每根 4 检查点 / 240,000 样本行 /
72 批 / 400,000 条报文）：

* SQL 语句 217 条（恰为 `3 × 72 + 1`，与批次数和字段数线性相关，与样本数无关）；
* 明细 240,000 行全部流式落盘，分母守恒与全场整数 P90 校验通过；
* 回测计算单次进程峰值 RSS 约 **496 MiB（< 512 MiB）**。

为让 RSS 高水位反映真实存活工作集而非保留的 pymalloc arena，规模演练在独立子
进程里设置 `PYTHONMALLOC=malloc` 与 `MALLOC_TRIM_THRESHOLD_=65536`（与机位
规模演练同一手法）；这是测量口径的一部分，已固化在 `scale_backtest.py` 中。
内存之所以可控，来自：每批只保留一个历史身份图、检查点用 `array` 紧凑列而非
每检查点对象、字符串与证据按需驻留（只为真正获胜的报文建证据）、明细不进内存。

```bash
python3 -I tests/generate_backtest_fixtures.py     # 小夹具
python3 -I tests/check_backtest.py ...             # 独立逐检查点对照
python3 -I tests/test_backtest.py                  # 行为/拒绝门禁
python3 -I tests/scale_backtest.py                 # 60k 根规模演练
```
