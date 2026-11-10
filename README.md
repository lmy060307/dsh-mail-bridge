# dsh-mail-bridge

在 DeepSeek Harness 里收发邮件。基于 Python 标准库的 SMTP / IMAP 实现，无需额外 npm 依赖，默认适配 QQ 邮箱。

## 提供的工具

| 工具 | 作用 |
| --- | --- |
| `mail_send` | 发送邮件，支持抄送与本地附件 |
| `mail_list` | 列出某个文件夹最近的邮件（uid / 发件人 / 主题 / 时间） |
| `mail_read` | 读取指定 uid 的邮件正文（纯文本与 HTML） |
| `mail_download` | 把指定邮件的附件下载到本地目录 |
| `mail_folders` | 列出邮箱里的文件夹（IMAP 名称已解码为可读形式） |

## 安装

```bash
dsh plugin install dsh-mail-bridge
```

或从本地目录安装：

```bash
dsh plugin install /path/to/mail-bridge
```

需要本机有 Python 3（默认命令 `python`，可用 `pythonPath` 覆盖）。

## 配置

**凭据不要写进插件包**，而是在 profile 的 `cordis.patch.yml` 里按 id 覆盖：

```yaml
- id: mail-bridge
  name: dsh-mail-bridge
  config:
    user: your-name@qq.com
    pass: your-smtp-authorization-code
```

其余字段留空即可（默认 `smtp.qq.com:465`、`imap.qq.com:993`）。要接其他邮箱时再补：

```yaml
    smtpHost: smtp.example.com
    smtpPort: 465
    imapHost: imap.example.com
    imapPort: 993
```

QQ 邮箱的 `pass` 是「设置 → 账户 → POP3/IMAP 服务」里生成的**授权码**，不是登录密码。

## 安全提示

- 授权码等同于邮箱的收发权限，只放在本地 profile 配置里，不要提交到任何仓库。
- 发信工具会真实发出邮件，建议在需要人工确认的场景下使用。
- 授权码泄露后应立即在邮箱后台重新生成。

## 已知限制

- 只实现了 QQ 邮箱用得上的子集：不使用 OAuth，不解析草稿、不回写已读状态。
- 附件下载会过滤路径穿越，文件名冲突时自动加序号。
- 邮件正文的远程图片不会被下载。

## License

MIT
