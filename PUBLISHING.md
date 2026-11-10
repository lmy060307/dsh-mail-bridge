# 发布到 DSH 插件市场

市场本身只是一个浏览界面，目录数据来自 GitHub 仓库 `awesome-dsh-plugin/awesome-dsh-plugin`。
所以发布分两步：先上 npm，再去那个仓库开 PR 加一条 entry。

## 1. 发布 npm 包

```bash
npm login
npm publish --access public
```

发布前确认名字没被占用：

```bash
npm view dsh-mail-bridge
```

包内 `files` 已限制为运行时需要的 8 个文件，`private` 已去掉。

## 2. 提交目录 entry

入口：https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/blob/main/contributing.md

entry 草稿（字段名来自 `dshmarket` 的 `RegistryPlugin` 类型，具体落盘位置与 `install` 写法以 contributing.md 为准）：

```json
{
  "name": "dsh-mail-bridge",
  "owner": "<你的 GitHub 用户名>",
  "url": "https://github.com/<owner>/dsh-mail-bridge",
  "category": "productivity",
  "description": {
    "en": "Send, read and download email through SMTP/IMAP. Works out of the box with a QQ mailbox.",
    "zh": "在 DSH 里收发邮件：发信、列收件箱、读正文、下载附件，默认适配 QQ 邮箱。"
  },
  "npm": "dsh-mail-bridge",
  "install": "npm:dsh-mail-bridge",
  "version": "0.1.0",
  "added": "2026-11-10"
}
```

`repository`、`homepage`、`bugs` 目前没有填，开了 GitHub 仓库后补进 package.json 再发一版。

## 3. 本机环境注意

Windows 沙箱里 curl / PowerShell / git 默认走 Schannel，会报 `AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`，看起来像断网，其实网络是通的：

- HTTPS 抓取：用 Python 的 OpenSSL 栈（`urllib`）正常
- git over HTTPS：加 `-c http.sslBackend=openssl`
- git over SSH：不受影响，但要把 `UserKnownHostsFile` 指到可写目录

仓库已初始化并有首个提交，remote 指向 `git@github.com:lmy060307/dsh-mail-bridge.git`。密钥在 `D:\agent\.ssh\id_ed25519_github`，公钥加到 GitHub 后执行：

```bash
GIT_SSH_COMMAND='ssh -i D:/agent/.ssh/id_ed25519_github -o UserKnownHostsFile=D:/agent/.ssh/known_hosts' git push -u origin main
```

