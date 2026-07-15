# 45-card pilot 采集包

本包故意保持无效。15 个组件都必须填写真实物理标签：`verified_physical_label` 状态、非占位 verification ID、带时区的 ISO 时间戳，以及 `evidence/labels/` 下的 whole-file locator。冻结和重验会读取每个标签文件并绑定大小、文件 SHA-256 与 record SHA-256。

DFT 必须满足 L < M < H；三个 acceptance 区间严格有序且不得接触或重叠。使用 `freeze-pilot-design --registry-evidence-root evidence` 和 `verify-pilot-design-receipt --registry-evidence-root evidence`。冻结仅允许采集；拟合、holdout 释放、物理排序和晋级始终为 false。

公开 roster 预注册的是计划 `target_NV` 采集输入，不是 holdout 实测结果。完成后的实际 NV、实测 DFT、反射光谱和 holdout 评估产物必须保存在本仓库之外，并由独立流程保管。
