# Acquisition package preparation (fail-closed)

Use `moocow-km-calibration prepare-acquisition-package --conversion-route <mass_solids_nonvolatile_density|wet_density_volume_solids> --output-dir <private-empty-root>` to create a private 15-material, 17-open-batch, and 3-sealed-batch template package.

The command creates deliberately invalid templates only. Every unresolved physical text value begins with `REQUIRED_`; every observed numeric value is JSON `null`. It creates no label, property, or weighing evidence under a live `evidence/` directory and emits no receipt, rank, DFT, spectrum, signature, custody metadata, or permission.

Only the `shared-template` and `open-template` subtrees are public-safe. Keep the private subtree separate, complete all placeholders from observed physical evidence and frozen mappings, and then follow the existing preflight sequence. The generated package is not a preflight approval and never authorizes measurement admission, fitting, release, ranking, or promotion.

# 采集预检操作合同 v1（软件边界已独立验证）

> 状态：**independently software-verified / not physically activated**。五个预检命令已注册；临时确定性 fixture 的单元、集成和 CLI 测试以及第二轮独立复核均已通过（0 个阻断项）。真实物料、称量和 custody 证据仍未提供。本文件不是可放行的采集包，也不授权光谱导入、拟合、holdout 释放、物理排序或晋级。

## 目的与前置条件

本阶段只把已冻结的 45-card pilot 设计连接到采集前的可审计事实：15 个当前批次物料、实际称量、实际非挥发体积（NV）分数、固定卡片/读数槽位，以及密封 holdout 的最小公开承诺。它在任何光谱或实际 DFT 导入之前停止。

开始前必须已有可验证的 frozen pilot-design receipt 和 four-card diagnostic prerequisite。两者不是本阶段的替代品；缺失、变更或与当前批次不匹配时应失败。

## 三根目录与隔离边界

```text
acquisition/
  shared/             # 恰好 15 个已验证的当前批次物料记录；不含光谱
  open/               # 恰好 17 个公式批次：15 train + 2 validation
  sealed-holdout/     # 恰好 3 个 holdout 公式批次；绝不可成为 open 命令的参数
```

`shared/`、`open/`、`sealed-holdout/` 是三个独立根。`sealed-holdout/` 的原始批次、称量、NV、DFT、光谱、路径、哈希和测量标识不得进入 `open/`，也不得进入任何公开或最终 acquisition-preflight receipt。

密封侧只可向公共流程提供 `holdout-custody-commitment.json` 中的最小承诺：设计承诺哈希、用于私下 cross-split 检查的 open receipt 哈希、密封批次清单承诺哈希、公开计数 `3/3/9/54`、custody identity/key fingerprint 与 signature metadata，以及状态和六个权限字段。它不得含 holdout formula/batch/card ID、raw locator、实际湿质量、NV 值/向量、物性值、DFT、reflectance 或 measurement ID。

`commit-holdout-custody` 必须读取已经验证的 open receipt，并在密封进程内比较 open 与 sealed 的 `weighing_event_id` 集合。发现交集时返回 `CROSS_SPLIT_ID`，但公开 commitment 只记录 open receipt 的整体 SHA-256，不公开任何 sealed event ID 或 event hash。

## 固定身份与计数

15 列 basis 的顺序固定如下；不得按词典序排序、重排，或把 `073` 数字化。`073` 是字符串 key，只映射到 `colorant-073`。

```text
[base, Y83S, Y74S, B150S, B153S, R254D, R101Y, R101V,
 Y42S, 073, W064, V23, G7, R122S, BK7H]
```

| 区域 | Families / batches | Cards | Primary reading slots |
|---|---:|---:|---:|
| `shared/` | 15 current-lot materials | — | — |
| `open/` train | 15 / 15 | 30 | 180 |
| `open/` validation | 2 / 2 | 6 | 36 |
| `open/` 总计 | **17 / 17** | **36** | **216** |
| `sealed-holdout/` | **3 / 3** | **9** | **54** |

每张卡必须保留两个不同 backing：`black` 与 `white`；每个 backing 下恰好为 `POS01`、`POS02`、`POS03`。这里仅生成 identity skeleton，不得写入实测 DFT、spectrum、reflectance、measurement ID 或 raw-reading evidence。

## 物料、实际称量与 NV 合同

`shared/` 中每个物料必须有 `verified_physical_label`、当前物理批次、匹配的 canonical property record 及其 SHA-256 sidecar。任何 template、catalog-only、synthetic、inferred、null 或 placeholder 值都不是可接受证据。

每个正贡献组件只能选择一条转换路线；**同一个 formula batch 的所有正贡献组件必须使用同一条路线**。不得在一个批次混用路线，也不得从别的物理批次借用属性。

| `conversion_route` | 只允许的属性字段 | 计算 |
|---|---|---|
| `mass_solids_nonvolatile_density` | `nonvolatile_mass_fraction`、`nonvolatile_density_g_ml` | `V_NV = wet_mass_g * nonvolatile_mass_fraction / nonvolatile_density_g_ml` |
| `wet_density_volume_solids` | `wet_density_g_ml`、`component_nonvolatile_volume_fraction` | `V_NV = (wet_mass_g / wet_density_g_ml) * component_nonvolatile_volume_fraction` |

随后按 `x_j = V_NV,j / sum(V_NV,k)` 重算实际 NV fraction。湿质量本身不得代替 `x_j`；冻结的 target-NV 仅作 provenance，不得参加 rank 运算。

### 多次添加（multiple additions）

同一 formula batch 内，同一 component 和同一 physical lot 可以有多个实际添加事件，前提是每个 `weighing_event_id` 不同、为正克数，并且 formula/batch/component/lot ID 均匹配。先累加这些事件的 `wet_mass_g`，再进行一次路线转换和 NV 计算。

这不等同于允许重复事件：同一个 `weighing_event_id` 出现两次必须拒绝，且事件不得跨 component、family、batch 或 split 复用。generated weighing plan 不是 `actual_weighing_observation`，不能作为实际称量证据。

## Actual-NV rank gate

rank 只使用 15 条 train basis rows 和上述固定 15 列顺序，形成 `15 x 15` actual-NV matrix；validation 和 holdout 不得进入此计算。实现合同为 IEEE-754 binary64：

```text
eps = 2^-52
tolerance = max(15, 15) * eps * sigma_max
numerical_rank = count(sigma_i > tolerance)
```

硬门槛只有 `numerical_rank == 15`。receipt 必须记录 SVD singular values、tolerance、`float64.hex()` matrix entries、rank、condition number 及其有限性；**没有 condition-number threshold，也不得把 condition number 作为通过/失败或物理质量阈值。**

## 已实现命令序列（仍需真实证据）

以下 command name、flag name、状态转换和产物名已在 CLI 中实现并通过软件边界独立复核。临时 fixture 与独立攻击探针通过不等于真实批次通过；实际执行前仍须准备完整物理证据。

| 顺序 | Command 与必需字段 | 预期状态 / 产物 |
|---:|---|---|
| 1 | `preflight-pilot-materials`：`--pilot-design-receipt --design --registry --registry-evidence-root --diagnostic-receipt --diagnostic-evidence-root --shared-root --output-dir` | `DESIGN_RECEIPT_VERIFIED -> COMMON_MATERIALS_VERIFIED`；`common-material-receipt.json` 与 `.sha256` |
| 2 | `preflight-open-batches`：`--materials-receipt --open-batch-root --open-evidence-root --output-dir` | `COMMON_MATERIALS_VERIFIED -> OPEN_BATCH_PREFLIGHT_VERIFIED`；`actual-nv-rank-receipt.json`、`open-batch-preflight-receipt.json` 与各自 `.sha256` |
| 3 | `commit-holdout-custody`：`--materials-receipt --open-batch-receipt --sealed-holdout-batch-root --sealed-evidence-root --custody-identity --custody-key-fingerprint --signature-metadata --output-dir` | 私下检查 open/sealed event ID 不相交后进入 `HOLDOUT_CUSTODY_COMMITTED`；只写入密封输出根的 `holdout-custody-commitment.json` 与 `.sha256` |
| 4 | `assemble-acquisition-preflight`：`--open-batch-receipt --holdout-custody-commitment --output-dir` | `OPEN_BATCH_PREFLIGHT_VERIFIED + HOLDOUT_CUSTODY_COMMITTED -> ACQUISITION_PREFLIGHT_READY`；`acquisition-preflight-receipt.json` 与 `.sha256` |
| 5 | `verify-acquisition-preflight`：`--receipt --shared-root --open-root` | 状态保持 `ACQUISITION_PREFLIGHT_READY`；`acquisition_preflight_verified`；只读、无 output directory、无 mutation |

`admit-open-measurements`、`OPEN_MEASUREMENTS_ADMITTED`、candidate freeze、fitting、evaluation、holdout release、physical ranking 和 promotion 不属于本阶段。把这些行为或实际 DFT/spectrum 字段传入上述 preflight 命令，预期应以 `PREFLIGHT_SCOPE` 失败。

对类型校验失败，合同要求 CLI return code 为 `2`、stdout 为空、stderr 为 `ERROR: [CODE] ...`，且不发布输出。未知或缺失参数保持 `argparse` 的 `SystemExit(2)`/usage stderr 行为。

## 六个权限必须全部为 `false`

每一个本阶段 receipt 和成功 CLI response 都必须包含下列完整 vector，值全部为 `false`。`receipt_verified: true` 或 status/state 是验证状态，不是权限。

```yaml
pilot_acquisition_permitted: false
open_admission_permitted: false
model_fitting_permitted: false
holdout_release_permitted: false
physical_ranking_enabled: false
promotion_permitted: false
```

本规则以 acceptance-test specification 为准，并覆盖任何早期架构文字中把 `open_admission_permitted` 设为 `true` 的建议。

## Holdout 泄漏防线

- open 命令不能接受 sealed root；`--sealed-holdout-root` 不是 open 命令的记录参数。
- 以 sealed root 充当 declared open root 必须失败，不能“兼容处理”。
- public/open/final receipt 递归检查时不得有 holdout raw locator/hash、actual mass、actual NV volume/fraction/vector、property/weighing evidence locator、DFT、reflectance、measurement ID、spectrum/raw-reading source 或 sealed-root path。
- final assembly 只绑定两个 receipt hash 和公开基数；re-verification 只重开其声明的 copied `shared/` 与 `open/` roots。

## 当前项目仍缺少的真实证据与实现

截至本草案作者检查时，仓库内尚无本协议的可执行实物证据：

- 没有 `data/calibration/acquisition/shared/`、`open/` 或 `sealed-holdout/` 根；
- 没有这五类 acquisition-preflight receipts 或其 sidecars；
- 现有 pilot/four-card registry 仍含 `REQUIRED_` physical-label/lot/property 占位信息；未找到 `verified_physical_label` 记录；
- 未找到 `record_kind: actual_weighing_observation` 的实际称量记录；
- 没有 15 个 current-lot material records、17 个 open batch records、3 个 sealed batch records、actual-NV rank receipt 或独立 custody/signature metadata；
- 五个 CLI 命令已注册并通过临时 fixture 测试，但尚未使用本项目真实批次证据执行；
- actual DFT 与 measured spectra 也尚未出现，但它们本来就属于后续 open-admission slice，不能以本阶段补齐。

这些是当前项目的证据/实现缺口，不应由虚构物料值、哈希、签名、spectra、DFT 或 receipt 填补。

## 明确不在威胁模型内

本合同防止错误输入、stale/corrupt/reused local artifacts、意外泄漏与普通输出冲突。它**不**声称能防御：

1. 能同时改写 roots、receipts、sidecars 或 custody attestations 的 hostile administrator；
2. 在 file-handle 层与 trusted reader/publisher 竞争的 concurrent operating-system writer。

SHA-256 提供完整性绑定，不证明独立 custody。上述攻击者需要独立的 custody/signature 边界和 handle-safe trusted-read 设计；不要以 sleep/retry/race 测试暗示本地合同未提供的保证。

## 何时可以物理激活

软件边界的独立代码验证已通过，但只有真实 current-lot evidence 也满足以下全部事项后，才可把本阶段标记为 physically activated：receipt/sidecar 与 portable copied-root re-verification 均通过；rank 为 15；六个权限仍全 false；公开输出无 holdout raw 数据；open/sealed event ID 不相交；且 focused 与 full regression suites 保持通过。更新本文件本身不构成任何 release、admission、fitting、ranking 或 promotion 授权。
