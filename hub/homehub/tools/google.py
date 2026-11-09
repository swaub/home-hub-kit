from homehub.tools import Scope, ToolModule, ToolSpec

GMAIL = "mcp__claude_ai_Gmail__"
GCAL = "mcp__claude_ai_Google_Calendar__"
GMAIL_READ = ("search_threads", "get_thread", "get_message", "list_labels", "list_drafts", "get_draft")
GCAL_READ = ("list_calendars", "list_events", "search_events", "get_event", "suggest_time")
GCAL_WRITE = ("create_event", "update_event", "delete_event", "respond_to_event")
GMAIL_DRAFT = ("create_draft", "update_draft")
GDRIVE = "mcp__claude_ai_Google_Drive__"
GDOCS = "mcp__claude_ai_Google_Docs__"
GDRIVE_READ = ("search_files", "list_recent_files", "read_file_content", "get_file_metadata", "download_file_content")
GDRIVE_WRITE = ("create_file", "update_file")
GDRIVE_HIDDEN = ("share_file", "trash_file", "copy_file", "get_file_permissions")
GMAIL_WRITE = ("send_message", "reply", "forward", "delete_draft", "trash_message", "trash_thread",
               "untrash_message", "untrash_thread", "label_message", "label_thread", "unlabel_message", "unlabel_thread",
               "create_label", "update_label", "delete_label", "update_message_labels", "mark_message_spam", "mark_thread_spam",
               "unmark_message_spam", "unmark_thread_spam", "apply_sensitive_message_label", "apply_sensitive_thread_label")
HIDDEN_CONNECTORS = ("mcp__claude_ai_Vercel", "mcp__claude_ai_Higgsfield", "mcp__claude_ai_Claude_Docs")
PROMPT = ("You can read the user's Gmail and write drafts (create_draft and update_draft save a draft in their Drafts folder; you "
          "cannot send, reply, forward, delete or label email, so tell them the draft is waiting for them to review and send), "
          "search and read their Google Drive and Docs (creating or changing a file asks them first), and their Google "
          "Calendar (read freely; adding or changing events asks them first). When reading email aloud, summarize briefly: who "
          "it's from and what it's about, not whole messages. For packages use the packages tool first; search Gmail only "
          "for an order it doesn't list.")


def describe_calendar(action):
    def describe(tool_input):
        title = tool_input.get("summary") or tool_input.get("title") or "an event"
        return {"create": f"add '{title}' to your Google Calendar", "update": f"change '{title}' on your Google Calendar",
                "delete": "delete an event from your Google Calendar", "respond": "reply to a calendar invitation"}[action]

    return describe


def describe_drive(action):
    def describe(tool_input):
        name = tool_input.get("title") or tool_input.get("name") or tool_input.get("file_name") or "a file"
        return {"create_file": f"create '{name}' in your Google Drive", "update_file": f"change '{name}' in your Google Drive",
                "update_doc": "edit a Google Doc"}[action]

    return describe


def build_tools():
    tools = [ToolSpec(name=GMAIL + n, policy="auto", scope=Scope.personal("email"), untrusted_output=True, untrusted_source="email") for n in GMAIL_READ]
    tools += [ToolSpec(name=GCAL + n, policy="auto", scope=Scope.personal("calendar"), untrusted_output=True, untrusted_source="calendar") for n in GCAL_READ]
    tools += [ToolSpec(name=GCAL + n, policy="confirm", scope=Scope.personal("calendar"), outward=True, describe=describe_calendar(n.split("_")[0]))
              for n in GCAL_WRITE]
    tools += [ToolSpec(name=GMAIL + n, policy="auto", scope=Scope.personal("email")) for n in GMAIL_DRAFT]
    tools += [ToolSpec(name=GDRIVE + n, policy="auto", scope=Scope.personal("files"), untrusted_output=True, untrusted_source="Google Drive")
              for n in GDRIVE_READ]
    tools += [ToolSpec(name=GDOCS + "read_doc", policy="auto", scope=Scope.personal("files"), untrusted_output=True, untrusted_source="Google Docs")]
    tools += [ToolSpec(name=GDRIVE + n, policy="confirm", scope=Scope.personal("files"), outward=True, describe=describe_drive(n)) for n in GDRIVE_WRITE]
    tools += [ToolSpec(name=GDOCS + "update_doc", policy="confirm", scope=Scope.personal("files"), outward=True, describe=describe_drive("update_doc"))]
    tools += [ToolSpec(name=GDRIVE + n, policy="hidden") for n in GDRIVE_HIDDEN]
    tools += [ToolSpec(name=GMAIL + n, policy="hidden") for n in GMAIL_WRITE]
    tools += [ToolSpec(name=prefix, policy="hidden") for prefix in HIDDEN_CONNECTORS]
    return tools


MODULE = ToolModule(server="", tools=build_tools(), available=lambda cfg: cfg.features.google, prompt=PROMPT)
