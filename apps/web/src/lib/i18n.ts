export type Locale = "zh" | "en";

export const translations: Record<Locale, Record<string, string>> = {
  zh: {
    // ─── Toolbar ───
    "toolbar.save": "保存",
    "toolbar.run": "运行",
    "toolbar.cancel": "取消",
    "toolbar.status.idle": "就绪",
    "toolbar.status.running": "运行中",
    "toolbar.status.paused": "已暂停",
    "toolbar.status.completed": "已完成",
    "toolbar.status.failed": "失败",
    "toolbar.clickToRename": "点击重命名",
    "toolbar.untitled": "未命名工作流",
    "toolbar.saveShortcut": "保存 (Ctrl+S)",
    "toolbar.runShortcut": "运行 (Ctrl+Enter)",
    "toolbar.cancelShortcut": "取消",
    "toolbar.appTitle": "Multi-Agent Studio",
    "toolbar.shortcutHint": "Ctrl+S / Ctrl+Enter",

    // ─── Sidebar ───
    "sidebar.title": "节点库",
    "sidebar.dragToCanvas": "拖拽到画布",

    // ─── ConfigPanel ───
    "config.label": "标签",
    "config.agentType": "Agent 类型",
    "config.description": "描述",
    "config.descriptionPlaceholder": "描述此人工步骤需要验证或审批的内容...",
    "config.closePanel": "关闭面板",

    // ─── OutputPanel ───
    "output.tab.llm": "LLM",
    "output.tab.shell": "终端",
    "output.tab.tools": "工具",
    "output.filter.allNodes": "所有节点",
    "output.collapse": "收起面板",
    "output.expand": "展开面板",

    // ─── ApprovalModal ───
    "approval.title": "需要人工审批",
    "approval.subtitle": "请在继续之前查看以下更改",
    "approval.loading": "加载更改中...",
    "approval.changes": "更改",
    "approval.noChanges": "没有可显示的更改。",
    "approval.reject": "拒绝",
    "approval.approve": "批准",

    // ─── ToolCallList ───
    "tools.noCalls": "暂无工具调用",
    "tools.call": "调用",
    "tools.result": "结果",

    // ─── LLMOutput ───
    "llm.waiting": "等待 LLM 输出...",

    // ─── CommandEditor ───
    "command.label": "命令",
    "command.placeholder": "例如: npm run build && npm test",

    // ─── PermissionsEditor ───
    "permissions.label": "权限",
    "permissions.tool": "工具",
    "permissions.allow": "允许",
    "permissions.deny": "拒绝",
    "permissions.ask": "询问",

    // ─── PromptEditor ───
    "prompt.label": "提示词",
    "prompt.placeholder": "输入此 Agent 的系统/用户提示词...",

    // ─── ModelSelector ───
    "model.label": "模型",
    "model.selectPlaceholder": "选择模型...",

    // ─── Workflow List Page ───
    "wfList.loading": "加载工作流中...",
    "wfList.loadFailed": "加载工作流失败",
    "wfList.retry": "重试",
    "wfList.emptyTitle": "还没有工作流",
    "wfList.emptyDesc": "创建你的第一个工作流，开始构建多 Agent 自动化流程。拖拽节点到画布，配置并运行。",
    "wfList.createFirst": "创建你的第一个工作流",
    "wfList.newWorkflow": "新建工作流",
    "wfList.deleteConfirm": "确定要删除此工作流吗？此操作无法撤销。",
    "wfList.deleteTooltip": "删除工作流",
    "wfList.justNow": "刚刚",
    "wfList.minAgo": "{n}分钟前",
    "wfList.hrAgo": "{n}小时前",
    "wfList.dayAgo": "{n}天前",
    "wfList.runs": "{n}次运行",

    // ─── Workflow Editor Page ───
    "wfEditor.loading": "加载工作流中...",
    "wfEditor.loadFailed": "加载工作流失败",
    "wfEditor.retry": "重试",
    "wfEditor.untitled": "未命名工作流",

    // ─── NODE_META ───
    "node.coder.label": "编码器",
    "node.coder.description": "编写和修改代码文件",
    "node.plan.label": "规划器",
    "node.plan.description": "分析任务并创建执行计划",
    "node.explore.label": "探索器",
    "node.explore.description": "搜索代码库并收集信息",
    "node.shell.label": "Shell",
    "node.shell.description": "执行 Shell 命令",
    "node.review.label": "审查器",
    "node.review.description": "审查代码更改并提供反馈",
    "node.human.label": "人工",
    "node.human.description": "暂停等待人工审批或输入",
  },

  en: {
    // ─── Toolbar ───
    "toolbar.save": "Save",
    "toolbar.run": "Run",
    "toolbar.cancel": "Cancel",
    "toolbar.status.idle": "Ready",
    "toolbar.status.running": "Running",
    "toolbar.status.paused": "Paused",
    "toolbar.status.completed": "Completed",
    "toolbar.status.failed": "Failed",
    "toolbar.clickToRename": "Click to rename",
    "toolbar.untitled": "Untitled Workflow",
    "toolbar.saveShortcut": "Save (Ctrl+S)",
    "toolbar.runShortcut": "Run (Ctrl+Enter)",
    "toolbar.cancelShortcut": "Cancel",
    "toolbar.appTitle": "Multi-Agent Studio",
    "toolbar.shortcutHint": "Ctrl+S / Ctrl+Enter",

    // ─── Sidebar ───
    "sidebar.title": "Nodes",
    "sidebar.dragToCanvas": "Drag to canvas",

    // ─── ConfigPanel ───
    "config.label": "Label",
    "config.agentType": "Agent Type",
    "config.description": "Description",
    "config.descriptionPlaceholder": "Describe what this human step should verify or approve...",
    "config.closePanel": "Close panel",

    // ─── OutputPanel ───
    "output.tab.llm": "LLM",
    "output.tab.shell": "Shell",
    "output.tab.tools": "Tools",
    "output.filter.allNodes": "All Nodes",
    "output.collapse": "Collapse panel",
    "output.expand": "Expand panel",

    // ─── ApprovalModal ───
    "approval.title": "Human Approval Required",
    "approval.subtitle": "Review the changes below before proceeding",
    "approval.loading": "Loading changes...",
    "approval.changes": "Changes",
    "approval.noChanges": "No changes to display.",
    "approval.reject": "Reject",
    "approval.approve": "Approve",

    // ─── ToolCallList ───
    "tools.noCalls": "No tool calls yet",
    "tools.call": "Call",
    "tools.result": "Result",

    // ─── LLMOutput ───
    "llm.waiting": "Waiting for LLM output...",

    // ─── CommandEditor ───
    "command.label": "Command",
    "command.placeholder": "e.g. npm run build && npm test",

    // ─── PermissionsEditor ───
    "permissions.label": "Permissions",
    "permissions.tool": "Tool",
    "permissions.allow": "Allow",
    "permissions.deny": "Deny",
    "permissions.ask": "Ask",

    // ─── PromptEditor ───
    "prompt.label": "Prompt",
    "prompt.placeholder": "Enter system/user prompt for this agent...",

    // ─── ModelSelector ───
    "model.label": "Model",
    "model.selectPlaceholder": "Select a model...",

    // ─── Workflow List Page ───
    "wfList.loading": "Loading workflows...",
    "wfList.loadFailed": "Failed to load workflows",
    "wfList.retry": "Retry",
    "wfList.emptyTitle": "No workflows yet",
    "wfList.emptyDesc": "Create your first workflow to start building multi-agent automations. Drag nodes onto the canvas, configure them, and run.",
    "wfList.createFirst": "Create Your First Workflow",
    "wfList.newWorkflow": "New Workflow",
    "wfList.deleteConfirm": "Are you sure you want to delete this workflow? This action cannot be undone.",
    "wfList.deleteTooltip": "Delete workflow",
    "wfList.justNow": "Just now",
    "wfList.minAgo": "{n}m ago",
    "wfList.hrAgo": "{n}h ago",
    "wfList.dayAgo": "{n}d ago",
    "wfList.runs": "{n} run(s)",

    // ─── Workflow Editor Page ───
    "wfEditor.loading": "Loading workflow...",
    "wfEditor.loadFailed": "Failed to load workflow",
    "wfEditor.retry": "Retry",
    "wfEditor.untitled": "Untitled Workflow",

    // ─── NODE_META ───
    "node.coder.label": "Coder",
    "node.coder.description": "Writes and modifies code files",
    "node.plan.label": "Planner",
    "node.plan.description": "Analyses tasks and creates execution plans",
    "node.explore.label": "Explorer",
    "node.explore.description": "Searches codebase and gathers information",
    "node.shell.label": "Shell",
    "node.shell.description": "Executes shell commands",
    "node.review.label": "Reviewer",
    "node.review.description": "Reviews code changes and provides feedback",
    "node.human.label": "Human",
    "node.human.description": "Pauses for human approval or input",
  },
};
