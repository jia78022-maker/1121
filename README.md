# 客户管理器

Windows 本地客户、订单、备货与交货提醒工具。

## 本地运行

需要 Windows 与 Python 3.14+：

```powershell
python app.py
```

本地账号、会话和订单数据库只保存在运行程序的文件夹中，已通过 `.gitignore` 排除，绝不会提交到仓库。

## 发布新版

每次发布将生成 `客户管理器.exe`，并通过 GitHub Releases 提供给自动更新功能下载。更新清单字段见 `update_manifest.example.json`。
