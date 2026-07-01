# MooCow Mini 调色工具 / Color Mixing Tool / 調色ツール

[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-brightgreen)](https://raydenpromen96-maker.github.io/moocow-color-tool/)
[![Version](https://img.shields.io/badge/Version-3.0.0-blue)](https://github.com/raydenpromen96-maker/moocow-color-tool)
[![Languages](https://img.shields.io/badge/Languages-中文%20%7C%20English%20%7C%20日本語-orange)](https://github.com/raydenpromen96-maker/moocow-color-tool)

## 🌟 功能特性 / Features / 機能

## 最新更新 / Latest Update / 最新更新

### v3.0.0 (2026-07-01) - 黑底遮盖与专业工作台界面

本版本把原来的视觉配色工具升级成更接近真实调色流程的本地调色工作台：

- **黑底遮盖模拟**：默认按黑色基材、清漆体系和一遍/两遍施工评估，显示遮盖风险。
- **近似光谱混色**：在原有 Lab / Kubelka-Munk 近似模型基础上，加入公开颜料索引和近似光谱参考。
- **模型分歧提示**：显示 `模型分歧 dE` 与 `参考可信度`，避免把不可靠的屏幕结果误判成可直接生产。
- **更真实的风险分级**：真正高色差保持“风险较高”；低色差但模型分歧大的颜色标为“接近可用”，并提示必须刮样确认。
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
- **191种RAL色码** - 覆盖常用 RAL 色卡
- **14种基础色浆** - 基于科莱恩色浆 Lab、批次强度、颜料索引和近似光谱参考
- **精确重量计算** - 支持500ML、1L、5L、20L多种容量
- **实时预览** - 颜色混合效果即时显示
- **智能配方** - 自动生成颜料配比，并显示黑底遮盖、模型分歧和参考可信度

### 📊 数据可视化 / Data Visualization / データ可視化
- 颜料比例饼图显示
- 实时重量计算和比例分析
- 混合比例智能提示
- 黑底一遍/两遍模拟
- dE2000 色差、基材影响、模型分歧和参考可信度提示

### 💾 导出功能 / Export Features / エクスポート機能
- **配方导出** - 详细的颜料重量清单
- **一键复制** - 快速分享配方信息
- **多格式支持** - TXT文件导出
- **QR码生成** - 移动端快速访问

## 🚀 在线使用 / Live Demo / ライブデモ

访问地址 / URL / アクセス: [https://raydenpromen96-maker.github.io/moocow-color-tool/](https://raydenpromen96-maker.github.io/moocow-color-tool/)

## 🔧 技术规格 / Technical Specifications / 技術仕様

### 颜料系统 / Pigment System / 顔料システム
基于图像数据更新的14种专业颜料LAB色彩空间参数：

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
- **spectral.js** - 浏览器端辅助光谱混色参考
- **Vanilla JavaScript** - 原生 JS 业务逻辑，轻量静态页面
- **Font Awesome** - 图标库
- **GitHub Pages** - 静态网站托管

## 📱 移动端优化 / Mobile Optimization / モバイル最適化

- 响应式设计，完美适配手机和平板
- 触摸友好的滑块控件
- 优化的移动端交互体验
- 快速加载和流畅操作

## 🔄 版本历史 / Version History / バージョン履歴

### v3.0.0 (2026-07-01) - 黑底遮盖与专业工作台界面
- ✨ 加入黑底一遍/两遍遮盖模拟和 dE2000 风险评估
- ✨ 加入公开颜料索引、颜料含量、密度、参考可信度和近似光谱数据
- ✨ 加入模型分歧 dE 与参考可信度提示，避免屏幕结果被误认为实测结果
- 🔧 优化配方搜索，减少过复杂和小剂量堆叠配方
- 🎨 重做桌面 UI 为左右两栏调色工作台
- 📱 优化手机端布局，修复横向溢出并提升滑块操作体验
- ✅ 批量回归测试 191 个 RAL 色：风险项集中在红色、深紫红、荧光黄等真实色域薄弱区域

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

---

© 2024 MooCow Color Tools. All rights reserved.
