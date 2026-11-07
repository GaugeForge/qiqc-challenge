"use strict";
// Only the independent staff application loads this module. Raw records, user
// content, identifiers and input values retain their original language/bytes.
(() => {
  const dictionary = {
    "Operations console": "赛事管理后台", "Competition website ↗": "参赛网站 ↗",
    "STAFF": "工作人员", "STAFF WORKSPACE": "工作人员工作台",
    "QIQC · Operations console": "QIQC · 赛事管理后台", "Authorized staff only": "仅限授权工作人员",
    "All review states": "全部审查状态", "From (UTC+8)": "起始时间（UTC+8）", "Until (UTC+8)": "截止时间（UTC+8）",
    "Download review report": "下载审查报告", "Event status": "赛事阶段", "Formal model configuration is not ready. Admission is paused; no attempts are charged.": "正式模型配置尚未就绪，受理已暂停，不扣评测机会。", "Six-hour quiet period. New uploads and evaluations are closed.": "赛前六小时静息期，已关闭新上传与评测受理。",
    "Interface language": "界面语言", "Skip to content": "跳到正文", "Sign out": "退出登录",
    "Staff console": "工作人员后台", "Staff sign in": "工作人员登录", "ORGANIZER ACCESS": "工作人员入口",
    "Manage your submissions, start an evaluation and follow your results.": "请使用工作人员账号登录赛事管理后台。",
    "Use the account issued by the organizers.": "请使用赛事组开通的账号。", "Email address": "邮箱地址", "Password": "密码",
    "Sign in →": "登录 →", "For account access or a password reset, contact the organizers.": "账号或密码问题请联系赛事负责人。",
    "ACCOUNT SECURITY": "账号安全", "Set your password": "设置密码", "Change temporary password": "修改临时密码",
    "Replace your temporary password before using your account.": "使用账号前，请先修改临时密码。",
    "New password (at least 12 characters)": "新密码（至少12个字符）", "Save password →": "保存密码 →", "Password updated.": "密码已更新。",
    "ORGANIZER CONTROL ROOM": "赛事运营工作台", "Competition operations": "赛事运营",
    "Review submissions, manage evaluations and record staff actions. Every change requires a reason and is recorded in the audit log.": "审核提交、管理评测并记录工作人员操作。每项修改都需要填写理由，并写入审计日志。",
    "Evaluation policy": "评测政策", "Scientific evaluations and simulations are labeled separately. Time-limited science runs do not enter the leaderboard.": "科学评测与历史模拟记录分别标记。当前限时科学验证不计入正式排行榜。",
    "Overview": "总览", "Submissions": "提交审查", "Evaluations": "评测管理", "Teams & access": "队伍与账号", "Announcements": "公告", "Operations": "运行与资源", "Audit log": "审计日志",
    "Start with current health and work that needs attention.": "查看当前运行状态和待处理事项。",
    "Review immutable packages and record a decision when staff follow-up is needed.": "查看已固定的提交包，对需要人工处理的版本记录复核结论。",
    "Follow recent evaluations, inspect failures and retry eligible infrastructure errors.": "跟踪近期评测、查看失败原因，对符合条件的平台故障执行补跑。",
    "Find team accounts, issue temporary credentials and adjust attempt allowances.": "查找队伍账号、开通账号、重置密码并调整评测额度。",
    "Publish versioned notices to everyone, all teams or a selected team.": "向公众、全部队伍或指定队伍发布带版本的公告。",
    "Check service health, verified backups, model usage and the event calendar.": "检查服务状态、备份验证、模型用量和比赛日历。",
    "Inspect who changed what, when, and why.": "核查操作人、时间、修改内容与理由。",
    "Awaiting review": "待审查", "Under review": "审查中", "All submission versions": "全部提交版本", "Queued / running": "排队 / 运行",
    "Needs attention": "待处理异常", "Includes historical unresolved evaluations": "包含历史未处理评测", "Model calls": "模型调用",
    "Dispatch is paused": "派发已暂停", "Dispatch is enabled": "派发已启用",
    "Operator name (recorded with your signed-in account)": "操作人（以当前登录账号记录）", "Reason for this action · required for every change": "操作理由（每项修改必填）",
    "For example: reviewed submission or resolved incident": "例如：已复核提交，或已处理具体故障",
    "Pause dispatch": "暂停派发", "Resume dispatch →": "恢复派发 →",
    "Review submissions": "处理提交审查", "Inspect scanner findings and AI advice.": "查看静态扫描发现和AI审查建议。",
    "Follow evaluations": "跟踪评测", "Check queue, failures and completed results.": "检查队列、异常和完成结果。",
    "Manage team access": "管理队伍账号", "Create accounts, reset passwords and adjust attempts.": "创建账号、重置密码并调整次数。",
    "Check services & backups": "检查服务与备份", "Read monitored status and recovery evidence.": "查看监控状态与恢复验证记录。",
    "Inspect the audit trail": "核查审计日志", "Verify changes and their recorded reasons.": "检查修改及其记录的理由。",
    "Before changing competition state": "修改比赛状态前",
    "Identify the team, submission SHA or evaluation ID. Open its details before taking action.": "先确认队伍、提交SHA或评测ID，再打开详情核查。",
    "Select a work section and enter a specific action reason. Your signed-in staff account is recorded automatically.": "选择对应工作分区并填写具体操作理由。系统自动记录当前工作人员账号。",
    "Use retry only for eligible platform failures. Unconfirmed remote cleanup must be reconciled first; do not create a duplicate run.": "仅对符合条件的平台故障补跑。远端资源未确认清理时，须先核销，避免重复运行。",
    "Confirm the result in the audit log. Notification records do not mean an email was delivered.": "在审计日志中确认结果。通知记录不代表邮件已送达。",
    "Account directory": "账号目录", "Find a team, email or account ID": "搜索队伍、邮箱或账号ID",
    "Team / account": "队伍 / 账号", "Role & access": "角色与访问", "Remaining attempts": "剩余次数", "Actions": "操作",
    "Team": "队伍", "Staff": "工作人员", "Staff administrator": "工作人员管理员", "Team captain": "队长", "Password set": "密码已设置", "Password change required": "需修改密码",
    "Reset password": "重置密码", "Adjust attempts": "调整次数", "Team accounts and admission controls": "账号开通与受理控制",
    "Fill in your operator name and reason first. Share temporary passwords privately. Email delivery is not configured.": "请先填写操作理由，并通过私密渠道交付临时密码。邮件发送尚未配置。",
    "Team captain email": "账号邮箱", "Team name": "队伍或工作人员名称", "Account role": "账号角色",
    "Staff accounts can manage all teams and competition settings. Issue one account per operator.": "工作人员账号可管理全部队伍与赛事设置，请为每位工作人员分别开通账号。",
    "Temporary password (at least 12 characters)": "临时密码（至少12个字符）", "Create team account": "创建账号",
    "Account": "账号", "Select an account": "选择账号", "New password (at least 12 characters)": "新密码（至少12个字符）", "Generate a strong password": "生成强密码", "Reset password": "重置密码", "Copy": "复制", "Clear": "清除",
    "Use this when a team emails to ask for a password reset. Verify the request comes from the account email first. The new password works immediately; the team is not asked to change it. All existing sessions of the account are signed out.": "队伍发邮件申请重置密码时使用。请先核实邮件来自该账号邮箱。新密码立即生效，队伍无需再修改；该账号所有现有登录会话都会退出。",
    "Send this password to the team by email. They can sign in with it right away.": "请通过邮件把该密码发给队伍，对方可直接用它登录。",
    "Password reset. Existing sessions were signed out.": "密码已重置，原有会话已退出。", "Password copied.": "密码已复制。", "Leaderboard": "排行榜", "← Back to the console": "← 返回后台", "Queued evaluations": "排队中的评测", "Running evaluations": "运行中的评测", "Live · updated": "实时 · 更新于", "Provisional standings": "暂定排名", "Can your harness beat GPT-6 Astra?": "你的 harness 能超过 GPT-6 Astra 吗？", "Full evaluation results update automatically; public practice is unranked.": "完整评测结果自动更新；公开测试不计入排名。", "AWAITING COMPETITION": "等待比赛开始", "LIVE · PROVISIONAL": "实时 · 暂定", "BENCHMARKING AGENT HARNESSES": "智能体 HARNESS 评测", "TASKS / EVALUATION": "题 / 每次评测", "FULL ATTEMPTS": "次完整评测", "THE LEADERBOARD": "排行榜", "Each team is ranked by its one complete evaluation: tasks passed first, then mean runtime.": "每队按唯一一次完整评测排名：先比通过题数，再比平均用时。", "Reference pending verification": "基线待核验", "Awaiting measured baseline": "等待实测基线", "No official results published yet.": "暂无正式成绩。", "First above baseline · provisional": "首个超过基线 · 暂定", "Tied · judge review": "并列 · 待评审", "Rank": "排名", "Team / version": "队伍 / 版本", "PASS RATE": "通过率", "Mean time / task": "平均用时 / 题", "Accepted (UTC+8)": "受理时间（UTC+8）", "Show": "显示", "Hide": "隐藏", "Show password": "显示密码", "Hide password": "隐藏密码", "Confirm new password": "确认新密码", "The two passwords do not match.": "两次输入的密码不一致。", "Password reset for": "已重置密码：", "Select the account to reset.": "请选择要重置的账号。", "Copy failed; select the password and copy it manually.": "复制失败，请手动选中密码复制。",
    "Closing admission lets accepted evaluations finish. This control cannot reopen admission.": "关闭受理后，已接受的评测继续完成。此按钮不能重新开放受理。",
    "Close evaluation admission": "关闭评测受理", "Attempt type": "额度类型", "Full evaluation": "全榜评测", "Public test": "公开测试", "Infrastructure test": "环境测试",
    "Change (+ adds, − removes)": "调整值（正数增加，负数减少）", "Record adjustment": "记录额度调整", "Appends a ledger entry · preserves history": "追加额度流水，保留历史记录",
    "Submission review": "提交审查", "Search team, package name or SHA": "搜索队伍、包名或SHA", "Search team, name or SHA": "搜索队伍、名称或SHA",
    "Pending review": "待复核", "Review package": "审查提交包", "Review findings and source": "查看发现与源码", "Waiting for automated review": "等待自动审查",
    "Approve": "批准", "Reject": "拒绝", "Approved": "已批准", "Rejected": "已拒绝", "Static findings": "静态发现", "AI advice": "AI审查建议",
    "No static findings recorded.": "未记录静态风险。", "No completed AI advice is available.": "暂无完成的AI审查建议。", "Captured source excerpts": "采集的源码片段", "No source excerpt is available.": "暂无源码片段。",
    "Package text is untrusted. Review code and findings before deciding; AI advice is not proof of safety.": "提交包内容不可信。请核查代码与发现后决定，AI建议不等于安全保证。",
    "Evaluations and incidents": "评测与异常", "Staff-only internal status": "工作人员内部状态", "All statuses": "全部状态", "Queued": "排队中", "Running": "运行中", "Completed": "已完成", "Cancelled": "已取消", "Cancelling": "取消中",
    "Team / request": "队伍 / 请求", "Evaluation": "评测", "Internal status": "内部状态", "Task states": "任务状态", "Details": "详情", "Retry infrastructure failure": "平台故障补跑", "Invalidate score": "作废成绩",
    "Recorded failures": "已记录故障", "Task number": "任务编号", "Attempt": "尝试次数", "Error code": "错误代码", "No task errors are recorded.": "未记录任务错误。",
    "Retry is available only for eligible infrastructure failures. Unconfirmed remote resources must be reconciled first. A retry preserves the original evaluation and attempt ledger.": "仅符合条件的平台故障可补跑。须先核销未确认的远端资源；补跑保留原评测与额度记录。",
    "Formal appeals": "正式申诉", "One per team": "每队一次", "Respond using the action reason": "以操作理由回复",
    "Publish announcement": "发布公告", "Published notices are immutable; issue a new version to correct them": "已发布公告不可修改，如需更正请发布新版本",
    "Record your operator name and reason first. Publish confirmed information only. Never include credentials, private team data or hidden tasks.": "先填写操作理由。仅发布已确认的信息，不得包含凭据、队伍私有数据或隐藏任务。",
    "Notice version": "公告版本", "Title": "标题", "Body (plain text, 3–8,000 characters)": "正文（纯文本，3–8000字符）", "Audience": "发布对象", "Everyone": "公众", "All teams": "全部队伍", "Selected team only": "指定队伍",
    "Team ID (for a selected team only)": "队伍ID（仅指定队伍时填写）", "Rules version": "规则版本", "Publish and record audit entry": "发布并记录审计", "Newer notices": "更新公告", "Older notices": "更早公告", "No announcements yet.": "暂无公告。",
    "Versioned notices · Asia/Singapore UTC+8": "版本化公告 · 新加坡时间UTC+8", "Status": "状态",
    "Runtime and costs": "运行环境与费用", "Current environment": "当前环境", "Execution": "执行方式", "Time-limited science / simulation": "限时科学验证 / 历史模拟", "Model": "模型", "Global task slots": "全局任务槽位", "Free disk space": "磁盘可用空间", "Priced cost estimate": "已定价费用估算",
    "Server health & recovery": "服务健康与恢复", "Read-only monitor · refreshed every minute": "只读监控 · 每分钟更新", "Backup and restart evidence": "备份与重启验证记录", "No host monitor report is available. Service and backup health cannot be confirmed.": "暂无主机监控报告，无法确认服务与备份状态。",
    "Stale · check monitoring": "数据已过期，请检查监控", "Checks healthy": "检查正常", "Attention recorded": "存在待处理事项", "Latest backup": "最近备份", "Restore checks passed": "恢复校验通过", "No verified backup": "暂无已验证备份", "Backup time (UTC+8)": "备份时间（UTC+8）",
    "Files / submission ZIPs checked": "已校验文件 / 提交ZIP数", "Storage location": "备份位置", "Offsite": "异地", "Same VPS · not offsite": "同一VPS（非异地）", "Controlled host reboot": "受控整机重启", "Passed": "通过", "Not verified": "尚未验证", "Reboot checked (UTC+8)": "重启验证时间（UTC+8）", "Unknown": "未知", "Unavailable": "不可用",
    "Model usage and costs": "模型用量与费用", "Unknown prices remain unpriced": "未知价格保留为未定价", "Configure estimated prices (USD / million tokens)": "配置估算价格（美元 / 百万Token）",
    "These are operator estimates, not provider invoices. Do not enter an unknown price. Saved rates apply to future calls and existing unpriced calls.": "此处为工作人员估价，不是供应商账单。未知价格请勿填入；保存后用于新调用及现有未定价调用。",
    "Input": "输入", "Cached input": "缓存输入", "Output": "输出", "Not confirmed": "尚未确认", "Save price version and calculate": "保存价格版本并计算", "Time (UTC+8)": "时间（UTC+8）", "Run / attempt": "运行 / 尝试", "Input / output tokens": "输入 / 输出Token", "Cost": "费用", "Unpriced": "未定价",
    "Competition calendar": "比赛日历", "Admission mode": "受理模式", "Open admission": "开放受理", "Scheduled window": "按时间窗口受理", "Configuration version": "配置版本", "Competition starts (UTC+8)": "比赛开始（UTC+8）", "Competition ends (UTC+8)": "比赛结束（UTC+8）", "Appeal deadline (UTC+8)": "申诉截止（UTC+8）",
    "Infrastructure window starts (optional, UTC+8)": "环境测试开始（可选，UTC+8）", "Infrastructure window ends (optional, UTC+8)": "环境测试结束（可选，UTC+8）", "Save calendar": "保存日历",
    "Enter all times in UTC+8. Use a new calendar version and record an operator and reason. Accepted evaluations retain their original policy. Calendar changes do not reopen manually closed admission.": "时间统一填写UTC+8。请使用新日历版本并记录操作理由。已接受评测保留原政策；修改日历不会重新开放手动关闭的受理。",
    "In scheduled mode, infrastructure tests require their own window. Calendar settings do not enable an unvalidated executor.": "按时间窗口受理时，环境测试须配置独立窗口。日历设置不能启用未经验证的执行环境。",
    "Advanced development diagnostics": "高级诊断", "Legacy single-task diagnostic": "历史单题诊断", "Staff only · unranked": "仅工作人员 · 不计榜", "Approved version ID": "已批准版本ID", "Task index in the configured release": "运行清单中的任务索引", "Bind diagnostic target": "绑定诊断目标",
    "Latest 100 entries": "最近100条", "Operator": "操作人", "Action": "操作", "Reason": "理由", "Before / after": "修改前 / 后", "Before": "修改前", "After": "修改后", "View": "查看", "Technical record": "原始技术记录", "Notification records (no email delivery)": "通知记录（不代表邮件送达）",
    "No audit records yet.": "暂无审计记录。", "No evaluations yet.": "暂无评测。", "No submissions yet.": "暂无提交。", "No pending reviews.": "暂无待审查提交。",
    "Account created. Share the temporary password privately.": "账号已创建，请通过私密渠道交付临时密码。", "Sessions revoked. The user must change the new temporary password at sign in.": "旧会话已撤销，用户下次登录必须修改临时密码。",
    "Account details (password excluded)": "账号详情（不含密码）", "Confirm": "确认", "Cancel": "取消", "Close dialog": "关闭弹窗",
    "The platform is temporarily unavailable. Please try again.": "平台暂时不可用，请稍后重试。", "Please sign in first.": "请先登录。", "Too many requests. Wait a moment and try again.": "请求过于频繁，请稍后重试。",
    "No matching accounts in the directory.": "没有匹配的账号。", "No matching evaluations in the recent window.": "近期记录中没有匹配的评测。",
    "Permission denied.": "没有权限。", "Invalid email or password.": "邮箱或密码错误。", "Please change your initial password first.": "请先修改初始密码。"
  };
  // Localized counterparts of the Go HTTP error contract.
  Object.assign(dictionary, {
  "Use the account issued for this workspace.": "请使用为此工作台开通的账号。",
  "The infrastructure testing window is not configured.": "Infra 开放时间尚未冻结",
  "Upload checks are busy. Try again shortly; no attempt was charged.": "上传检查繁忙，请稍后重试；未扣次数",
  "Cross-site changes are not allowed.": "不允许跨站修改请求",
  "This request method is not supported.": "不支持的请求方法",
  "The incident does not match the submission digest.": "事件与提交摘要不匹配",
  "The incident does not match the evaluation.": "事件与评测记录不匹配",
  "Public tests require all five public tasks to be configured.": "五题公开清单未配置完整，公开测试暂不可用",
  "Only approved official scientific templates can be bound to diagnostic targets.": "仅可绑定已审查的官方科学模板",
  "Staff access requires changing your temporary password first.": "仅完成首次改密的管理员可访问",
  "Only staff can publish announcements.": "仅管理员可发布公告",
  "Only staff can create accounts.": "仅管理员可开通账号",
  "Staff access required.": "仅管理员可访问",
  "Your session has expired. Sign in again.": "会话已失效，请重新登录",
  "This announcement is not accessible.": "公告不可访问",
  "The announcement must reference the current rules version.": "公告必须引用当前PRD规则版本",
  "Check the notice version, title and body lengths.": "公告版本、标题或内容长度无效",
  "This notice version is already published. Use a new version for corrections.": "公告版本已发布；修改内容请使用新版本",
  "Public and all-team announcements cannot link private team records.": "公开或全队公告不能关联单队资料",
  "The linked incident is invalid. Contact the organizers.": "关联事件记录无效，请联系赛事组",
  "The linked record is not accessible.": "关联记录不可访问",
  "Remaining attempts cannot be negative.": "剩余次数不能为负",
  "The ZIP must not exceed 20 MiB.": "压缩包不能超过 20 MiB",
  "The package failed isolated validation. Check its format or contact the organizers.": "压缩包未通过隔离检查；请检查格式或联系赛事组",
  "The package is too large.": "压缩包过大",
  "Only evaluations with an unresolved infrastructure failure can be retried.": "只有待处理平台故障可重试",
  "Only completed scores can be invalidated.": "只能对已完成成绩作废",
  "Replies must contain 3–8,000 characters.": "回复长度为3至8000个字符",
  "Insufficient storage. Uploads are paused; contact staff. No attempt was charged.": "存储可用空间不足，暂不接收上传；请联系赛事组，未扣次数",
  "The reviewed bytes do not match the uploaded package.": "审查字节与上传字节不一致",
  "Your password state changed. Sign in again.": "密码状态已变化，请重新登录后重试",
  "Passwords must contain 12–200 characters.": "密码长度为 12–200 个字符",
  "Too many sign-in attempts. Try again in 15 minutes.": "尝试次数过多，请 15 分钟后重试",
  "This request ID is already associated with a different staff action.": "幂等键已绑定其他管理操作",
  "This request ID is already associated with another request.": "幂等键已绑定其他请求",
  "Invalid submission digest format.": "提交摘要格式无效",
  "Submission format validation failed.": "提交格式检查失败",
  "Your new password must differ from the current one.": "新密码不能与当前密码相同",
  "Invalid announcement audience.": "无效公告范围",
  "Invalid page parameters.": "无效分页参数",
  "Invalid review decision.": "无效复核决定",
  "Invalid version digest.": "无效版本摘要",
  "Invalid sign-in details.": "无效登录参数",
  "Invalid evaluation parameters.": "无效评测参数",
  "Invalid account details.": "无效账号参数",
  "Invalid attempt adjustment.": "无效额度调整",
  "You do not have permission to access this resource.": "无权访问",
  "Storage capacity could not be checked. Uploads are paused; no attempt was charged.": "无法确认存储容量，暂不接收上传；未扣次数",
  "This calendar version already has different content. Use a new version and give a reason.": "日历版本已绑定不同内容，请使用新版本并说明理由",
  "Unknown staff action.": "未知管理操作",
  "The demo allows up to 25 submission versions per team.": "本地 Demo 每队最多保留 25 个版本",
  "Your team already has an active evaluation. Wait for it to finish or cancel it.": "本队已有未结束批次，请等待或取消",
  "The scanner version changed. Contact staff before retrying.": "检查器版本发生变化",
  "The scanner returned an invalid report.": "检查器返回无效报告",
  "This version already has a frozen diagnostic target. Use a new submission version.": "此版本已冻结验收目标；请使用新的提交版本",
  "This version already has evaluations. Its task binding cannot change.": "此版本已有评测，不能改变任务绑定",
  "Each team may submit one formal appeal.": "每队只有一次申诉机会",
  "Submission version not found.": "版本不存在",
  "Submission integrity check failed. Admission for this version is stopped.": "版本字节校验不一致，已停止受理",
  "The submission file is missing or invalid.": "版本文件缺失或无效",
  "Appeal not found.": "申诉不存在",
  "An appeal must contain 10–4,000 characters.": "申诉内容应为 10–4000 字符",
  "The appeal window has closed.": "申诉受理已截止",
  "This appeal has already been answered.": "申诉已经答复",
  "The requested target is unavailable.": "目标不可用",
  "Legacy execution supports only the fixed Codex infrastructure mock package.": "真实本地执行仅支持固定 Codex 参考包的 infra mock 题",
  "The scientific task set is not configured.": "真实科学任务集尚未配置",
  "The scientific task set does not match the active runtime release.": "科学任务集与活动执行版本不一致",
  "Scientific diagnostics require a configured official template and use an unranked infrastructure attempt.": "科学诊断仅支持已配置的三类官方原始模板，使用独立 Infra 练习额度，不参与排名",
  "Calendar configuration is required.": "缺少日历配置",
  "A valid request ID is required.": "缺少有效请求标识",
  "The request protection header is missing.": "缺少请求保护标记",
  "Evidence integrity or read validation failed.": "证据完整性或读取检查失败",
  "Evaluation not found.": "评测不存在",
  "The evaluation does not match the submission digest.": "评测与提交摘要不匹配",
  "Evaluation admission is closed.": "评测受理已关闭",
  "The evaluation window has closed.": "评测受理已截止",
  "The evaluation window has not opened yet.": "评测尚未开始",
  "This submission version has not been approved.": "该版本尚未通过审查",
  "This version already has a review decision.": "该版本已有审查结论",
  "No attempts remain for this evaluation type.": "该类型评测次数已用完",
  "Use a team account with its temporary password already changed.": "请使用已改密队伍账号",
  "Sign in with your team account and change its temporary password first.": "请先使用队伍账号完成改密",
  "Change your temporary password first.": "请先完成改密",
  "Please sign in first.": "请先登录",
  "An operator name, action reason and valid request ID are required.": "请填写操作者、操作理由和有效请求标识",
  "This request ID has already been used for different content.": "请求标识已用于不同内容",
  "Invalid request format.": "请求格式无效",
  "Invalid request format or unknown fields.": "请求格式无效或含未知字段",
  "The request is too large.": "请求过大",
  "Enter your operator name and action reason.": "请输入操作者和操作理由",
  "Enter your operator name and a reason of at least 3 characters.": "请输入操作者和至少 3 个字符的操作理由",
  "Enter finite, non-negative unit prices.": "请输入非负有限单价",
  "Account not found.": "账号不存在",
  "An account with this email already exists.": "账号已存在",
  "This trace is not accessible.": "轨迹不可访问",
  "Trace storage is unavailable.": "轨迹存储不可用",
  "Remote sandbox cleanup is unconfirmed. Reconcile resources before retrying or releasing the slot.": "远端沙箱尚未完成回收对账，不能重跑或释放运行槽",
  "The email or password is incorrect.": "邮箱或密码不正确",
  "Team not found.": "队伍不存在",
  "Team accounts cannot set staff-only resolution fields.": "队伍不能设置管理员处理字段",
  "The isolated scanner report is unavailable.": "隔离扫描报告不可用",
  "The isolated scanner report is invalid.": "隔离扫描报告无效",
  "The isolated scanner report version is invalid.": "隔离扫描报告版本无效",
  "The isolated scanner is unavailable. No attempt was charged.": "隔离检查器不可用；未扣次数",
  "The isolated scanner is temporarily unavailable. No attempt was charged.": "隔离检查器暂不可用；未扣次数",
  "Isolated validation failed. No attempt was charged.": "隔离检查器失败；未扣次数",
  "The isolated scanner is not configured. No attempt was charged.": "隔离检查器未配置；未扣次数",
  "The scanner image is unavailable. No attempt was charged.": "隔离检查器镜像不可用；未扣次数",
  "The diagnostic binding does not match the submission digest.": "验收绑定与提交摘要不匹配",
  "The diagnostic task number must be between 1 and 49.": "验收编号必须在 1–49 范围内"
});
  const normalize = s => s.replace(/\s+/g, ' ').trim();
  Object.assign(dictionary, {
    "Temporary password must be changed": "需修改临时密码", "No matching accounts.": "没有匹配的账号。", "No matching submissions.": "没有匹配的提交。",
    "Find a team or evaluation ID in loaded records": "在已加载记录中搜索队伍或评测ID", "Find a submission by team, name or SHA": "搜索提交的队伍、名称或SHA",
    "Search team accounts": "搜索队伍账号", "Search evaluations": "搜索评测", "Filter evaluation status": "筛选评测状态", "Search submissions": "搜索提交",
    "Development diagnostics only: bind an approved official template to a task index. Use a new team and version for each check. The target cannot be changed. Normal contestant submissions do not need this step. Agent limit: 300 seconds, followed by verification and archiving.": "仅供开发诊断：将已批准官方模板绑定到任务索引，每次检查使用新队伍与新版本，绑定后不可更改。普通参赛提交不需要此步骤。此旧诊断入口的Agent时限为300秒，随后评分并归档。",
    "Alerts are recorded locally. No email, SMS or chat delivery is configured. Backups on this VPS do not protect against losing the host or its disk. Historical attention records remain visible until reviewed.": "告警保存在本机，尚未配置邮件、短信或聊天送达。同机备份无法抵御主机或磁盘丢失。历史异常记录在复核前继续显示。",
    "HTTPS entry": "HTTPS入口", "TLS certificate expiry": "TLS证书有效期", "API health": "API健康", "Database integrity": "数据库完整性", "Disk space": "磁盘空间",
    "Verified backup within 8 hours": "8小时内已验证备份", "Last backup job": "最近备份任务", "Queue waiting over 30 minutes": "排队超过30分钟", "Historical / unresolved run records": "历史或未处理运行记录",
    "Account created": "账号已创建", "Full": "全榜", "Public": "公开", "Infra": "环境", "Open": "待处理", "Closed": "已关闭", "Resolved": "已解决",
    "Create a staff administrator with access to all teams and competition controls?": "确认创建可管理全部队伍和比赛设置的工作人员管理员？",
    "Close evaluation admission? Accepted evaluations will finish. This action cannot be reversed from this console.": "确认关闭评测受理？已接受的评测继续完成，无法在此后台撤销关闭。"
  });
  const patterns = [
    [/^Full (\d+) · Public (\d+) · Infra (\d+)$/, (_,a,b,c)=>`全榜 ${a} · 公开 ${b} · 环境 ${c}`],
    [/^Showing (\d+) of (\d+) loaded evaluations \((\d+) total\)\. The latest 100 are loaded; overview counts cover all records\.$/, (_,a,b,c)=>`显示已加载的 ${b} 条中的 ${a} 条评测（共 ${c} 条）。列表加载最近100条；总览统计覆盖全部记录。`],
    [/^(\d+) of (\d+) accounts$/, (_,a,b)=>`${a} / ${b} 个账号`],
    [/^(\d+) queued tasks · (\d+) running tasks$/, (_,a,b)=>`${a} 个任务排队 · ${b} 个任务运行`],
    [/^(\d+) unpriced · no cost assumed$/, (_,a)=>`${a} 次调用未定价，不假定免费`],
    [/^(\d+) tasks currently running across (\d+) configured slots\. (\d+) submissions await a decision\. (\d+) evaluations have attention status; these are separate from active execution\.$/, (_,a,b,c,d)=>`${b} 个槽位中有 ${a} 个任务运行；${c} 份提交待处理；${d} 个评测需跟进，与实际运行分别统计。`],
    [/^Host monitor: (.+)\. Open Operations for individual checks and backup evidence\.$/, (_,a)=>`主机监控：${({unavailable:'不可用',stale:'已过期','checks healthy':'检查正常','attention recorded':'待处理'})[a]||a}。请在“运行与资源”中查看各项检查和备份记录。`],
    [/^Page (\d+)$/, (_,a)=>`第 ${a} 页`], [/^(\d+) Under review$/, (_,a)=>`${a} 份审查中`],
    [/^(Review submissions|Follow evaluations|Manage team access|Help participants|Check services & backups|Inspect the audit trail) ↗$/, (_,a)=>dictionary[a]+' ↗'],
    [/^Sampled (.+) \(UTC\+8\)$/, (_,a)=>`采样时间 ${a}（UTC+8）`]
  ];
  Object.assign(dictionary, {
"ACCOUNT PROVISIONING":"账号开通", "Create accounts":"开通账号", "Create account":"创建账号", "Import accounts":"批量导入", "Participant accounts":"参赛账号", "Create access for one team or import your participant list.":"为单个队伍开通账号，或导入参赛队伍名单。",
"Create one account or import a team list. Every account gets its own temporary password.":"创建单个账号或导入队伍名单，每个账号使用独立的临时密码。",
"Account creation mode":"账号创建方式", "Single account":"单个创建", "Import team accounts":"批量创建队伍账号", "Reason for account creation":"开通账号的理由",
"Prepare your team list":"准备队伍名单", "Two columns: email and team. Up to 200 accounts per import. Passwords are generated separately for each account.":"填写邮箱和队伍名称两列，每次最多导入200个账号，系统为每个账号分别生成临时密码。",
"Download CSV template":"下载CSV模板", "Upload a CSV file":"上传CSV文件", "Or paste your CSV":"或直接粘贴CSV内容", "Review accounts →":"预览账号 →", "Review before creating":"创建前确认",
"All rows are validated together. If any email already exists, no accounts in this import are created.":"全部账号统一校验；如果任意邮箱已存在，本次不会创建任何账号。",
"Create all team accounts":"确认创建全部队伍账号", "Accounts created":"账号已创建", "Creation not confirmed":"创建结果待确认",
"Download the credentials before leaving this page. Share each account privately. First sign-in requires a password change; no email is sent.":"离开页面前请下载账号凭据，再逐个通过私密渠道交付。首次登录必须修改密码，本操作不会发送邮件。",
"Download credentials CSV":"下载账号凭据CSV", "Clear credentials from this page":"清除本页凭据", "Reset account access":"重置账号访问", "Select an account in the directory below":"从下方账号目录选择账号", "Evaluation admission":"评测受理控制",
"Generate password":"生成随机密码", "Show password":"显示密码", "Hide password":"隐藏密码", "Unique temporary password · change required at first sign-in":"每个账号独立的临时密码，首次登录需修改",
"Credentials have not been downloaded. Clear them from this page anyway?":"尚未下载账号凭据，仍要清除本页保存的临时密码吗？",
"CSV must be smaller than 256 KiB.":"CSV文件不能超过256 KiB。", "Invalid CSV quoting. Use the template and try again.":"CSV引号格式错误，请使用模板修正后重试。",
"Provide between 1 and 200 team accounts.":"请提供1至200个队伍账号。", "Could not read the CSV file. Please try again.":"无法读取CSV文件，请重试。", "Enter a reason of at least 3 characters.":"请填写至少3个字符的操作理由。",
"Accounts created. Download and share the credentials privately.":"账号已创建，请下载凭据并逐个私密交付。",
"Unexpected account receipt. Check the account directory before retrying.":"账号回执异常，请先核查账号目录，再决定是否重试。",
"Connection interrupted. Check the account directory before retrying. Download the prepared credentials for reconciliation; creation is not confirmed.":"连接中断，尚不能确认创建结果。请先下载已准备的凭据并核对账号目录，再决定是否重试。"
});
  patterns.push(
    [/^(\d+) accounts$/,(_,n)=>`${n} 个账号`],
    [/^Row (\d+): enter a valid email and a team name of 1–100 characters\.$/,(_,n)=>`第${n}行：请填写有效邮箱及1至100个字符的队伍名称。`],
    [/^Row (\d+): the temporary password must contain 12–200 characters\.$/,(_,n)=>`第${n}行：临时密码必须为12至200个字符。`],
    [/^Row (\d+): duplicate email in this import\. No accounts were created\.$/,(_,n)=>`第${n}行：导入名单中存在重复邮箱，未创建任何账号。`],
    [/^Row (\d+): this email already has an account\. No accounts were created; check the account directory before retrying\.$/,(_,n)=>`第${n}行：邮箱已有账号，本次未创建任何账号。请核查账号目录后再重试。`]
  );
  Object.assign(dictionary, {
    "Manage team access, review submissions and monitor competition operations.": "管理队伍账号、审核提交并监控赛事运行。",
    "The infrastructure test has not opened yet.": "基础设施测试尚未开放。",
    "Ask the assistant": "问问助手",
    "Console assistant": "后台助手",
    "Answers from the staff manual and live console data. Read-only: it cannot change anything.": "基于工作人员手册和后台实时数据回答。只读：不会做任何修改。",
    "Ask about any part of this console, for example:": "可以问后台的任何内容，例如：",
    "New conversation": "新对话",
    "Send": "发送",
    "Close assistant": "关闭助手",
    "Ask a question… (Enter to send, Shift+Enter for a new line)": "输入问题…（Enter 发送，Shift+Enter 换行）",
    "Infrastructure test is open: five public tasks, unranked.": "基础设施测试已开放：五道公开题，不计入排名。",
    "New submissions are closed. Accepted competition evaluations continue.": "已停止接收新提交；已受理的正式评测继续完成。",
    "Competition admission is open.": "正式比赛评测受理已开放。",
    "Server clock moved backwards across an event boundary; admission is paused.": "服务器时钟回拨跨越赛程边界，评测受理已暂停。",
    "Admission has been paused by the organizers.": "赛事组已暂停评测受理。",
    "Your session has expired. Sign in again to continue.": "登录已过期，请重新登录。",
    "Connection interrupted. Check your connection and try again.": "连接中断，请检查网络后重试。",
    "The request timed out. Check the current status before retrying.": "请求超时，请先核查当前状态再重试。",
    "Find a team or evaluation ID across all records": "在全部记录中搜索队伍或评测ID",
    "Server clock synchronization": "服务器时钟同步",
    "Original task limits": "任务原生限制", "Public-task practice configuration": "公开任务测试配置",
    "Test model": "测试模型", "Formal model": "正式模型", "Not configured · formal admission blocked": "未配置 · 正式评测受理将保持关闭",
    "Practice time limit / task": "每题测试时限", "Formal task limits": "正式任务限制", "Original per-task configuration": "逐题沿用原始配置",
    "Review status": "审查状态", "Previous": "上一页", "Next": "下一页",
    "The start date must be before the end date.": "起始时间不能晚于截止时间。",
    "Invalid history page.": "历史页码无效。", "Invalid history status.": "历史记录状态无效。", "Invalid history date.": "历史记录日期无效。",
    "History search is limited to 200 characters.": "历史记录搜索最多200个字符。"
  });
  patterns.push(
    [/^Page (\d+) of (\d+) · (\d+) records$/,(_,a,b,c)=>`第 ${a} / ${b} 页 · 共 ${c} 条记录`],
    [/^Showing (\d+) evaluations on this page\. Search and filters cover all records\.$/,(_,n)=>`本页显示 ${n} 条评测，搜索与筛选覆盖全部记录。`],
    [/^(\d+) pending · pending first$/,(_,n)=>`${n} 份待审查 · 优先显示待审查记录`],
    [/^Admission window \(UTC\+8\): (.+) to (.+); appeal deadline: (.+)\. Server acceptance time applies\.$/,(_,a,b,c)=>`正式评测受理时间（UTC+8）：${a} 至 ${b}；申诉截止：${c}。以服务器受理时间为准。`],
    [/^(.+) Next boundary: (.+) \(UTC\+8\)\.$/,(_,a,b)=>`${dictionary[a]||a} 下一阶段时间：${b}（UTC+8）。`]
  );
  let locale='en';
  try { locale=localStorage.getItem('qiqc.staff.locale')==='zh'?'zh':'en'; } catch {}
  window.staffLocale=locale;
  const original=new WeakMap();
  function translate(value) {
    if(locale!=='zh')return value;
    const key=normalize(value);
    let translated=dictionary[key];
    if(!translated)for(const [pattern,replace] of patterns)if(pattern.test(key)){translated=key.replace(pattern,replace);break;}
    return translated?value.replace(value.trim(),translated):value;
  }
  window.staffTranslate=translate;
  const nativeConfirm=window.confirm.bind(window);
  window.confirm=message=>nativeConfirm(translate(message));
  function eligible(node) {
    const p=node.parentElement;
    if(!p||p.closest('script,style,pre,code,[data-no-translate],.language-switch,#identity'))return false;
    // Table cells and review prose contain private user data. Translate
    // controls/status badges there; keep names, messages and recorded reasons.
    if(p.closest('td,.review-item,#announcements-list,#detail-summary')&&!p.closest('button,.pill,summary,label,[data-ui-text]'))return false;
    return true;
  }
  function refresh() {
    observer.disconnect();
    document.documentElement.lang=locale==='zh'?'zh-CN':'en';
    document.title=locale==='zh'?'QIQC | 赛事管理后台':'QIQC | Operations console';
    document.querySelectorAll('[data-locale]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.locale===locale)));
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    while(walker.nextNode()){
      const node=walker.currentNode;if(!eligible(node)||!node.data.trim())continue;
      let state=original.get(node);
      if(!state||node.data!==state.last)state={source:node.data,last:node.data};
      const next=translate(state.source);if(node.data!==next)node.data=next;
      state.last=next;original.set(node,state);
    }
    document.querySelectorAll('[placeholder],[aria-label],[title]').forEach(e=>{
      if(e.closest('[data-no-translate],.language-switch,pre,code'))return;
      for(const attr of ['placeholder','aria-label','title'])if(e.hasAttribute(attr)){
        const key='i18n'+attr.replace('-','');
        if(!e.dataset[key])e.dataset[key]=e.getAttribute(attr);
        e.setAttribute(attr,translate(e.dataset[key]));
      }
    });
    observer.observe(document.body,{subtree:true,childList:true,characterData:true});
  }
  let queued=false;
  const observer=new MutationObserver(()=>{if(!queued){queued=true;queueMicrotask(()=>{queued=false;refresh();});}});
  document.querySelectorAll('[data-locale]').forEach(button=>button.addEventListener('click',()=>{
    locale=button.dataset.locale;window.staffLocale=locale;
    try{localStorage.setItem('qiqc.staff.locale',locale);}catch{}
    refresh();
  }));
  refresh();
})();
