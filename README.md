# job-scan-germany

**[中文](#中文)**

## English

`job-scan` is a local job discovery and review tool for Germany. It searches for jobs based on your resume, target roles, and preferred locations, removes duplicate listings, uses AI to review complete job descriptions, and presents eligible, uncertain, and excluded jobs in a browser.

The project searches only for jobs in Germany and assumes that the candidate needs visa sponsorship. It does not submit applications, contact recruiters, or bypass logins and CAPTCHA challenges.

### What it does

- Searches Bundesagentur für Arbeit, LinkedIn, Indeed Deutschland, StepStone, Glassdoor Deutschland, and Simplify.
- Builds a candidate profile from a PDF or DOCX resume.
- Uses Claude Code or an Anthropic-compatible API to compare jobs with the resume.
- Lets you review, filter, and track jobs in a local web interface.
- Keeps a separate history record for each web search.
- Optionally runs a daily scan.

### Requirements

- Linux or macOS.
- Python 3.11 or newer.
- A text-based PDF or DOCX resume. OCR for scanned PDFs is not supported.
- One AI runtime:
  - Claude Code CLI, installed and authenticated.
  - An Anthropic-compatible API endpoint, model, and API key.
- To search LinkedIn, Indeed Deutschland, StepStone, Glassdoor Deutschland, or Simplify:
  - OpenCLI and its Browser Bridge extension.
  - Chrome must remain open.
  - Sign in to sites that require an account and complete any CAPTCHA or browser challenge before scanning.

### Installation

Clone or download the project, then run these commands from the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Confirm that `job-scan` is installed:

```bash
job-scan version
```

If you use Claude Code, confirm that it is installed and authenticated:

```bash
claude --version
claude auth status
```

### Start the app

Keep the Python virtual environment active, then run:

```bash
job-scan review
```

The terminal prints the Setup URL. Open it in a browser:

- Linux normally prints `http://job-scan-germany.local:8765/setup` and a LAN IP fallback.
- macOS prints `http://127.0.0.1:8765/setup`.

On first use:

1. Upload a PDF or DOCX resume.
2. Select Claude Code or configure an Anthropic-compatible API.
3. Enter job titles, locations, German level, and job sources.
4. Optionally set a daily scan time.
5. Submit the setup and wait for the scan to finish.
6. Review and manage the results on the Review page.

Press `Ctrl-C` to stop the local service.

### Common commands

```bash
# Check the saved configuration and runtime environment
job-scan doctor

# Run a scan with the saved configuration
job-scan scan

# Start the local web interface
job-scan review

# Start on another port
job-scan review --port 9123
```

Data is stored in `~/.job-scan` by default. To use another location, set `JOB_SCAN_HOME` before each command:

```bash
export JOB_SCAN_HOME=/path/to/job-scan-data
job-scan review
```

---

## 中文

`job-scan` 是一个在本机运行的德国职位搜索和筛选工具. 它根据用户的简历, 求职方向和地点偏好搜索职位, 合并重复结果, 使用 AI 阅读完整职位描述, 最后在浏览器中展示适合, 待确认和不适合的职位.

项目只搜索德国职位, 并默认求职者需要签证支持. 它不会自动投递职位, 不会联系招聘人员, 也不会绕过登录或验证码.

### 能做什么

- 搜索德国联邦就业局, LinkedIn, Indeed Deutschland, StepStone, Glassdoor Deutschland 和 Simplify 的职位.
- 从 PDF 或 DOCX 简历生成求职画像.
- 使用 Claude Code 或 Anthropic-compatible API 分析职位与简历的匹配情况.
- 在本地网页中查看, 筛选和跟踪职位.
- 保存每次网页搜索的独立历史记录.
- 可选配置每日自动扫描.

### 环境要求

- Linux 或 macOS.
- Python 3.11 或更高版本.
- 文本型 PDF 或 DOCX 简历. 扫描图片型 PDF 不支持 OCR.
- 以下 AI 运行方式任选一种:
  - 已安装并登录的 Claude Code CLI.
  - 可用的 Anthropic-compatible API 地址, 模型和 API key.
- 搜索 LinkedIn, Indeed Deutschland, StepStone, Glassdoor Deutschland 或 Simplify 时, 还需要:
  - 已安装 OpenCLI 及其 Browser Bridge 扩展.
  - 保持 Chrome 开启.
  - 在 Chrome 中提前登录需要登录的职位网站, 并处理完验证码或浏览器挑战.

### 安装

克隆或下载项目后, 在项目目录中执行:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

确认命令可用:

```bash
job-scan version
```

如果使用 Claude Code, 启动前确认它已安装并登录:

```bash
claude --version
claude auth status
```

### 启动

保持 Python 虚拟环境已激活, 然后运行:

```bash
job-scan review
```

终端会显示 Setup 地址. 在浏览器中打开该地址:

- Linux 通常显示 `http://job-scan-germany.local:8765/setup`, 并同时提供局域网 IP 地址.
- macOS 显示 `http://127.0.0.1:8765/setup`.

首次使用时:

1. 上传 PDF 或 DOCX 简历.
2. 选择 Claude Code 或配置 Anthropic-compatible API.
3. 填写职位关键词, 地点, 德语水平和职位来源.
4. 可选设置每日扫描时间.
5. 提交设置并等待扫描完成.
6. 在 Review 页面查看和管理结果.

按 `Ctrl-C` 停止本地服务.

### 常用命令

```bash
# 检查当前配置和运行环境
job-scan doctor

# 使用已保存的配置立即扫描
job-scan scan

# 启动本地网页
job-scan review

# 使用其他端口启动
job-scan review --port 9123
```

默认数据保存在 `~/.job-scan`. 如需修改位置, 在每次运行命令前设置 `JOB_SCAN_HOME`:

```bash
export JOB_SCAN_HOME=/path/to/job-scan-data
job-scan review
```
