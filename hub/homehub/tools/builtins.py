import platform

from homehub.tools import ToolModule, ToolSpec

SHELL_TOOL = "PowerShell" if platform.system() == "Windows" else "Bash"
WEB_PROMPT = "You can search the web and open web pages with WebSearch and WebFetch; every message carries today's date, so use it for prices and news."
FILES_PROMPT = "You can read and search files on this computer with Read, Glob and Grep."
SHELL_PROMPT = ("You can run {shell} commands, write files and edit files, but only when no dedicated tool fits, and always fill in the "
                "command's description field with a short plain-English summary because it is read aloud when asking the user to confirm.")


def describe_shell(tool_input):
    return tool_input.get("description") or "run a command on this computer"


def describe_file(action):
    def describe(tool_input):
        path = tool_input.get("file_path") or "a file"
        return f"{action} {path}"

    return describe


WEB = ToolModule(server="", prompt=WEB_PROMPT, tools=[
    ToolSpec(name="WebSearch", policy="auto", untrusted_output=True, untrusted_source="the web"),
    ToolSpec(name="WebFetch", policy="auto", untrusted_output=True, untrusted_source="the web"),
])
FILES = ToolModule(server="", prompt=FILES_PROMPT, tools=[
    ToolSpec(name="Read", policy="auto", untrusted_output=True, untrusted_source="a file"),
    ToolSpec(name="Glob", policy="auto"),
    ToolSpec(name="Grep", policy="auto", untrusted_output=True, untrusted_source="a file"),
])
SHELL = ToolModule(server="", prompt=SHELL_PROMPT.format(shell=SHELL_TOOL), tools=[
    ToolSpec(name=SHELL_TOOL, policy="confirm", outward=True, describe=describe_shell),
    ToolSpec(name="Write", policy="confirm", outward=True, describe=describe_file("write to")),
    ToolSpec(name="Edit", policy="confirm", outward=True, describe=describe_file("edit")),
])
HIDDEN_BUILTINS = ToolModule(server="", tools=[ToolSpec(name=n, policy="hidden") for n in ("Task", "Agent", "Cron", "CronCreate", "Workflow", "EnterWorktree")])
