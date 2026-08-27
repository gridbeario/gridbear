"""Context injection for MS365 plugin."""

MS365_BASE_CONTEXT = """[Microsoft 365 Access]
You have access to Microsoft 365 services. Available operations:

**SharePoint:**
- m365_list_sites(tenant?) - List accessible SharePoint sites
- m365_list_files(tenant, site_id, folder_path?) - List files in a folder
- m365_read_file(tenant, site_id, file_path) - Read file content
- m365_write_file(tenant, site_id, file_path, content) - Write/update file
- m365_search_files(tenant, query) - Search for files

**Planner:**
- m365_list_plans(tenant, group_id?) - List Planner plans
- m365_list_tasks(tenant, plan_id, bucket_id?) - List tasks in a plan
- m365_get_task(tenant, task_id) - Get task details
- m365_create_task(tenant, plan_id, title, bucket_id?, due_date?, assigned_to?) - Create task
- m365_update_task(tenant, task_id, updates) - Update task fields
- m365_complete_task(tenant, task_id) - Mark task as complete

**OneDrive:**
- m365_list_drive_files(tenant, folder_path?) - List files in OneDrive
- m365_read_drive_file(tenant, file_path) - Read file from OneDrive
- m365_write_drive_file(tenant, file_path, content) - Write file to OneDrive

**Shared with me:**
- m365_list_shared() - List documents shared with you (Shared with me)
- m365_read_shared(sharing_url | drive_id, item_id) - Read a shared document's text

Use tags in your response to execute operations:
[M365_LIST_SITES tenant="tenant_name"]
[M365_LIST_FILES tenant="tenant_name" site_id="..." folder_path="/Documents"]
[M365_READ_FILE tenant="tenant_name" site_id="..." file_path="/path/to/file.txt"]
[M365_WRITE_FILE tenant="tenant_name" site_id="..." file_path="/path/to/file.txt" content="..."]
[M365_LIST_PLANS tenant="tenant_name"]
[M365_LIST_TASKS tenant="tenant_name" plan_id="..."]
[M365_CREATE_TASK tenant="tenant_name" plan_id="..." title="Task title"]
[M365_COMPLETE_TASK tenant="tenant_name" task_id="..."]
[M365_LIST_DRIVE_FILES tenant="tenant_name" folder_path="/"]
[M365_READ_DRIVE_FILE tenant="tenant_name" file_path="/path/to/file.txt"]
[M365_WRITE_DRIVE_FILE tenant="tenant_name" file_path="/path/to/file.txt" content="..."]
"""


def get_context() -> str:
    """Return the base MS365 context."""
    return MS365_BASE_CONTEXT
