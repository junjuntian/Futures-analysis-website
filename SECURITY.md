# 安全说明

## 秘密信息

不得提交或记录以下内容：

- `.env`
- API Key
- Cookie
- Token
- storage state
- 主密钥
- 数据库明文凭据
- 用户上传文件

## 主密钥

futures VPS 当前主密钥路径按 `docs/DECISIONS.md` 固定为：

```text
/etc/futures-platform/secrets/master-key-v1
```

该文件必须由 `root` 所有，权限 `0400`，不得进入 Git、Docker 镜像或 PostgreSQL。

## 漏洞处理

发现安全问题时先记录复现条件、影响范围和证据，不在公开日志或 issue 中粘贴秘密。
