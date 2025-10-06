# GitHub 部署说明

## 快速部署到GitHub Pages

### 方法1：手动上传（推荐）

1. **创建GitHub仓库**
   - 登录 [GitHub](https://github.com)
   - 点击右上角的 "+" 按钮，选择 "New repository"
   - 仓库名称：`moocow-color-tool`
   - 描述：`Professional RAL color mixing tool with Delta E calculation`
   - 选择 "Public"
   - 勾选 "Add a README file"
   - 点击 "Create repository"

2. **上传文件**
   - 在新创建的仓库页面，点击 "uploading an existing file"
   - 将以下文件拖拽上传：
     - `index.html` (主要工具文件)
     - `README.md` (项目说明)
   - 提交信息：`Initial commit: MooCow Mini Color Tool v6.4`
   - 点击 "Commit changes"

3. **启用GitHub Pages**
   - 在仓库页面，点击 "Settings" 标签
   - 滚动到 "Pages" 部分
   - 在 "Source" 下选择 "Deploy from a branch"
   - 选择 "main" 分支和 "/ (root)" 文件夹
   - 点击 "Save"
   - 等待几分钟，您的网站将在 `https://[您的用户名].github.io/moocow-color-tool/` 可用

### 方法2：使用Git命令行

```bash
# 1. 在GitHub上创建空仓库 moocow-color-tool

# 2. 在本地执行以下命令
git init
git add .
git commit -m "Initial commit: MooCow Mini Color Tool v6.4"
git branch -M main
git remote add origin https://github.com/[您的用户名]/moocow-color-tool.git
git push -u origin main

# 3. 在GitHub仓库设置中启用Pages
```

## 功能验证清单

部署完成后，请验证以下功能：

- [ ] RAL颜色选择和显示
- [ ] 色差计算功能 (Delta E)
- [ ] 颜料配方生成
- [ ] 多语言切换 (中文/英文/日文)
- [ ] 响应式设计 (移动设备适配)
- [ ] 容量选择和重量计算

## 技术规格

- **文件大小**: ~73KB (单文件应用)
- **兼容性**: 现代浏览器 (Chrome, Firefox, Safari, Edge)
- **依赖**: 无外部依赖，纯HTML/CSS/JavaScript
- **响应式**: 支持桌面和移动设备

## 自定义域名（可选）

如果您有自定义域名，可以：

1. 在仓库根目录创建 `CNAME` 文件
2. 文件内容为您的域名，如：`color-tool.yourdomain.com`
3. 在域名DNS设置中添加CNAME记录指向 `[您的用户名].github.io`

## 更新和维护

要更新工具：

1. 修改 `index.html` 文件
2. 提交更改到GitHub仓库
3. GitHub Pages会自动更新网站（通常需要几分钟）

---

如有问题，请检查GitHub Pages的构建状态或联系技术支持。
