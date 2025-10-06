# 📋 GitHub 逐步部署指南

## 🎯 第1步：创建GitHub仓库

1. 访问 [GitHub](https://github.com) 并登录
2. 点击右上角的 **"+"** 按钮 → 选择 **"New repository"**
3. 填写仓库信息：
   - **Repository name**: `moocow-color-tool`
   - **Description**: `Professional RAL color mixing tool with Delta E calculation`
   - **Visibility**: 选择 **Public**
   - **❌ 不要勾选** "Add a README file"
4. 点击 **"Create repository"**

## 📁 第2步：上传主要文件

### 2.1 上传核心文件
1. 在新仓库页面，点击 **"uploading an existing file"**
2. 拖拽以下文件到页面：
   - `index.html`
   - `README.md`
   - `LICENSE`
   - `DEPLOYMENT.md`
3. 在底部提交信息框输入：`Add main files`
4. 点击 **"Commit changes"**

### 2.2 创建.github文件夹
1. 在仓库主页，点击 **"Create new file"**
2. 在文件名输入框中输入：`.github/workflows/deploy.yml`
   - 注意：输入 `.github/` 时会自动创建文件夹
   - 继续输入 `workflows/` 会创建子文件夹
   - 最后输入 `deploy.yml`
3. 在文件内容区域粘贴以下内容：

```yaml
name: Deploy to GitHub Pages

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: "pages"
  cancel-in-progress: false

jobs:
  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      
      - name: Setup Pages
        uses: actions/configure-pages@v4
      
      - name: Upload artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: '.'
      
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
```

4. 在底部提交信息输入：`Add GitHub Actions workflow`
5. 点击 **"Commit changes"**

## ⚙️ 第3步：启用GitHub Pages

1. 在仓库页面，点击 **"Settings"** 标签
2. 在左侧菜单中滚动找到 **"Pages"**
3. 在 **"Source"** 部分：
   - 选择 **"Deploy from a branch"**
   - **Branch**: 选择 **"main"**
   - **Folder**: 选择 **"/ (root)"**
4. 点击 **"Save"**
5. 页面会显示：**"Your site is live at https://raydenpromen96-maker.github.io/moocow-color-tool/"**

## 🔍 第4步：验证部署

### 4.1 检查Actions状态
1. 在仓库页面点击 **"Actions"** 标签
2. 应该看到一个正在运行或已完成的工作流
3. 如果显示绿色✅，说明部署成功

### 4.2 访问网站
1. 等待2-5分钟让部署完成
2. 访问：`https://raydenpromen96-maker.github.io/moocow-color-tool/`
3. 测试以下功能：
   - [ ] 页面正常加载
   - [ ] RAL颜色选择
   - [ ] 色差计算功能
   - [ ] 语言切换（中/英/日）
   - [ ] 移动设备适配

## 📂 最终文件结构

您的仓库应该包含：
```
moocow-color-tool/
├── index.html                    # ✅ 主要工具文件
├── README.md                     # ✅ 项目说明
├── LICENSE                       # ✅ 许可证
├── DEPLOYMENT.md                 # ✅ 部署说明
└── .github/
    └── workflows/
        └── deploy.yml            # ✅ 自动部署配置
```

## 🚨 常见问题解决

### 问题1：Actions权限错误
**解决方案**：
1. Settings → Actions → General
2. 找到 "Workflow permissions"
3. 选择 "Read and write permissions"
4. 勾选 "Allow GitHub Actions to create and approve pull requests"
5. 点击 Save

### 问题2：Pages未启用
**解决方案**：
1. 确保在Settings → Pages中正确配置
2. 检查分支名是否为 "main"
3. 等待几分钟让设置生效

### 问题3：网站404错误
**解决方案**：
1. 检查index.html文件是否在根目录
2. 确认GitHub Pages已启用
3. 等待DNS传播（可能需要几小时）

## 🔄 更新网站

要更新工具内容：
1. 在GitHub仓库中点击 `index.html`
2. 点击编辑按钮（铅笔图标）
3. 修改内容后提交
4. GitHub Actions会自动重新部署

## 🎉 完成！

按照以上步骤，您将拥有：
- ✅ 专业的RAL调色工具
- ✅ 自动化部署流程
- ✅ 永久的在线访问地址
- ✅ 完整的项目文档

**访问地址**: https://raydenpromen96-maker.github.io/moocow-color-tool/
