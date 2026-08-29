# 抖音博主新作品邮件通知

这个目录提供一个 GitHub Actions 监控器：Actions 定时打开抖音主页，在页面上下文中读取该博主的公开作品列表；发现未记录的作品后，通过 SMTP 发邮件，并把已见作品 ID 保存到 `douyin/state.json`。

默认监控主页：<https://www.douyin.com/user/MS4wLjABAAAA3Z3BGF5DOu1M-ONu57cXLA7uGZmQI8ibm_ZVx_837Ao?from_tab_name=main>。

之所以使用 Chromium，是因为抖音网页会在浏览器运行时生成接口请求所需的动态参数。脚本不保存登录账号密码；如果抖音对 GitHub Actions 的匿名访问触发校验，可以把浏览器 Cookie 作为 `DOUYIN_COOKIE` Secret 传入。

## 从零开始部署

### 1. 创建 GitHub 仓库

建议在 GitHub 新建一个单独的仓库，例如 `douyin-monitor`。如果当前工作区还包含其他项目，不要使用 `git add .`，否则可能把无关文件一并上传；只提交下面的两个路径：

- `douyin/`
- `.github/workflows/douyin-monitor.yml`

在当前 Windows PowerShell 中可以这样操作（把远程地址替换成你自己的仓库地址）：

```powershell
Set-Location -LiteralPath "C:\Users\77035\Desktop\Claude code"
git init -b main
git add douyin .github
git commit -m "feat: add Douyin monitor"
git remote add origin "https://github.com/<你的用户名>/douyin-monitor.git"
git push -u origin main
```

PowerShell 请一行一行执行，每执行一行后确认出现新的 `PS ...>` 提示符；不要把 `>>` 也输入进去。若提示符变成单独一行的 `>>`，说明上一条命令没有结束，先按 `Ctrl+C` 取消，再重新执行。建议先执行 `Get-Location` 确认当前路径，再执行后续 Git 命令。截图中如果已经显示 `On branch main`，说明仓库已经初始化，不需要再次执行 `git init`。

如果本地已经配置过 `origin`，不要重复执行 `git remote add origin`；先用 `git remote -v` 查看即可。工作流文件必须位于仓库根目录的 `.github/workflows/`，不能放在 `douyin/.github/`，否则 GitHub 不会将它识别为 Actions 工作流。

### 2. 打开 Actions 写入权限

进入 GitHub 仓库页面：

1. 点击 `Settings`。
2. 左侧进入 `Actions → General`。
3. 找到 `Workflow permissions`。
4. 选择 `Read and write permissions`。
5. 点击 `Save`。

工作流文件本身也声明了 `contents: write`，这两处都允许写入后，Actions 才能在首次运行或发现新作品后提交 `douyin/state.json`。

### 3. 准备一个发件邮箱

只需要选择 QQ、163 或 Gmail 中的一个作为发件箱，收件箱可以是任意邮箱。不要把网页登录密码直接填入 `SMTP_PASSWORD`，应使用邮箱服务商生成的授权码/应用专用密码。

#### QQ 邮箱

1. 登录 <https://mail.qq.com/>。
2. 打开设置，进入账户/账户安全相关页面。
3. 找到 `POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV` 服务区域。
4. 开启 SMTP 相关服务，并按页面要求完成验证。
5. 生成并复制授权码。

填写参数：`smtp.qq.com`、端口 `465`、SSL `true`，密码使用刚生成的授权码。

#### 163 邮箱

1. 登录 <https://mail.163.com/>。
2. 进入 `设置 → POP3/SMTP/IMAP`。
3. 开启 `IMAP/SMTP` 或 `POP3/SMTP` 服务。
4. 按要求完成短信验证并生成客户端授权密码。

填写参数：`smtp.163.com`、端口 `465`、SSL `true`，密码使用客户端授权密码。

#### Gmail

1. 打开 Google 账号的 `安全性` 页面，先开启两步验证。
2. 在 `安全性 → 两步验证 → 应用专用密码` 中创建一个应用密码，例如命名为 `Douyin GitHub Actions`。
3. 复制生成的 16 位应用专用密码。

填写参数：`smtp.gmail.com`、端口 `465`、SSL `true`。Gmail 的应用专用密码需要两步验证；不要使用 Gmail 的网页登录密码。

### 4. 添加 GitHub Secrets

进入仓库：`Settings → Secrets and variables → Actions → Secrets`，点击 `New repository secret`，逐项新增以下 4 个 Secrets：

| Name | Value 示例 | 作用 |
| --- | --- | --- |
| `SMTP_USERNAME` | `your_sender@qq.com` | SMTP 登录账号，填完整邮箱地址 |
| `SMTP_PASSWORD` | `邮箱授权码` | QQ/163 授权码或 Gmail 应用专用密码 |
| `SMTP_FROM` | `your_sender@qq.com` | 发件人地址，通常与登录账号相同 |
| `SMTP_TO` | `your_receiver@example.com` | 收件地址；多个地址用英文逗号分隔 |

输入真实值时不要带反引号。GitHub Secrets 创建后不会再显示原文，这是正常现象。邮箱授权码和 Cookie 都属于敏感信息，只能放在 Secrets，不能写进 `.py`、`.yml`、README 或提交记录。

### 5. 添加 GitHub Variables

进入同一个页面的 `Variables` 标签，点击 `New repository variable`。最小配置只需添加下面 4 项；不添加时脚本也会使用表中的默认值：

| Name | Value |
| --- | --- |
| `SMTP_HOST` | `smtp.qq.com`、`smtp.163.com` 或 `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USE_SSL` | `true` |
| `PUBLISHED_TIMEZONE` | `Asia/Shanghai` |

如果使用 Gmail 587 端口，则改为添加/设置：`SMTP_PORT=587`、`SMTP_USE_SSL=false`、`SMTP_STARTTLS=true`。

目标主页已经写入脚本默认值，因此不需要配置 `DOUYIN_USER_URL`。如果以后监控其他博主，可以新增 Variable `DOUYIN_USER_URL`，值填写完整的抖音主页链接，例如 `https://www.douyin.com/user/<sec_user_id>`。更换博主前要删除仓库中的 `douyin/state.json`，否则脚本会提示状态文件属于另一位博主。

### 6. 手动运行首次检查

1. 打开仓库的 `Actions` 标签。
2. 如果页面提示启用工作流，点击 `I understand my workflows, go ahead and enable them` 或类似的启用按钮。
3. 左侧点击 `Douyin new-work monitor`。
4. 点击右侧 `Run workflow`，选择 `main` 分支，再点击运行。
5. 打开这次运行的 `check` Job 查看日志。

首次正常运行应看到类似下面的结果：

```text
本次读取到 ... 个作品。
首次运行仅建立基线，记录 ... 个作品，不发送历史作品通知。
```

首次默认不发邮件是为了避免把博主已有的历史作品一次性全部发出。运行成功后，Actions 会提交 `douyin/state.json`。之后只有发现新的作品 ID 才会发邮件。

如果要测试邮件发送，应在建立基线前把 Variable `NOTIFY_ON_FIRST_RUN` 设置为 `true`，再手动运行一次；这会把当前可见作品作为通知发送，测试完成后务必改回 `false` 或删除该 Variable。如果已经运行过首次基线，需要先在 GitHub 网页中删除 `douyin/state.json` 并提交删除，再设置该 Variable 并手动运行；否则脚本会按“已初始化”处理，不会重复发送历史作品。

### 7. 等待自动检查

工作流使用 `0,25,50 * * * *`，按 UTC 的每小时第 00、25、50 分钟运行。中国大陆时间是 UTC+8，例如 UTC 00:00 对应北京时间 08:00。GitHub 的定时任务可能因平台负载延迟几分钟，而且标准 cron 在每小时边界无法保持严格的 25 分钟间隔，所以 50 分钟到下一小时 00 分钟之间会有一次 10 分钟间隔。

## 抖音 Cookie 兜底配置

第一次可以先不配置 Cookie。若日志报 `抖音作品接口返回空响应` 或连续抓取失败，再执行下面操作：

1. 在 Chrome 中打开目标抖音主页，必要时先登录抖音。
2. 按 `F12` 打开开发者工具，进入 `Network`，刷新页面。
3. 找到抖音主页/作品接口请求，打开 `Headers`，找到 `Request Headers` 下的 `Cookie`。
4. 复制 `Cookie:` 后面的完整内容，不要复制 `Cookie:` 这几个字。
5. 在 GitHub 的 `Settings → Secrets and variables → Actions → Secrets` 中新增或更新 `DOUYIN_COOKIE`。
6. 再次手动运行工作流。

Cookie 会过期，过期后需要重新复制。Cookie 等同于登录会话凭证，不要发到聊天、写入代码或打印到日志。

## 常见问题

- **第一次没有收到邮件**：这是默认行为，第一次只建立基线；若要测试邮件，需在首次运行前设置 `NOTIFY_ON_FIRST_RUN=true`。已有 `state.json` 时，先删除并提交它，再设置该 Variable 测试。
- **`SMTPAuthenticationError`**：检查 SMTP 用户名是否为完整邮箱地址，并确认密码是授权码/应用专用密码，而不是网页登录密码。
- **连接超时或 SSL 错误**：465 使用 `SMTP_USE_SSL=true`；587 使用 `SMTP_USE_SSL=false`、`SMTP_STARTTLS=true`。
- **抖音作品接口空响应**：先重新手动运行；仍失败时配置最新 `DOUYIN_COOKIE`。
- **`git push` 被拒绝**：检查 `Settings → Actions → General → Workflow permissions` 是否为 `Read and write permissions`，以及仓库默认分支是否为 `main`。
- **工作流根本不显示**：确认路径是仓库根目录 `.github/workflows/douyin-monitor.yml`，并确认文件已推送到默认分支。
- **更换博主后报状态文件不匹配**：删除 `douyin/state.json` 并重新运行，让新博主重新建立基线。

## 本地测试

在安装 Python 依赖和 Chromium 后运行：

```powershell
python -m pip install -r douyin/requirements.txt
python -m playwright install chromium
python douyin/monitor.py
```

脚本会把配置从环境变量读取。第一次本地运行也会写入 `douyin/state.json`；若只是验证抓取，不想留下状态，可以运行后删除该文件，或使用临时状态路径：

```powershell
$env:DOUYIN_STATE_FILE = "douyin/state.local.json"
python douyin/monitor.py
```

不要把 `SMTP_PASSWORD`、`DOUYIN_COOKIE` 或包含这些值的调试日志提交到 Git。删除状态文件会使下一次运行重新建立基线；只有显式设置 `NOTIFY_ON_FIRST_RUN=true` 时才会把当前作品作为新作品通知。
