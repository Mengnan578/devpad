# 发布清单

发布到 npm 只需要你的一条授权命令，其余都已就绪。

## 现在就能发的状态（已核对）

| 项 | 状态 |
|---|---|
| 包名 `devpad` | ✅ npm 上未占用 |
| 版本 | `0.1.0` |
| 测试 | ✅ 181 项全过（46 静态 + 54 驱动 + 81 端到端） |
| 打包内容 | ✅ 12 个文件 / 68.3 kB，无 `__pycache__` |
| `files` 白名单 | ✅ 只收 `.py` 与文档，不含 tests/poc |
| `prepack` | ✅ 打包前自动清 Python 字节码 |
| LICENSE | ✅ MIT |
| 元数据 | ✅ 已移除指向 `your-org` 的死链 |

## 你需要做的两步

```bash
npm login          # 浏览器 + OTP 授权，只能你本人操作
```

然后：

```bash
cd ~/devtools/devpad
npm publish
```

或者登录后告诉我，我来执行发布并回报结果。

## 发布后可选的收尾

**1. 补作者与仓库信息**（现在没有仓库，先留着也不会失败，但公开包建议补上）：

```bash
cd ~/devtools/devpad
npm pkg set author="你的名字 <you@example.com>"
npm pkg set repository.type=git repository.url=git+https://github.com/<你>/devpad.git
npm pkg set homepage=https://github.com/<你>/devpad#readme
npm pkg set bugs.url=https://github.com/<你>/devpad/issues
```

**2. 验证安装**

```bash
npx devpad-cli@0.1.0 --version
```

**3. 打标签**（如果建了 git 仓库）

```bash
git tag v0.1.0 && git push --tags
```

## 关于 2FA（发布时仍要求 6 位验证码）

**经典 Publish token 不支持绕过 2FA**，无论怎么配置，`npm publish` 都会要求验证码。
`npm token list` 里显示为 `Publish token` 的就是经典 token。

只有 **Granular Access Token** 且创建时勾了 **Bypass 2FA**，才能免验证码发布。但注意 npm 正在收紧：

- 2026-07-31 起：bypass 2FA 的 GAT 不能再做建/删 token、改包权限与维护者等敏感操作
- 2027-01 起：bypass 2FA 的 GAT **将失去直接发布能力**，只剩暂存（staged），需维护者用 2FA 批准

因此长期方案是 **trusted publishing（OIDC）**，而非长期豁免 2FA 的 token。

眼下最省事：发布时用新鲜验证码——

```bash
./release.sh            # 最后一步才提示输入，前面跑测试不占用验证码有效期
```

验证码约 30 秒失效，所以务必在提示出现后再取。

## 版本号的含义

`0.1.0` 是刻意的：macOS-only、只有 iTerm/Terminal/tmux 三种真实驱动，Linux/Windows
还没做。等有人用、有人在 Linux 上要，再谈 `0.2` 和 `1.0`。

## 发布之后不要做的事

- **不要急着 `npm unpublish`**：npm 对 72 小时后的退包有严格限制，而且名字会被冻结。
  发错版本的正确做法是 `npm version patch && npm publish`。
- **不要在没跑测试的情况下 `npm publish`**：`prepack` 只清缓存，不跑测试。
  发布前手动跑一遍：

```bash
cd ~/devtools/devpad
python3 tests/check_static.py && python3 tests/test_terminals.py && python3 tests/test_devpad.py
```
