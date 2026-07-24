# 部署说明

## 部署边界

- 本地 Git 仓库是唯一源码源头。
- VPS 上不得手工编辑业务源码。
- 部署目录建议：`/opt/futures-platform`。
- 秘密目录建议：`/etc/futures-platform/secrets`。
- 数据目录建议：`/var/lib/futures-platform`。

## 本地启动

```powershell
Copy-Item .env.example .env
docker compose --profile dev up --build
```

本地默认入口：

```text
http://localhost:8088
```

## 主密钥挂载

futures VPS 主密钥文件：

```text
/etc/futures-platform/secrets/master-key-v1
```

要求：

- `root` 所有。
- 权限 `0400`。
- 只读挂载给需要解密的容器。
- 不进入 Git、Docker 镜像、PostgreSQL、日志或最终回复。

## 恢复与轮换

1. 新建主密钥版本文件，例如 `master-key-v2`。
2. 使用受控维护任务重新包裹 DEK。
3. 验证旧密文可由新版本解密。
4. 将 `key_version_metadata` 中旧版本标记为 retired。
5. 数据库备份与主密钥恢复副本分开保存。
