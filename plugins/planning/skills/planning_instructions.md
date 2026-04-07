# Planning Tools

You have access to planning tools for managing task plans.

When the user asks you to plan or organize a complex task:
1. Break it into discrete, actionable steps
2. Call plan_create with a title, task list, and the conversation_id from your [Message Source] context
3. Wait for user approval before starting
4. When told to proceed, call plan_update(action: "start") with the plan_id
5. Execute tasks one by one:
   - Call plan_task_update(status: "in_progress") before starting each task
   - Do the work (use other tools as needed)
   - Call plan_task_update(status: "completed", result: "...") when done
   - If a task fails, call plan_task_update(status: "failed", result: "error description"). The plan will pause automatically.
6. Continue to the next task

If the user asks to modify the plan, use plan_update:
- To edit an existing task: use update_tasks with the task_id and new title/description
- To add new tasks: use add_tasks
- To remove tasks: use remove_tasks with task IDs
Prefer editing existing tasks over removing and re-adding them.
Keep task titles short and actionable. Include a brief result for each completed task.
