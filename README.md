# MooCow Mini 调色工具 / Color Mixing Tool / 調色ツール

#### Unreleased（2026-07-17）- 科莱恩湿密度换算

- 接入供应商提供的14款色浆标称湿密度，并在网页与TXT导出中按 `湿体积mL = 湿重g / 密度g/mL` 自动换算。
- 供应商确认 `Colanyl DPP Red GD 131-CN = PR254`，项目主代码保持 `R254D`；确认 `Colanyl Orange D2R 100-CN = PO73`，并报送 C.I. 编号 `561170`（项目未独立核验该编号体系）。
- 原始表未标密度单位，项目按 `g/mL`（数值等同 `g/cm3`）解释并在页面逐项标记“单位假定”。密度不能反推出光谱、K/S、固含或非挥发体积，项目仍保持未物理校准状态。
- 规范化记录位于 [`data/supplier/colanyl-wet-density-2026-07-17.json`](data/supplier/colanyl-wet-density-2026-07-17.json)，原始聊天附件不进入仓库。
- 216色屏幕回归全部生成3个候选；稳定首选为 `216/216`，平均两遍模型 dE `4.1612`，失败等级 `60`。这是未校准模型内部指标，不是实体刮板精度。

#### v4.5.0（2026-07-12）- 遮盖稳定候选优先

- 默认推荐优先满足模型两遍遮盖率 `>=96%`、黑白底差 `<=3.0 dE`，再比较原有模型分数。
- 候选卡直接显示两遍遮盖率与黑白底差；候选集合、总量 `106g/L`、`0.5g/L` 网格和最多 `4` 种色浆均保持不变。
- 216 色回归中稳定首选由 `109` 提升到 `197`，模型失败等级由 `96` 降到 `73`。平均屏幕模型 dE 从 `3.937` 升至 `4.723`，原因是不再优先选择依赖黑底掩盖误差的方案；这不等于实体刮样精度已提高。

[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-brightgreen)](https://raydenpromen96-maker.github.io/moocow-color-tool/)
[![Version](https://img.shields.io/badge/Version-4.3.0-blue)](https://github.com/raydenpromen96-maker/moocow-color-tool)
[![Languages](https://img.shields.io/badge/Languages-中文%20%7C%20English%20%7C%20日本語-orange)](https://github.com/raydenpromen96-maker/moocow-color-tool)

## 🌟 功能特性 / Features / 機能

## 最新更新 / Latest Update / 最新更新

### v4.3.0 (2026-07-12) - 水性丙烯酸家族参考 / Waterborne Acrylic Family Reference

- **体系纠正**：移除 `MultipigmentPhantoms` 颜料-环氧 `mu_a / mu_s'` 数组，不再把环氧体系显示为本工具的家族曲线证据。
- **水性丙烯酸参考**：按数据页的公开分享许可说明，接入 GOLDEN Heavy Body 水性丙烯酸漆膜的反射率与单常数 K/S，覆盖 9 个精确 C.I. 家族。
- **严格边界**：数据为白色 Leneta 卡上约 6 mil 干膜，只作精确 C.I. 旁路参考，不参与候选排序，不以相似 C.I. 补缺，也不冒充当前科莱恩/Heubach CN 批次。
- **标准色度修正**：30 nm 光谱积分改用 D65 与 CIE 1931 2° 采样值，并修正旧版 580 nm x-bar 录入错误。
- **Orange D2R 边界**：取消未经证实的 PO13 映射；供应商确认 C.I. 身份前不再套用橙色家族曲线。
- **来源可追溯**：测量体系、数据包和授权边界记录在 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

#### v4.2.0 内部阶段 B（2026-07-11）- 多候选剂量网格 / Multi-candidate Dose Grid

- **三组模型候选**：Phase-2 通过本地 `recipe-search.js` 生成并选择三组支持集不同的模型候选；切换候选不会改写 RAL 预设。
- **临时未校准剂量政策**：搜索固定为总量 `106g/L`、`0.5g/L` 网格、活性色浆 `>=1.0g/L`、最多 `4` 种色浆，并在界面和导出中明确标注。
- **候选可读性**：候选区以紧凑、可换行的 14px 控件文字显示两遍模型 dE、活性色浆数、当前容量最小剂量及模型分歧/风险；切换语言或容量会保留并重绘候选。
- **整体界面清晰度**：正文与元数据最低提升到 12px，常用控件为 14px、主模块标题为 16px，并增强深色模型面板对比度和 320px 手机布局。
- **明确限制**：这是临时未校准的屏幕模型政策，不代表已校准设备、物理准确度、实测色差或生产通过；投产前仍必须制作实体刮样并进行仪器和目视确认。

#### v4.2.0 内部阶段 A（2026-07-11）- 确定性 ColorCore

- **统一颜色核心**：网页与 Node 测试共享本地 `ColorCore`，不再维护两套颜色数学。
- **Lab 优先模型输入**：存在 `manualLab` 时，显示 HEX 不再反向覆盖模型输入。
- **确定性评分**：移除 `spectral.js` 运行时分支，网络状态不再改变配方评分和等级。
- **稳定生成**：生成结果存入独立会话状态，不再改写 RAL 预设；重载预设会清除旧候选，同一输入重复生成结果一致。
- **最终配方重评分**：清理和取整后的实际输出配方会重新评分后再参与选优。
- **自动回归**：使用 Node 内置测试覆盖 CIEDE2000、RGB/Lab、K/S、数据优先级和关键页面守卫。
- **诚实表述**：界面统一使用“AI候选”“模型接近”“模型高风险”，不把内部结果称为实物通过。
- **安全导出**：复制和 TXT 导出保留模型限定与实体刮样验收警告，避免把模型值误作实测值。

### v3.0.0 (2026-07-01) - 黑底遮盖与专业工作台界面

本版本把原来的视觉配色工具升级成更接近真实调色流程的本地调色工作台：

- **黑底遮盖模拟**：默认按黑色基材、清漆体系和一遍/两遍施工评估，显示遮盖风险。
- **近似光谱混色**：在原有 Lab / Kubelka-Munk 近似模型基础上，加入公开颜料索引和近似光谱参考。
- **模型分歧提示**：显示 `模型分歧 dE` 与 `参考可信度`，避免把不可靠的屏幕结果误判成可直接生产。
- **更诚实的风险分级**：界面统一标注为“模型接近 / 模型边界 / 模型高风险”，不把内部模拟结果称作实物通过。
- **配方搜索优化**：减少无意义的小剂量色浆和过复杂配方，优先推荐更接近真实调色习惯的少色浆方案。
- **桌面 UI 重做**：改为左右两栏工作台，左侧选色与色浆调节，右侧显示配方、黑底模拟、图表和导出。
- **手机 UI 优化**：修复横向溢出，提升移动端首屏、输入框、按钮和滑块的可用性。

### 🌐 多语言支持 / Multi-language Support / 多言語対応
- **中文（简体）** - 默认语言，完整的中文界面
- **English** - Full English interface with professional terminology
- **日本語** - 完全な日本語インターフェース
- 实时语言切换，无需刷新页面
- 自动保存用户语言偏好设置

### 🎨 专业调色功能 / Professional Color Mixing / プロフェッショナル調色機能
- **216种RAL CLASSIC色码** - 使用带来源记录的千通彩 QTC 电子参考值；Lab 用于模型目标，HEX 用于屏幕显示
- **14种基础色浆** - 基于科莱恩色浆 Lab、批次强度、颜料索引和近似光谱参考
- **模型重量换算** - 支持50ML、100ML、500ML、1L、5L、20L多种容量
- **实时预览** - 颜色混合效果即时显示
- **AI候选配方** - 自动生成屏幕模型候选，并显示黑底估算、模型分歧和参考可信度

### 📊 数据可视化 / Data Visualization / データ可視化
- 颜料比例饼图显示
- 实时重量计算和比例分析
- 混合比例智能提示
- 黑底一遍/两遍、白底两遍和黑白基材差异模拟
- dE2000 色差、黑白基材差异、模型分歧和参考可信度提示

### 💾 导出功能 / Export Features / エクスポート機能
- **配方导出** - 详细的颜料重量清单
- **一键复制** - 快速分享配方信息
- **多格式支持** - TXT文件导出
- **QR码生成** - 移动端快速访问

## 🚀 在线使用 / Live Demo / ライブデモ

访问地址 / URL / アクセス: [https://raydenpromen96-maker.github.io/moocow-color-tool/](https://raydenpromen96-maker.github.io/moocow-color-tool/)

## 🔧 技术规格 / Technical Specifications / 技術仕様

### 颜料系统 / Pigment System / 顔料システム
当前模型使用的14种色浆 `manualLab` 参考值；这些值用于屏幕模型，不代表完整实测反射光谱：

家族参考来自 GOLDEN Heavy Body 水性丙烯酸漆膜的实测反射率与单常数 K/S。试样为白色 Leneta 卡上约 6 mil 干膜，白底会影响透明色；它们不是当前科莱恩/Heubach CN 批次，也不代表黑底完全遮盖。系统只接受精确 C.I. 对应并作旁路参考，不参与候选排序。现有 `REFERENCE_SPECTRA` 仍是屏幕模型近似。来源和边界见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

供应商标称湿密度只用于湿重/湿体积换算及现有未校准遮盖筛选。原表没有批次、单位、温度或测试方法；当前按 `g/mL` 解释，不作为当前批次实测密度或物理校准证据。R101V 与 Y42S 的原表品牌为 Colanyl，而目录登记为 Ecosperse，现按产品名暂时映射并在页面标为待确认。

| 代码 | 产品 | C.I. | 标称湿密度（按 g/mL 假定） |
|------|------|------|------------------|
| Y83S | Colanyl Yellow HR 130-CN | PY83 | 1.16 |
| Y74S | Colanyl Yellow 2GXD 130-CN | PY74 | 1.18 |
| B150S | Colanyl Blue A2R 131-CN | PB15:1 | 1.21 |
| B153S | Colanyl Blue B2G 132-CN | PB15:3 | 1.21 |
| R254D | Colanyl DPP Red GD 131-CN | PR254 | 1.19 |
| R101Y | Colanyl Oxide Red G 100-CN | PR101 | 2.01 |
| R101V | Ecosperse Oxide Red BA 100-CN | PR101 | 2.25 |
| Y42S | Ecosperse Oxide Yellow RA 100-CN | PY42 | 1.94 |
| 073 | Colanyl Orange D2R 100-CN | PO73（供应商报送编号 561170） | 1.07 |
| W064 | Colanyl White TQ 100-CN | PW6 | 1.83 |
| V23 | Colanyl Violet RL 131-CN | PV23 | 1.12 |
| G7 | Colanyl Green GG 131-CN | PG7 | 1.37 |
| R122S | Colanyl Pink E 100-CN | PR122 | 1.09 |
| BK7H | Colanyl Black N 131-CN | PBk7 | 1.27 |

| 代码 | 中文名称 | English Name | 日本語名 | L | A | B |
|------|----------|--------------|----------|---|---|---|
| Y83S | 金黄 | Golden Yellow | ゴールデンイエロー | 82.52 | 17.29 | 73.59 |
| Y74S | 中黄 | Medium Yellow | ミディアムイエロー | 87.77 | 0.8 | 72.81 |
| B150S | 宝蓝 | Royal Blue | ロイヤルブルー | 48.64 | -5.95 | -40.42 |
| B153S | 艳蓝 | Bright Blue | ブライトブルー | 59.36 | -16.97 | -38.95 |
| R254D | 大红 | Bright Red | ブライトレッド | 57.18 | 49.67 | 13.26 |
| R101Y | 铁红（黄相） | Iron Red (Yellow) | アイアンレッド（イエロー系） | 43.99 | 32.75 | 24.57 |
| R101V | 铁红（紫相） | Iron Red (Purple) | アイアンレッド（パープル系） | 40.09 | 28.89 | 18.77 |
| Y42S | 铁黄 | Iron Yellow | アイアンイエロー | 80.38 | 10.64 | 36.87 |
| 073 | 橙 | Orange | オレンジ | 70.43 | 45.73 | 27.33 |
| W064 | 白色 | White | ホワイト | 95.23 | -0.58 | 0.04 |
| V23 | 紫色 | Purple | パープル | 37.82 | 18.96 | -33.68 |
| G7 | 绿色 | Green | グリーン | 62.16 | -47.51 | 1.35 |
| R122S | 玫红 | Rose Red | ローズレッド | 55.54 | 45.65 | -16.47 |
| BK7H | 黑色 | Black | ブラック | 34.26 | -0.19 | -2.47 |

### 技术栈 / Tech Stack / 技術スタック
- **HTML5** - 现代化语义标记
- **Tailwind CSS** - 响应式设计框架
- **Chart.js** - 数据可视化图表
- **ColorCore** - 本地确定性颜色转换、CIEDE2000 与 K/S 数学核心，无运行时依赖
- **RecipeSearch** - 本地确定性剂量网格、最多4色约束与多候选选择模块
- **FamilySpectra** - 带来源哈希、精确 C.I. 匹配和 fail-closed 诊断的 GOLDEN 水性丙烯酸反射率/K/S旁路参考层
- **Vanilla JavaScript** - 原生 JS 业务逻辑，轻量静态页面
- **Font Awesome** - 图标库
- **GitHub Pages** - 静态网站托管

## 📱 移动端优化 / Mobile Optimization / モバイル最適化

- 响应式设计，适配手机和平板
- 触摸友好的滑块控件
- 优化的移动端交互体验
- 快速加载和流畅操作

## 🔄 版本历史 / Version History / バージョン履歴

### v4.3.0 (2026-07-12) - 水性丙烯酸家族参考
- 🔧 移除误接入的环氧体系 `mu_a / mu_s'` 数据
- ✨ 接入 9 个精确 C.I. 的 GOLDEN 水性丙烯酸反射率与单常数 K/S 参考
- 🔒 数据保持旁路诊断，不参与候选排序，不冒充当前 CN 批次
- ✅ 未覆盖或 C.I. 未确认的色浆继续 fail closed，不套用相似颜料曲线

### v3.0.0 (2026-07-01) - 黑底遮盖与专业工作台界面
- ✨ 加入黑底一遍/两遍遮盖模拟和 dE2000 风险评估
- ✨ 加入公开颜料索引、颜料含量、密度、参考可信度和近似光谱数据
- ✨ 加入模型分歧 dE 与参考可信度提示，避免屏幕结果被误认为实测结果
- 🔧 优化配方搜索，减少过复杂和小剂量堆叠配方
- 🎨 重做桌面 UI 为左右两栏调色工作台
- 📱 优化手机端布局，修复横向溢出并提升滑块操作体验
- ✅ 对216个 RAL CLASSIC 电子参考目标执行数据完整性与模型回归；该结果不代表实物色差或生产验收

### v2.0.0 (2024-10-11) - 多语言版本
- ✨ 新增中英日三语言切换功能
- 🔄 更新14种颜料的LAB色彩空间数据
- 🎨 优化用户界面和交互体验
- 📊 改进数据可视化效果
- 🌐 完整的国际化支持

### v1.x - 历史版本
- 基础调色功能
- RAL色码支持
- 配方导出功能

## 📄 许可证 / License / ライセンス

MIT License - 详见 [LICENSE](LICENSE) 文件

## 🤝 贡献 / Contributing / 貢献

欢迎提交 Issues 和 Pull Requests！

## 📞 联系方式 / Contact / 連絡先

- GitHub: [@raydenpromen96-maker](https://github.com/raydenpromen96-maker)
- Project: [moocow-color-tool](https://github.com/raydenpromen96-maker/moocow-color-tool)

## Physical pilot acquisition gate

The 45-card pilot operator pack is at `calibration/protocols/pilot-acquisition-v1/`, mirrored byte-for-byte at `data/calibration/acquisition/pilot-45-card-current/`. It is deliberately invalid until all 15 current-lot labels are materially verified. Freezing and verification require `--registry-evidence-root`; they bind each whole-file label's size and SHA-256 values and permit acquisition only. Generic `fit-km` accepts `synthetic_only` data only; physical `research_only` fitting remains reserved for the future receipt-gated `fit-pilot-selection` command.

Open-only measurement admission is documented at `calibration/protocols/open-measurement-admission-v1/`. Its two commands bind independently supplied open measurements to a verified acquisition receipt and reverify that binding; they create a non-promotable open-selection dataset only. They do not enable fitting, evaluation, ranking, release, promotion, or runtime activation, and every permission remains false.

The receipt-derived operator templates and instrument-neutral CSV assembler are documented at `calibration/protocols/open-measurement-pack-v1/`. They prepare the exact 36-card, 72-DFT, 6-bare, 216-coated, and 222-spectrum-identity open roster, then assemble completed operator files into the existing admission-input schema. This remains a laboratory data-collection path only; it grants no measured-accuracy or runtime authority.

The deterministic offline inverse recipe boundary is documented at `calibration/protocols/open-selection-recipe-solver-v1/`. It consumes a reverified open-selection K-M fit plus measured target spectra and current-lot dispenser evidence, re-predicts black/white cells after wet-mass quantization, and emits a laboratory-trial candidate only. It does not accept Lab/HEX targets, read sealed holdout data, enable browser/runtime ranking, or claim physical accuracy.

---

© 2024 MooCow Color Tools. All rights reserved.
