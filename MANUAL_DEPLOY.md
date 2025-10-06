# 手动部署到GitHub指南

## 🚀 快速部署步骤

### 第1步：创建GitHub仓库
1. 访问 [GitHub](https://github.com) 并登录
2. 点击右上角的 "+" 按钮，选择 "New repository"
3. 填写仓库信息：
   - **Repository name**: `moocow-color-tool`
   - **Description**: `Professional RAL color mixing tool with Delta E calculation`
   - **Visibility**: Public
   - **不要勾选** "Add a README file"（我们已经有了）
4. 点击 "Create repository"

### 第2步：上传文件
有两种方法上传文件：

#### 方法A：拖拽上传（推荐）
1. 在新创建的仓库页面，点击 "uploading an existing file"
2. 将以下文件拖拽到页面上：
   - `index.html`
   - `README.md`
   - `LICENSE`
   - `DEPLOYMENT.md`
   - `.github/workflows/deploy.yml`（需要先创建.github/workflows文件夹）
3. 提交信息填写：`Initial commit: MooCow Mini Color Tool v6.4`
4. 点击 "Commit changes"

#### 方法B：使用Git命令行
```bash
# 在您的本地机器上执行
git clone https://github.com/raydenpromen96-maker/moocow-color-tool.git
cd moocow-color-tool

# 将下载的文件复制到这个目录，然后：
git add .
git commit -m "Initial commit: MooCow Mini Color Tool v6.4"
git push origin main
```

### 第3步：启用GitHub Pages
1. 在仓库页面，点击 "Settings" 标签
2. 在左侧菜单中找到 "Pages"
3. 在 "Source" 部分：
   - 选择 "Deploy from a branch"
   - Branch: 选择 "main"
   - Folder: 选择 "/ (root)"
4. 点击 "Save"
5. 等待几分钟，您的网站将在以下地址可用：
   `https://raydenpromen96-maker.github.io/moocow-color-tool/`

## 📁 文件结构
确保您的仓库包含以下文件：
```
moocow-color-tool/
├── index.html              # 主要工具文件
├── README.md               # 项目说明
├── LICENSE                 # MIT许可证
├── DEPLOYMENT.md           # 部署说明
├── MANUAL_DEPLOY.md        # 手动部署指南
└── .github/
    └── workflows/
        └── deploy.yml      # GitHub Actions自动部署
```

## 🔧 GitHub Actions自动部署
包含的 `.github/workflows/deploy.yml` 文件会自动：
- 在每次推送到main分支时触发
- 自动构建和部署到GitHub Pages
- 无需手动操作

## ✅ 验证部署
部署完成后，访问您的网站并测试：
- [ ] 页面正常加载
- [ ] RAL颜色选择功能
- [ ] 色差计算功能
- [ ] 多语言切换
- [ ] 移动设备适配

## 🌐 访问地址
- **主要地址**: https://raydenpromen96-maker.github.io/moocow-color-tool/
- **仓库地址**: https://github.com/raydenpromen96-maker/moocow-color-tool

## 🔄 更新网站
要更新网站内容：
1. 修改 `index.html` 文件
2. 提交更改到GitHub仓库
3. GitHub Actions会自动重新部署（约2-5分钟）

## 📞 技术支持
如果遇到问题：
1. 检查GitHub Pages设置是否正确
2. 确认所有文件都已上传
3. 查看GitHub Actions的构建日志
4. 等待DNS传播（可能需要几分钟到几小时）

---
🎉 部署完成后，您将拥有一个专业的在线RAL调色工具！
