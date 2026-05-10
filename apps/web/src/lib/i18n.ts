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

    // ─── LeftPanel ───
    "leftPanel.tasks": "任务",

    // ─── ConfigPanel ───
    "config.label": "标签",
    "config.agentType": "Agent 类型",
    "config.title": "配置",
    "config.selectNodeHint": "在画布上选择节点以编辑配置",
    "config.description": "描述",
    "config.descriptionPlaceholder": "描述此人工步骤需要验证或审批的内容...",
    "config.closePanel": "关闭面板",
    "config.deleteNode": "删除节点",
    "config.workflowSettings": "工作流设置",
    "config.workspaceDirectory": "工作区目录",
    "config.workspaceDirectoryPlaceholder": "例如: /path/to/project 或 C:\\workspace",

    // ─── Edge Config ───
    "config.edgeTitle": "连线配置",
    "config.transferFiles": "文件传递",
    "config.transferFilesDesc": "下游节点是否复用上游的 sandbox（能看到上游的文件改动）",
    "config.enableFileTransfer": "启用文件传递",
    "config.transferSummary": "摘要注入",
    "config.transferSummaryDesc": "将上游节点的输出摘要注入下游节点的 prompt 中",
    "config.enableSummaryInjection": "启用摘要注入",
    "config.transferFormat": "传递格式",
    "config.transferFormatDesc": "选择上游数据传递到下游的方式",
    "config.formatSummary": "摘要 (Summary)",
    "config.formatFull": "完整输出 (Full)",
    "config.formatDiff": "文件差异 (Diff)",
    "config.edgeInfo": "连线定义了节点间的数据通道：执行顺序、文件继承和上下文传递。禁用文件传递时，下游节点将获得独立 sandbox。禁用摘要注入时，下游节点不会收到上游的输出信息。",
    "config.workflowMode": "工作流模式",
    "config.modeAutoHint": "自动模式：输入目标，Planner 自动规划并构建工作流 DAG",
    "config.modeManualHint": "手动模式：在画布上拖拽节点、连线，自定义工作流",

    // ─── OutputPanel ───
    "output.tab.llm": "LLM",
    "output.tab.shell": "终端",
    "output.tab.tools": "工具",
    "output.tab.comm": "通讯",
    "output.tab.chat": "Chat",
    "output.filter.allNodes": "所有节点",
    "output.collapse": "收起面板",
    "output.expand": "展开面板",
    "output.clearNodeFilter": "清除节点筛选",

    // ─── PlanNode ───
    "planNode.childTasks": "创建了 {n} 个子任务",
    "planNode.readonly": "规划模式",

    // ─── CommunicationPanel ───
    "comm.selectNode": "选择一个节点查看通讯记录",
    "comm.noRecords": "暂无通讯记录",
    "comm.receivedLlm": "收到 LLM 输出",
    "comm.sentLlm": "发送 LLM 输出",
    "comm.receivedToolCall": "收到工具调用",
    "comm.sentToolCall": "调用工具",
    "comm.receivedToolResult": "收到工具结果",
    "comm.sentToolResult": "返回工具结果",
    "comm.createdChild": "创建子任务",
    "comm.receivedChild": "子任务已创建",
    "comm.childCompleted": "子任务完成",

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
    "model.noModelsHint": "请先在设置中添加模型",

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

    // ─── Workflow Dual Mode ───
    "workflow.modeAuto": "自动规划",
    "workflow.modeManual": "手动工作流",
    "workflow.modeAutoDesc": "描述目标，Planner 自动构建工作流",
    "workflow.modeManualDesc": "在画布上设计节点和连线",
    "workflow.goalLabel": "目标",
    "workflow.goalPlaceholder": "描述你想要实现的目标...",

    // ─── Planner Chat ───
    "planner.startConversation": "开始对话",
    "planner.currentPlan": "当前方案",
    "planner.nodes": "个节点",
    "planner.edges": "条连线",
    "planner.inputPlaceholder": "描述你想要的修改，或说「运行」开始执行...",
    "planner.inputHint": "按 Enter 发送，Shift+Enter 换行。可以说「加一个审查步骤」或「让编码和探索并行执行」来修改工作流。",
    "planner.planPreview": "工作流预览",

    // ─── Settings ───
    "settings.title": "设置",
    "settings.save": "保存",
    "settings.cancel": "取消",
    "settings.saveSuccess": "设置已保存",
    "settings.tabGeneral": "通用",
    "settings.tabDisplay": "显示",
    "settings.tabModels": "模型",
    "settings.language": "界面语言",
    "settings.languageDesc": "切换应用显示语言",
    "settings.defaultWorkspace": "默认工作区目录",
    "settings.defaultWorkspacePlaceholder": "例如: /path/to/project 或 C:\\workspace",
    "settings.defaultWorkspaceDesc": "新建工作流时的默认工作区路径，留空则每次手动指定",
    "settings.theme": "主题",
    "settings.themeDesc": "选择应用的色彩主题，系统选项会跟随操作系统的深色/浅色模式",
    "settings.themeLight": "浅色",
    "settings.themeDark": "深色",
    "settings.themeSystem": "跟随系统",
    "settings.compactMode": "紧凑模式",
    "settings.compactModeDesc": "减小界面元素间距，在屏幕较小时可显示更多内容",
    "settings.openaiFormat": "OpenAI 兼容格式",
    "settings.openaiFormatDesc": "支持所有兼容 OpenAI API 格式的模型服务商（如 OpenAI、DeepSeek、Moonshot 等）",
    "settings.claudeFormat": "Anthropic 兼容格式",
    "settings.claudeFormatDesc": "Anthropic Claude 系列模型的 API 格式",
    "settings.baseUrl": "API Base URL",
    "settings.apiKey": "API Key",
    "settings.defaultModel": "默认模型",
    "settings.modelsInfo": "API Key 仅保存在本地配置文件中，不会上传到任何服务器。模型配置会用于所有新建的工作流节点。",
    "settings.addModel": "添加模型",
    "settings.modelList": "已配置模型",
    "settings.noModels": "暂无模型配置，请从左侧添加",
    "settings.modelFormat": "API 格式",
    "settings.modelName": "名称",
    "settings.testUrl": "测试连通性",
    "settings.deleteModel": "删除模型",
    "settings.testSuccess": "连通成功",
    "settings.testFailed": "连通失败",
    "settings.modelsAvailable": "个模型可用",
    "settings.fetchModels": "获取模型列表",
    "settings.availableModels": "可用模型",
    "settings.selectAll": "全选",
    "settings.deselectAll": "取消全选",
    "settings.addSelected": "添加选中",
    "settings.noModelsFetched": "连接成功但未返回模型列表，请手动输入模型名称",
    "settings.addCustomModel": "手动添加模型",

    // ─── Directory Picker ───
    "dirPicker.browse": "浏览",
    "dirPicker.pathValid": "路径有效",
    "dirPicker.pathNotExist": "路径不存在",
    "dirPicker.pathNotDir": "路径不是目录",
    "dirPicker.pathInvalid": "路径格式无效",
    "dirPicker.pathNoPermission": "当前用户无写入权限",
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

    // ─── LeftPanel ───
    "leftPanel.tasks": "Tasks",

    // ─── ConfigPanel ───
    "config.label": "Label",
    "config.agentType": "Agent Type",
    "config.title": "Config",
    "config.selectNodeHint": "Select a node on the canvas to edit its configuration",
    "config.description": "Description",
    "config.descriptionPlaceholder": "Describe what this human step should verify or approve...",
    "config.closePanel": "Close panel",
    "config.deleteNode": "Delete node",
    "config.workflowSettings": "Workflow Settings",
    "config.workspaceDirectory": "Workspace Directory",
    "config.workspaceDirectoryPlaceholder": "e.g. /path/to/project or C:\\workspace",

    // ─── Edge Config ───
    "config.edgeTitle": "Edge Configuration",
    "config.transferFiles": "File Transfer",
    "config.transferFilesDesc": "Whether the downstream node reuses the upstream sandbox (can see upstream file changes)",
    "config.enableFileTransfer": "Enable file transfer",
    "config.transferSummary": "Summary Injection",
    "config.transferSummaryDesc": "Inject upstream node output summary into the downstream prompt",
    "config.enableSummaryInjection": "Enable summary injection",
    "config.transferFormat": "Transfer Format",
    "config.transferFormatDesc": "Choose how upstream data is passed to downstream",
    "config.formatSummary": "Summary",
    "config.formatFull": "Full Output",
    "config.formatDiff": "File Diff",
    "config.edgeInfo": "Edges define data channels between nodes: execution order, file inheritance, and context passing. Disabling file transfer gives the downstream node an independent sandbox. Disabling summary injection prevents the downstream from receiving upstream output.",
    "config.workflowMode": "Workflow Mode",
    "config.modeAutoHint": "Auto mode: describe your goal, Planner automatically designs and builds the workflow DAG",
    "config.modeManualHint": "Manual mode: drag nodes and draw connections on canvas to customize your workflow",

    // ─── OutputPanel ───
    "output.tab.llm": "LLM",
    "output.tab.shell": "Shell",
    "output.tab.tools": "Tools",
    "output.tab.comm": "Comm",
    "output.filter.allNodes": "All Nodes",
    "output.collapse": "Collapse panel",
    "output.expand": "Expand panel",
    "output.clearNodeFilter": "Clear node filter",

    // ─── PlanNode ───
    "planNode.childTasks": "{n} child task(s) created",
    "planNode.readonly": "Planning mode",

    // ─── CommunicationPanel ───
    "comm.selectNode": "Select a node to view communication records",
    "comm.noRecords": "No communication records",
    "comm.receivedLlm": "Received LLM output",
    "comm.sentLlm": "Sent LLM output",
    "comm.receivedToolCall": "Received tool call",
    "comm.sentToolCall": "Called tool",
    "comm.receivedToolResult": "Received tool result",
    "comm.sentToolResult": "Returned tool result",
    "comm.createdChild": "Created child task",
    "comm.receivedChild": "Child task created",
    "comm.childCompleted": "Child task completed",

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
    "model.noModelsHint": "Please add models in Settings first",

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

    // ─── Workflow Dual Mode ───
    "workflow.modeAuto": "Auto Plan",
    "workflow.modeManual": "Manual Workflow",
    "workflow.modeAutoDesc": "Describe your goal, Planner builds the workflow",
    "workflow.modeManualDesc": "Design nodes and connections on canvas",
    "workflow.goalLabel": "Goal",
    "workflow.goalPlaceholder": "Describe what you want to achieve...",

    // ─── Planner Chat ───
    "planner.startConversation": "Start Conversation",
    "planner.currentPlan": "Current Plan",
    "planner.nodes": "node(s)",
    "planner.edges": "edge(s)",
    "planner.inputPlaceholder": "Describe changes, or say 'run' to execute...",
    "planner.inputHint": "Press Enter to send, Shift+Enter for new line. Say 'add a review step' or 'run coder and explorer in parallel' to modify the workflow.",
    "planner.planPreview": "Workflow Preview",

    // ─── Settings ───
    "settings.title": "Settings",
    "settings.save": "Save",
    "settings.cancel": "Cancel",
    "settings.saveSuccess": "Settings saved",
    "settings.tabGeneral": "General",
    "settings.tabDisplay": "Display",
    "settings.tabModels": "Models",
    "settings.language": "Language",
    "settings.languageDesc": "Switch the application display language",
    "settings.defaultWorkspace": "Default Workspace Directory",
    "settings.defaultWorkspacePlaceholder": "e.g. /path/to/project or C:\\workspace",
    "settings.defaultWorkspaceDesc": "Default workspace path for new workflows. Leave empty to specify manually each time.",
    "settings.theme": "Theme",
    "settings.themeDesc": "Choose the color theme. System option follows your OS dark/light mode.",
    "settings.themeLight": "Light",
    "settings.themeDark": "Dark",
    "settings.themeSystem": "System",
    "settings.compactMode": "Compact Mode",
    "settings.compactModeDesc": "Reduce spacing between UI elements to show more content on smaller screens.",
    "settings.openaiFormat": "OpenAI Compatible",
    "settings.openaiFormatDesc": "Supports all providers with OpenAI-compatible API format (e.g. OpenAI, DeepSeek, Moonshot)",
    "settings.claudeFormat": "Claude Format",
    "settings.claudeFormatDesc": "API format for Anthropic Claude series models",
    "settings.baseUrl": "API Base URL",
    "settings.apiKey": "API Key",
    "settings.defaultModel": "Default Model",
    "settings.modelsInfo": "API Keys are stored locally only and never uploaded to any server. Model config will be used for all new workflow nodes.",
    "settings.addModel": "Add Model",
    "settings.modelList": "Configured Models",
    "settings.noModels": "No models configured. Add one from the left.",
    "settings.modelFormat": "API Format",
    "settings.modelName": "Name",
    "settings.testUrl": "Test Connectivity",
    "settings.deleteModel": "Delete Model",
    "settings.testSuccess": "Connection successful",
    "settings.testFailed": "Connection failed",
    "settings.modelsAvailable": "model(s) available",
    "settings.fetchModels": "Fetch Models",
    "settings.availableModels": "Available Models",
    "settings.selectAll": "Select All",
    "settings.deselectAll": "Deselect All",
    "settings.addSelected": "Add Selected",
    "settings.noModelsFetched": "Connected but no model list returned. Please enter model name manually.",
    "settings.addCustomModel": "Add Custom Model",

    // ─── Directory Picker ───
    "dirPicker.browse": "Browse",
    "dirPicker.pathValid": "Path is valid",
    "dirPicker.pathNotExist": "Path does not exist",
    "dirPicker.pathNotDir": "Path is not a directory",
    "dirPicker.pathInvalid": "Invalid path format",
    "dirPicker.pathNoPermission": "No write permission",
  },
};
