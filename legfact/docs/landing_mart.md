# 落地使用费数据集市（Landing-Fee Data Mart）

机场每月把「落地使用费」明细交财务。本集市建立在现有 `legfact` 航段事实仓
之上，**只读**一个已发布的 `receipt_batch` 知识截点，只做计价、对账与更正
分录；不做支付、开票、航班调度，也不把任何计价结果写回航段源仓。

- 代码：`src/legfact/landing_config.py`（维度解析与结构校验）、
  `src/legfact/landing.py`（取数、计价、版本账本）、
  `src/legfact/landing_cli.py`（`legfact mart ...` 子命令）
- 夹具：`fixtures/landing/`（仅小夹具；规模数据由脚本运行时生成）
- 测试：`tests/generate_landing_fixtures.py`、`tests/check_landing_mart.py`、
  `tests/test_landing_mart.py`、`tests/scale_landing.py`

## 1. 一套账（book）

`mart init` 开账，开账时固定四件事，之后不可改：

| 字段 | 含义 |
|------|------|
| `airport` | 机场代码，例如 `SHA`。只有**到达**该机场的落地才属于本账 |
| `timezone` | IANA 时区，例如 `Asia/Shanghai`。所有「当地日期」换算都用它 |
| `months` | 固定结算月份集合，`YYYY-MM`，例如 `["2026-01","2026-02"]` |
| `currency` | 结算币种，ISO-4217 风格代码，例如 `CNY`。所有金额是**最小货币单位的非负整数**（分），绝不使用浮点 |

开账配置（`book.json`）：

```json
{"airport": "SHA", "timezone": "Asia/Shanghai",
 "months": ["2026-01", "2026-02"], "currency": "CNY"}
```

## 2. 一次计算的输入

`mart plan` / `mart release` 共同接受：

| 参数 | 含义 |
|------|------|
| `--warehouse DIR` | 航段仓目录（已发布批次） |
| `--receipt-batch N` | 已发布的 legfact 接收批次 = 知识截点 |
| `--cutoff TS` | **带显式 UTC 偏移**的事实截止时刻，例如 `2026-02-10T00:00:00+08:00` |
| `--roster FILE` | 落地计量名册（本次计算的范围，全量替换） |
| `--tariff FILE` | 协议（全量替换） |

### 2.1 落地计量名册 `roster.json`

```json
{"airport": "SHA",
 "roots": [
   {"root_id": "R-A",
    "root": {"carrier": "CA", "flight_no": "101", "suffix": "",
             "service_date": "2026-01-01", "departure": "PEK", "arrival": "SHA"},
    "account": "ACCT-A", "weight_kg": 1000, "category": "JET"}]}
```

- 每条以 legfact 的**运营根**标识：`root_id` 必须是该知识截点里某个
  `initial_leg` 的 `root_id`；`root` 必须就是那条 `initial_leg` 的初始运营键
  （六元组）。行与声明不一致时拒绝整次计算（`roster_key_mismatch`），不会
  用另一个键去凑一笔落地。运营键名册与报文里的改名/codeshare 别名无关——不同异名不代表
  多次落地，改名前后仍是同一个根、同一笔落地。
- 名册即本次范围：从名册移除的根不再计费，也不会因此报错。
- 结构错误拒绝整次计算：重复 `root_id`、同一运营键被两个根占用、重量不是
  正整数千克、账户/类别为空。

### 2.2 协议 `tariff.json`

```json
{"airport": "SHA", "currency": "CNY",
 "rates": [
   {"category": "JET", "valid_from": "2026-01-01",
    "valid_to": "2026-02-01", "min_per_unit": 1000}],
 "serials": [
   {"account": "ACCT-A",
    "rungs": [{"upto_serial": 1, "ten_thousandths": 10000},
              {"upto_serial": null, "ten_thousandths": 5000}]}]}
```

- `rates`：每个类别在**机场当地日期**上的半开区间 `[valid_from, valid_to)`
  内每计费单位收多少最小货币单位（非负整数）。
- `serials`：每个账户的月内序号阶梯。按递增的**含上界**序号 `upto_serial`
  排列，最后一档 `upto_serial: null` 无上界（兜底）。倍率是万分数整数，
  `10000` = ×1。
- 拒绝整次计算的结构错误：同类别区间重叠、阶梯缺兜底档、阶梯上界不递增、
  重复账户阶梯、负费率、协议机场/币种与账不符。

## 3. 计价规则

1. **只用权威实际到达**：只使用 `legfact` 在该 `receipt_batch`、该
   `effective_at <= cutoff` 知识截点已经裁定的 `act_arr` 字段，以及它的
   `winning_source`（事件、批次、报文、XML 行列）。计划/预计/外部报文一律
   不能填空。字段裁定复用仓内唯一实现
   `legfact.query.resolve_field_updates`（同时刻同版本不同值 = `conflict`，
   显式清空 = `cleared`）。
2. **实际到达不晚于事实截止**：字段值本身的 UTC 瞬间晚于 `cutoff` 的，不参与
   计价（`actual_after_cutoff`）。
3. **未计价四类原因**（都**不占**阶梯序号，也不产生任何金额）：
   - `unknown_root`：名册根不在本知识截点
   - `arrival_airport_mismatch`：根的**初始运营键**到达机场不是本账机场
   - `no_definite_actual`：无实际值 / 已清空 / 冲突
   - `actual_after_cutoff`：实际值晚于事实截止
4. **取消状态不影响落地事实**：实际值确定时，即便另有取消状态，仍按实际落地
   计费。
5. **当地日期**：用实到瞬间换算到账本时区的当地日期；当地日期落在约定月份
   之外时状态为 `out_of_range`——**可见、不收费、不占序号**，也不算未计价。
6. **序号**：每个账户 × 每个当地月份，按实际 UTC 瞬间升序，同瞬间按
   `root_id` 升序，从 1 开始。类别变化、日期变化**不重置**序号。
7. **计费单位**：重量必须是正整数千克，每 1000 千克为 1 计费单位，不足按 1
   计：`units = ceil(kg / 1000)`。
8. **金额**：`units × min_per_unit × ten_thousandths / 10000`，全程整数，
   **只在逐条最终金额处**四舍五入（半分进位，非银行家舍入）。零金额是
   **已计价**行，保留记账身份；未计价行没有金额，绝不是「零元」。
9. **完整性命中**：某个合格落地没有对应费率条目，或账户没有可用阶梯档，
   **拒绝本次出数**（`no_tariff_entry` / `no_serial_rung`），不静默按零元计。

## 4. 版本账本（记账模型）

### 4.1 预演 `mart plan`

输出完整逐条明细、汇总、未计价清单和**预计变动**（相对当前有效记账的冲销/
正向）。**不写任何数据**。

### 4.2 正式发布 `mart release`

必须声明 `--base-version N`（所依据的上一发布版本，首版为不存在的 `null`，
CLI 用 `--base-version 0` 表示「无上一版」）与 `--request-id`。

规则：

- 第一版每个已计价根生成一条**正向记账**记录。
- 之后每版对比「上一版有效记账」（未冲销的正向记录）：
  - 原根不再可计价 → 以**原账户、原月份、原金额**生成冲销，指向尚未冲销的
    那条正向记录；
  - 账户、月份或金额任一变化 → 同样冲销原记录；根仍应计价时，再按新结果生成
    正向记录；
  - 之前没有入账的根直接补记正向；
  - 只改来源、序号或费率说明而不改账户/月份/金额 → 保留新解释（写进新版本
    明细），**不重复记账**，原记账身份继续有效。
- 金额为零的已计价行也有记账身份。
- 不得只重算被更正的根：同账户所有合格落地重新编号，受影响者必须重算（见
  演练）。不得按新费率估算冲销金额——冲销一律取原记账金额的相反数。

### 4.3 幂等、过期与中断

- 同一 `request_id` 且输入指纹相同 → 返回原版本（`replayed: true`），不新增
  版本；同一 `request_id` 不同输入 → `request_conflict`。
- `--base-version` 不是当前已发布头 → `stale_base_version`，拒绝写入。
- 版本行、分录、冲销状态、明细在同一 SQLite 事务内写入；进程中断后要么全无、
  要么全有，读取者永远看不到「跨月冲销只做了一边」或「分录已追加但明细未
  发布」。中断后用原 `request_id` 重试即可。
- 读取者只读已发布版本：旧版本明细、分录、当时引用的名册与协议原文（版本行中
  保存 `roster_json`/`tariff_json` 及 sha256）不随新报文或新维度改变。

### 4.4 对账不变量

对每个已发布版本 V：

- `sum(该版本明细中 priced 行 amount_minor) == 该版本 effective_total_minor`；
- `sum(截至 V 的全部有符号分录) == V 的有效金额`；
- 每个「开放记账」在 V 的明细中能找到同 `posting_id`、同金额的行；
- 逐条明细可通过 `evidence` 回到原始报文、批次与 XML 位置。

`legfact mart-verify --book DIR` 独立重算上述不变量（只读存储行，不重跑计价）。

## 5. 命令行与机器可读格式

所有命令输出 JSON 到 stdout，错误输出 `{"error": code, "detail": ...}` 到
stderr 并以退出码 2 结束（`mart-verify` 不变量失败退出码 1）。

```bash
# 开账
python3 -I src/legfact/cli.py mart init --book /tmp/book --config book.json

# 预演（不写）
python3 -I src/legfact/cli.py mart plan --book /tmp/book --warehouse /tmp/wh \
  --receipt-batch 1 --cutoff 2026-02-10T00:00:00+08:00 \
  --roster roster.json --tariff tariff.json

# 正式发布：首版 base-version 0
python3 -I src/legfact/cli.py mart release --book /tmp/book --warehouse /tmp/wh \
  --receipt-batch 1 --cutoff 2026-02-10T00:00:00+08:00 \
  --roster roster.json --tariff tariff.json \
  --request-id close-2026-02-v1 --base-version 0

# 查看：账本、版本、某版明细与分录、按原归属的当版变动、全量分录历史
python3 -I src/legfact/cli.py mart show --book /tmp/book
python3 -I src/legfact/cli.py mart show --book /tmp/book --version 2
python3 -I src/legfact/cli.py mart show --book /tmp/book --journal

# 独立校验（不变量失败退出码 1）
python3 -I src/legfact/cli.py mart-verify --book /tmp/book
```

### 5.1 `plan` 输出

```json
{"mode": "plan",
 "book": {"airport": "SHA", "timezone": "Asia/Shanghai",
          "months": ["2026-01","2026-02"], "currency": "CNY"},
 "base_version": 1,
 "inputs": {"receipt_batch": 2, "cutoff": "2026-02-10T00:00:00+08:00",
            "roster_sha256": "...", "tariff_sha256": "..."},
 "summary": {
   "roster_count": 3, "priced_count": 3, "unpriced_count": 0,
   "out_of_range_count": 0, "total_minor": 2500, "currency": "CNY",
   "accounts": ["ACCT-A"],
   "balances": [{"account":"ACCT-A","month":"2026-01","amount_minor":1000},
                {"account":"ACCT-A","month":"2026-02","amount_minor":1500}],
   "unpriced_by_reason": {},
   "receipt_batch": 2, "cutoff": "2026-02-09T16:00:00Z",
   "scan_stats": {"sql_statements": 1, "candidate_act_arr_rows": 12,
                  "roster_roots": 3, "roots_with_actual_candidates": 3}},
 "projected_changes": {
   "reversals": [{"root_id":"R-A","posting_id":1,"account":"ACCT-A",
                  "month":"2026-01","amount_minor":-1000,
                  "reason":"account, month or amount changed"}],
   "positives": [{"root_id":"R-A","account":"ACCT-A","month":"2026-02",
                  "amount_minor":1000}],
   "unchanged_count": 0, "net_minor": 0, "entry_count": 6},
 "lines": [ ... 见 5.2 ... ]}
```

`summary.balances` 只是按「账户 × 月份」汇总的当版金额；明细行可按
`account`/`month`/`amount_minor` 回加成同一余额。

### 5.2 明细行（`lines[]`）

```json
{"seq": 1, "root_id": "R-A",
 "root": {"carrier":"CA","flight_no":"101","suffix":"",
          "service_date":"2026-01-01","departure":"PEK","arrival":"SHA"},
 "status": "priced",                 // priced | unpriced | out_of_range
 "reason": null,                     // 见 §3.3；out_of_range 时 month_outside_book
 "reason_detail": null,
 "account": "ACCT-A", "category": "JET", "weight_kg": 1000, "bulk_units": 1,
 "actual_arrival": {"value": "2026-02-01T00:20:00+08:00",
                    "utc": "2026-01-31T16:20:00Z", "local_date": "2026-02-01"},
 "month": "2026-02", "serial": 1,
 "tariff_entry": {"valid_from":"2026-01-01","valid_to":"2026-03-01",
                  "min_per_unit":1000},
 "serial_rung": {"upto_serial":1,"ten_thousandths":10000},
 "calculation": {"bulk_units":1,"rate_min_per_unit":1000,
                 "ten_thousandths":10000,"numerator":10000,
                 "denominator":10000,"amount_minor":1000},
 "amount_minor": 1000,
 "evidence": [{"event_id":"E-A-2","receipt_batch":2,"sender":"SHA-OCC",
               "source_revision":2,"effective_at":"2026-02-02T01:00:00+08:00",
               "raw_file":"msg_A_corr.xml",
               "xml_location":"/AIDXMessageEnvelope[1]/FlightLeg[1]/ActualTimeOnBlocks[1] @line 16:6",
               "line":16,"column":6}]}
```

未计价与范围外行的 `account`/`month`/`serial`/金额均为 `null`，但
`reason`/`reason_detail` 与（若有）`evidence` 保留。

### 5.3 `release` 输出

```json
{"mode": "release", "replayed": false,
 "version": {"version_id": 2, "request_id": "...", "base_version": 1,
             "receipt_batch": 2, "cutoff": "...", "cutoff_text": "...",
             "priced_count": 3, "unpriced_count": 0, "out_of_range_count": 0,
             "effective_total_minor": 2500, "entry_count": 6,
             "entry_total_minor": 0, "created_at": "..."},
 "effective": {"version_id": 2, "total_minor": 2500,
               "balances": [{"account":"ACCT-A","month":"2026-01",
                             "amount_minor":1000}, ...]},
 "entries": [{"entry_id": 4, "version_id": 2, "kind": "reversal",
              "posting_id": 1, "reverses_entry_id": 1, "root_id": "R-A",
              "account": "ACCT-A", "month": "2026-01",
              "amount_minor": -1000, "note": "..."}],
 "changes_by_attribution": [{"account":"ACCT-A","month":"2026-01",
                             "positive_count":1,"reversal_count":2,
                             "net_minor":-500}]}
```

- `entries`：本版追加的全部分录；`reversal` 的 `reverses_entry_id` 指向它
  冲销的那条正向记录。
- `changes_by_attribution`：按**原归属**账户/月份汇总的当版变动（财务视角）；
  新归属的补记也计入其目标归属。
- 完整历史分录：`mart show --book DIR --journal`（`journal`，按追加顺序）；
  单版分录用 `show --version N`。逐版累计有符号金额见 `mart-verify` 的
  `versions[].cumulative_signed_minor`。

## 6. 存储

账本是一个独立目录，只有 `landing.sqlite`（标准库 sqlite3）：

- `landing_book`：开账配置（单行）
- `landing_versions`：版本头 + 输入指纹 + 名册/协议原文 + sha256 + 汇总
- `landing_lines`：版本明细快照（含 `posting_id`，解释可复核）
- `landing_postings`：记账身份（开放/已冲销），唯一开放索引保证一版一根最多
  一条有效记账
- `landing_entries`：追加式分录（正向/冲销），只增不改

航段仓本身不新增任何表、不写回任何数据。

## 7. 演练与规模

- 可手算演练：`fixtures/landing/walkthrough/README.md`（A/B/C 跨月更正、
  净额为零仍须分录、账户改派连带重排），期望表
  `fixtures/landing/expected/walkthrough_expected.json`。
- 边界演练：`fixtures/landing/edge/`（同瞬间排序、时区换月、日期费率交界、
  缺失实际值 vs 零金额、改名与重复报文不重复计费、未知根/错机场/超截止/
  范围外）。
- 输入拒绝：`fixtures/landing/invalid/`。
- 规模演练：`python3 -I tests/scale_landing.py`（默认运行时生成 30,000 个运营根、
  120,000 条稀疏报文，执行整账预演/首版，再执行一次跨 2 月/3 月分区的改账；
  报告参与计价/未决条数、分区余额、进程峰值 RSS，单次作业须 < 384 MiB）。
  规模数据不提交仓库。

```bash
bash tests/run_all.sh          # 含原有航段仓门禁 + 本集市门禁
```
