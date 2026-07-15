# 物理刮涂与光谱采集协议 v1

## 0. 状态与边界

**状态：实验室采集协议 / 导入交接；不是当前物理准确度的证明。** 本协议只定义如何取得可审计的实测有限膜厚反射率数据，以供 `calibration/km_calibration` 的二常数 Kubelka–Munk 工作流研究使用。现有合成数据、catalog 中的手工 Lab/HEX/QTC、色强、遮盖力或任何公开类比光谱，均不得作为本协议的校准目标或通过证据。

在独立、真实的混合族 holdout 上取得改进并完成审核之前：

- `physical_ranking_enabled` 必须保持 `false`；
- 不得把模型接入生产配方排序；
- 不得将 106 g/L 的现有 screen 限制解释成颜料物理浓度、非挥发体积分数或校准真值。

配套文件：

- `current-batch-component-registry-v1.json`：当前本地 catalog 的 14 个组分及批次抄录；
- `measurement-record-template-v1.json`：字段兼容、但故意不能通过校验的单条记录模板；
- 实际导入时须另建带 SHA-256 的 `manifest.json` 和真实测量 source 文件，不能直接导入模板。

## 1. 开工前的硬性空白项

下列项目当前未知，必须从实际桶/罐、CoA 或本次测量填写；禁止估计、沿用 UI 数值或从相似产品推断：

| 项目 | 当前状态 | 不填的后果 |
|---|---|---|
| 水性透明基料的准确产品名、供应商与批号 | `REQUIRED_NOT_YET_KNOWN` | 不能创建唯一的 `role=base` manifest 组分 |
| 基料湿密度、非挥发质量分数与非挥发密度，或直接测得的非挥发体积分数 | `REQUIRED_NOT_MEASURED` | 不能把湿重换算为非挥发体积分数 |
| 固化程序、起止时间、固化温度和 RH | `REQUIRED_NOT_MEASURED` | 不能锁定光谱条件 |
| 施涂方式、湿膜目标/刮刀间隙 | `REQUIRED_NOT_MEASURED` | 不能解释膜厚差异 |
| 每张卡的实测 DFT（含测法与多点读数） | `REQUIRED_NOT_MEASURED` | 不能拟合有限膜厚模型 |

每个使用的色浆也须在投料前核对实体标签、实际湿密度及本批非挥发信息。`src/paint-catalog.js` 的 14 个代码、产品名与 batch 仅为当前本地登记证据；不能替代实体标签、CoA 或本次测量。

## 2. 锁定条件（一次实验一个条件哈希）

先测未涂覆的黑/白不透明卡区域，保存与样本同一波长格的反射率；再开始刮涂。一次可导入数据集内，以下项目必须完全一致并写入 `manifest.locked_conditions`、每条记录的 `conditions` 和其 canonical `conditions_sha256`：

1. 分光仪 ID、型号、软件版本、校准标准/校准时间；
2. 几何（例如 d/8 或 45/0）、镜面分量状态（SCI/SCE 或等效设置）和 UV 设置；
3. 实测波长格：起止波长、间隔、单位和完整 numeric array；不得使用手机颜色、HEX 或 QTC 值；
4. 黑/白不透明卡的制造商、产品、批号、保存状态及所录的背景光谱；
5. 基料批号，及每个色浆代码/产品名/实体批号；
6. 固化程序、时间窗、温度与相对湿度；
7. 施涂方法、刮刀/喷涂设定、湿膜目标和操作者；
8. 每张卡的实测 DFT、测量方法、位置、均值和离散度。

出现不同卡批、不同几何/UV/镜面设置、不同基料/色浆批、不同固化条件或不同施涂方法时，不得混入同一个条件哈希。应先作为独立实验批；若将来需要合并，先修改/扩展 schema 并复核。

## 3. 非挥发体积分数：从湿重到导入浓度

`components[].nonvolatile_volume_fraction` 不是湿重百分比、不是 catalog 的 `solidContent`，更不是 106 g/L screen 数值。对每个组分 `j`，实际称取湿重为 `m_wet,j`，导入浓度为：

\[
x_j = \frac{V_{NV,j}}{\sum_k V_{NV,k}}, \quad \sum_j x_j = 1
\]

任选一条由真实本批数据支持的换算路线，并在原始称量记录中保留原始量及测法：

1. 已知非挥发**质量分数** `w_NV,j` 和非挥发物密度 `rho_NV,j` 时：

   \[
   V_{NV,j} = \frac{m_{wet,j} w_{NV,j}}{rho_{NV,j}}
   \]

2. 已知湿密度 `rho_wet,j` 与直接测得的非挥发**体积分数** `phi_NV,j` 时：

   \[
   V_{NV,j} = \frac{m_{wet,j}}{rho_{wet,j}} phi_{NV,j}
   \]

若要从设计的 `x_j` 反求湿重，可先选一个目标总非挥发体积 `V*`，再用 `m_wet,j = x_j V* rho_NV,j / w_NV,j`（路线 1）或 `m_wet,j = x_j V* rho_wet,j / phi_NV,j`（路线 2）。所有分母均须来自真实本批测量；不允许用未知基料的假设值补齐。

**仅用于演示的合成例子，绝不可写入本项目登记表：** 假定 A 的 `m=80.0 g`、`rho_wet=1.05 g/mL`、`phi_NV=0.40`，C 的 `m=20.0 g`、`rho_wet=1.20 g/mL`、`phi_NV=0.60`。则 `V_NV,A=80/1.05*0.40=30.476 mL`，`V_NV,C=20/1.20*0.60=10.000 mL`；导入值为 `x_A=0.7528`、`x_C=0.2472`。这些数字是公式示例，不是水性透明基料或任何色浆的物性。

## 4. 阶段 A：4 张诊断卡（只验证流程）

目的：验证基料、W064、混合/施涂、固化、DFT 测量、黑白区域读取及重复定位的工作流。**它不能校准全部 14 个色浆，不能作为物理模型推广证据。**

在填写第 1 节空白并预先锁定数值 DFT 前，使用两档 DFT：`DFT-L` 与 `DFT-H`。两档的目标值、湿膜设定和每卡实测 DFT 均为必填真实值，本协议不预设微米数。

| 卡 ID | 公式族 | 非挥发体积配方 | DFT | 黑/白区域 |
|---|---|---|---|---|
| `CARD-DX-BASE-DFT-L-001` | `FAM-DX-BASE` | base = 1.0000 | `DFT-L` | 同一不透明卡的黑、白区域 |
| `CARD-DX-W064-DFT-L-001` | `FAM-DX-W064` | base = 0.8500；W064 = 0.1500 | `DFT-L` | 同上 |
| `CARD-DX-BASE-DFT-H-001` | `FAM-DX-BASE` | base = 1.0000 | `DFT-H` | 同上 |
| `CARD-DX-W064-DFT-H-001` | `FAM-DX-W064` | base = 0.8500；W064 = 0.1500 | `DFT-H` | 同上 |

每张卡在黑、白区域各取得至少 3 个重新定位读数（`POS01`–`POS03`）。因此诊断阶段至少有 `4 cards × 2 backings × 3 repeats = 24` 条原始光谱记录。诊断必须先满足数据完整性门槛；若失败，修复流程并重新做诊断，而不是直接进入 45 卡试验。

## 5. 阶段 B：45 张可识别 pilot

### 5.1 固定结构与数量

| Split | 配方族 | DFT 档 | 卡数 | 目的 |
|---|---:|---:|---:|---|
| train | 15 个 basis 族：base-only + 每个 14 个色浆各一个 base+single 族 | `DFT-L`, `DFT-H` | 15 × 2 = 30 | 识别基料和每个单色浆的有限膜厚响应 |
| validation | 2 个预先登记的 mixed 族 | `DFT-L`, `DFT-M`, `DFT-H` | 2 × 3 = 6 | 选模/设定正则或工艺决定，不能当 holdout |
| holdout | 3 个预先登记、从未用于拟合或选择的 mixed 族 | `DFT-L`, `DFT-M`, `DFT-H` | 3 × 3 = 9 | 独立物理提升判定 |
| **总计** | **20 个公式族** |  | **45** |  |

`DFT-L/M/H` 必须在诊断后、pilot 前一次性登记真实数值和可接受实测范围；不得按照测得颜色或模型误差事后挑选卡。所有 DFT 都须导入每条光谱记录的 `dft_um` 实测值，不能只用标签。

### 5.2 训练 basis 族与满秩检查

按列 `[base, Y83S, Y74S, B150S, B153S, R254D, R101Y, R101V, Y42S, 073, W064, V23, G7, R122S, BK7H]` 建立 15 × 15 的训练浓度矩阵：

- `FAM-TR-BASIS-BASE`：`[1, 0, ..., 0]`；
- 对每个 14 个 catalog 色浆 `i`：`FAM-TR-BASIS-i` 为 base = `0.8500`、色浆 `i` = `0.1500`、其余 = `0`。

这 15 个配方均为目标**非挥发体积分数**。矩阵的颜色子块是 `0.15 I`，故 `rank(X)=15` 且 `det(X)=0.15^14`；两档 DFT 是膜厚信息，不会替代浓度满秩。投料前必须用将要导入的精确浮点 `x_j` 重算并记录 `rank=15`，并记录 condition number。任何改配、漏称、零浓度或用湿重替换 `x_j` 都会使这项保证失效，必须在制卡前更正。

### 5.3 预注册 mixed 族

下表是独立公式族，不与 train 共享 `formula_family_id`、`formula_id`、`formula_batch_id`、`card_id`、`sample_group_id` 或任何 repeat。数值仍是目标非挥发体积分数；实际湿重必须按第 3 节用当前物性回算。

| Split | 公式族 | 非挥发体积配方 |
|---|---|---|
| validation | `FAM-VA-MIX-01` | base 0.7000；Y83S 0.1500；B150S 0.1500 |
| validation | `FAM-VA-MIX-02` | base 0.7000；R254D 0.1500；G7 0.1500 |
| holdout | `FAM-HO-MIX-01` | base 0.7000；Y74S 0.1500；R122S 0.1500 |
| holdout | `FAM-HO-MIX-02` | base 0.7000；R101Y 0.1500；B153S 0.1500 |
| holdout | `FAM-HO-MIX-03` | base 0.7000；BK7H 0.1000；V23 0.1000；Y42S 0.1000 |

同一公式族仅可在其分配的卡/DFT/黑白区域/重定位重复中出现；holdout 的三个混合族在特征拟合、超参选择、失效修复、阈值挑选和人工目视挑选期间均不可查看误差结果。色浆本身会出现在 train 的单色 basis 中，这是设计需要；被隔离的是完整**混合公式族**及其制备批、实体卡、sample group 和重复记录。

## 6. 采集、命名和反泄漏规则

一张实际不透明卡必须产出两组互不合并的光谱：黑区域和白区域。每个区域至少 3 次重新定位（移开、重新放置、重新对准；记录位置/方向）并保留原始光谱，不可先平均。pilot 的最低记录数为 `45 × 2 × 3 = 270`；额外重测只能新增记录，不能覆盖原始记录。

使用以下 ID 语法：

```text
formula_family_id = FAM-TR-BASIS-Y83S | FAM-VA-MIX-01 | FAM-HO-MIX-01
formula_id        = FORM-TR-BASIS-Y83S
formula_batch_id  = FB-TR-BASIS-Y83S-YYYYMMDD-01      # 一个 batch 只能属于一个 family
card_id           = CARD-TR-BASIS-Y83S-DFT-L-001      # 一张实体卡只能属于一个 family
sample_group_id   = SG-CARD-TR-BASIS-Y83S-DFT-L-001-BLACK
repeat_id          = POS01 | POS02 | POS03
measurement_id     = MSR-SG-CARD-TR-BASIS-Y83S-DFT-L-001-BLACK-POS01
```

黑、白区域的 `backing` 必须分别为 `black`、`white`；`sample_group_id` 也必须不同。不要复用 formula batch、card、sample group 或 repeat 到另一个 split。schema 会审计 `formula_family_id` 对 formula、batch、card 与 sample group 的一对一归属；实验日志还应记录称量表、操作者、卡照片/扫描与仪器原始文件 hash。

## 7. 导入顺序

1. 填完基料空白、实体验证 14 个色浆标签，并冻结 `current-batch-component-registry-v1.json` 的实际副本；批次不符即新建登记版本。
2. 完成诊断，建立仪器/工艺重复性基线，并一次性冻结 DFT-L/M/H、卡批、波长格和 locked conditions。
3. 按第 3 节把每个原始湿重转换为 `nonvolatile_volume_fraction`；保存原始称量、密度/非挥发测试证据和换算表。
4. 从模板复制为真实 source JSON：替换所有 `TEMPLATE_NOT_MEASURED`、空数组、`null` 与未计算 hash；每条 record 必须含 schema 所需的 14 个记录字段。
5. 建立 dataset `manifest.json`：一个真实 base、已声明组分/批次、实测黑白 backing arrays、有效 Saunderson 项、source 文件 SHA-256、以及互斥的 train/validation/holdout family 列表。初始 `dataset_status` 保持 `research_only`、`physical_ranking_enabled` 保持 `false`。
6. 执行 schema 导入和 split audit。v1 schema 会要求统一 numeric 波长格、反射率在 [0,1]、每个配方的 `x_j` 和为 1、条件 hash 一致、以及 family 泄漏为零。
7. 只在完整性 gate 通过后拟合；先保持研究结果与生产运行时隔离，再做独立 holdout 评估。

## 8. 接受门槛与禁止激活条件

### 8.1 Gate 0：数据完整性先行

必须先全部通过，才允许计算任何模型优劣：45 张卡齐全；每卡有黑/白记录和至少 3 个重新定位重复；所有 DFT 实测且为正；基料/色浆/卡的批次可追溯；数组与锁定波长格一致；反射率有效；湿重换算能重算到 `sum(x)=1`；conditions hash、source hash、split audit 均通过；没有模板/手机/HEX/QTC/类比光谱字段。

诊断和 pilot 的仪器、刮涂、DFT、固化重复性必须形成可复查的基线。数值容差今天不虚构：它们由诊断中的同位重测、重新定位重复、卡间变异及基线模型表现共同确定并预注册。

### 8.2 Gate 1：未触碰 mixed holdout 的真实提升

仅在三组 `FAM-HO-MIX-*` 的全部 DFT、黑白 backing 和原始重复上，比较基线与候选。至少报告：

- `dE00` 的 median 与 P90：D65/10 为主条件，外加至少一种预先锁定的替代照明/10°观察者（建议 A/10；可另加 F11/10）；
- 同一实测波长格上的 spectral RMSE：按 DFT 和 backing 分层，并给出整体与各族摘要；
- 是否出现任一基材、DFT、照明或公式族退化。

具体的“通过”阈值在取得上述基线和过程重复性前不得编造。只有真实、独立 holdout 显示候选在 median/P90 dE00 与 spectral RMSE 上有可审计改善，并且不恶化替代照明或黑/白 backing 失败，才可提交后续生产激活评审。合成 pass、train/validation 改善、单卡好看或人工调色都不是生产激活证据。

## 9. 结束声明

本协议提供真实世界数据收集及导入的最小可审计路径；它本身不证明当前模型、catalog 颜色或任何配方已经具有物理准确性。真实 holdout 改进、独立复核和显式生产审批之前，物理排序功能保持关闭。
