# 岗位采集工作台

一款面向 Windows 的本地学习工具，用于采集和整理招聘网站上的岗位信息、提取岗位技术关键词，以及收集开源项目和 AI 应用案例候选。数据默认保存在本机，可导出为 Excel、PDF 或 JSON。

> 招聘网站的页面结构、登录要求和访问规则可能变化。各站点适配器均为试验状态；使用前请确认来源规则、授权和适用法律。公开可访问不等于允许自动采集。

## 功能

- **岗位采集**：按关键词、地区及数量手动启动；支持在专用浏览器中人工筛选后采集当前页面。
- **本地管理**：SQLite 保存岗位和任务，支持去重、筛选、详情查看、批量选择及删除。
- **导入与导出**：支持 CSV、Excel 导入；可按筛选结果或选中项导出 Excel 和中文 PDF。
- **岗位技术提取**：使用本地技术词典从岗位标题和描述中提取关联及证据，生成待复核的 JSON 批次。
- **内容候选**：采集 GitHub、Gitee 仓库元数据和 README 来源材料；支持腾讯云公开案例采集与人工整理。候选需要人工核对，README 不直接作为发布正文。
- **可选后台同步**：支持配置兼容指定批次协议的后台，手动或按设置提交岗位及发现数据。后台服务需要单独部署；本仓库不包含管理后台或小程序。

当前提供 BOSS 直聘、51job／前程无忧、智联招聘、猎聘、中华英才网、卓博人才网、南方人才网和国家大学生就业服务平台的试验适配。地区、列表和详情能力因来源而异，以应用中的来源说明为准。网站页面改版、登录或验证要求都可能导致适配失效。

## 环境要求

- Windows；目前主要在 Windows 环境使用和验证。
- Python 3.13，以及兼容的 Chrome 或 Edge 浏览器。
- 依赖版本见 [requirements.txt](requirements.txt)，主要包括 PySide6、DrissionPage、openpyxl、ReportLab 和 Requests。
- PDF 使用仓库内提供的中文字体，无需安装 Microsoft Excel。

## 快速开始

克隆仓库并安装依赖：

```powershell
git clone https://github.com/kevin-sanas/job-collector-workbench.git
cd job-collector-workbench
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

也可以在 PyCharm 中打开项目，将解释器设置为项目内的 `.venv\Scripts\python.exe`，新建 Python 运行配置，选择 `main.py`，并将工作目录设置为项目根目录。首次使用时，在“来源与设置”中选择本机 Chrome 或 Edge 的可执行文件。

## 基本使用

1. 在“采集任务”选择来源、关键词、省份和城市，设置采集上限。来源暂不支持自动筛选的地区，可先在专用浏览器中筛选，再使用“采集当前页”。
2. 点击“打开浏览器并登录”。需要登录、关闭弹窗或完成人机验证时，请在专用窗口自行处理，再按界面提示继续；不要向他人提供账号凭据或浏览器会话。
3. 采集完成后在“岗位数据”中查看和筛选，可导入本地 CSV／Excel，或导出全部筛选结果及选中的岗位。
4. 在“发现数据”中运行本地技术提取，或采集／整理内容候选；导出 JSON 前请核对来源、证据及待复核问题。

采集器不会自动求解验证码。遇到访问限制或限流时应停止并按网站提示处理。少量现场验证不代表所有地区、账号和页面始终可用。

## 网络与代理

招聘页面由工具启动的独立 Chromium 浏览器访问，使用独立用户目录并设置 `--no-proxy-server`。这一设置不会修改 Windows 系统代理或 Clash 配置；它也不能绕过 TUN 等透明网络接管。如需确认实际网络路径，请在自己的环境中检查。

GitHub 内容来源的默认 `system` 模式使用 Python 进程实际获得的代理环境；它不保证自动读取 Windows 系统代理。若需要为 GitHub 单独指定本机 HTTP 代理，可在 `config/discovery/` 下创建不纳入版本控制的 `content_sources.local.json`：

```json
{
  "network_mode": "local_proxy",
  "proxy_url": "http://127.0.0.1:7890"
}
```

将端口改为自己的代理端口；也可使用 `direct` 或 `system` 模式。该本地覆盖只影响 GitHub 内容请求，不改变招聘浏览器、Gitee 或腾讯云来源的直连配置。Gitee 推荐页可能要求用户人工完成验证。

## 数据与隐私

| 路径 | 用途 |
| --- | --- |
| `data/jobs.sqlite3` | 本地岗位及任务数据 |
| `data/discovery.sqlite3` | 技术提取、内容候选及批次数据 |
| `data/browser_profiles/` | 各来源的专用浏览器会话 |
| `config/settings.json` | 本机设置；启用后台同步时可能包含专用凭据 |
| `exports/`、`logs/` | 本地导出文件和运行日志 |

以上运行数据不纳入版本库。浏览器会话、导出文件和设置可能包含敏感信息，请勿公开分享；后台凭据保存在本机设置文件中，请自行控制文件访问权限。备份数据库前请先正常退出工作台。

## 项目结构

```text
app/
  browser.py, collector.py   浏览器会话与采集任务
  adapters.py, adapter_*.py  来源适配
  storage.py, models.py      数据模型与 SQLite
  transfers.py               导入、Excel 和 PDF 导出
  discovery/                 技术提取、内容来源与 JSON 批次
  ui/                        PySide6 桌面界面
assets/                      中文字体、省市数据及其许可
config/                      来源规则、技术词典和 JSON Schema
examples/                    虚构岗位导入示例
tests/                       离线回归测试
main.py                      应用入口
```

## 测试

在项目根目录运行：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe main.py --check
```

`--check` 使用临时数据库启动窗口并自动退出，不采集网站。离线测试不能替代对第三方网站当前页面、账号状态和网络条件的实际验证。

## 已知限制

- 来源适配器为试验规则；并非所有网站都支持所有城市自动搜索。通常只读取当前已加载列表，实际数量可能低于上限。
- 来源未提供或无法可靠识别的字段会留空；薪资字体、地点和岗位身份识别可能因页面变化而失败。
- 内容候选、技术词典及技术关联需要人工复核；不提供自动生成的可信度或能力判断。
- 未实现通用网页采集、进程重启后自动续采，也不承诺逐请求网络路由审计。

## 许可证与第三方依赖

本项目自有代码采用 [MIT License](LICENSE)。第三方依赖及资源使用各自的许可，项目许可证不会改变它们的条款：

- [DrissionPage 4.1.1.4](https://pypi.org/project/DrissionPage/4.1.1.4/) 的发布条款限制用于个人学习和合法非盈利目的；商业使用需取得其权利人授权。请查阅其完整条款。
- [Qt for Python / PySide6](https://doc.qt.io/qtforpython-6/commercial/index.html) 提供开源与商业许可选项，使用和分发时需遵守适用条款。
- Noto Sans SC 字体及其许可位于 `assets/fonts/`；省市数据的来源与许可位于 `assets/regions/`。

欢迎通过仓库 Issue 反馈问题或提交改进。报告页面问题时请先移除账号信息、Cookie、令牌和个人联系方式，不要上传本地数据库或浏览器配置目录。
