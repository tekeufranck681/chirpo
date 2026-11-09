"""All user-facing bot text, in one place so it can be translated later."""

PONG = "pong"

HELP = """Hi, I'm chirpo. I turn your team chat into GitHub actions.

Commands:
/ping - check that I'm alive
/help - show this message
/about - who made me
/iam - link your GitHub account (once)
/iam_switch - link a different GitHub account
/link owner/repo - connect a repo to this chat
/issue <title> - create an issue in the default repo
/members - who's here, their role and GitHub account
/role @name admin|maintainer|member - change someone's role (admins)
/unlink owner/repo - disconnect a repo (admins)

Soon you'll be able to just mention me:
"@chirpo checkout crashes on Android, give it to Franck"."""

UNKNOWN_COMMAND = "I don't know that command. Try /help."

NOT_UNDERSTOOD_YET = "I can't understand free text yet. For now try /ping or /help."

ABOUT = """chirpo: manage your GitHub from your team chat, in plain language.
Open source, created by Tekeu Franck.
https://github.com/tekeufranck681/chirpo"""

IAM_CODE = """{name}, let's link your GitHub account.

1. Open {url}
2. Enter the code: {code}
3. Approve chirpo.

The code works for {minutes} minutes. I'll message you here when it's done.
Only approve if you sent /iam yourself. Never enter a code someone else gave you."""

IAM_JUST_SENT = (
    "I sent you a code less than a minute ago. Use it, "
    "or send the command again in a minute for a new one."
)

IAM_ALREADY_LINKED = (
    "You're already linked as @{login} on GitHub. To link a different account, send /iam_switch."
)

IAM_SENT_PRIVATELY = "I sent you a private message with the next step."

IAM_OPEN_PRIVATE_CHAT = (
    "I need to send you a code privately, but you haven't opened a private chat with me yet. "
    "Open {link}, press Start, then send /iam again."
)

IAM_ANONYMOUS = "You're posting anonymously, so I can't tell who you are. Send /iam as yourself."

IAM_DONE = (
    "Done: you're @{login} on GitHub. I'll check your access to each repo before acting for you."
)

IAM_DENIED = "You declined the request on GitHub, so nothing was linked. Send /iam to try again."

IAM_EXPIRED = "The code expired before it was approved. Send /iam for a new one."

GITHUB_LOGIN_UNAVAILABLE = (
    "GitHub account linking isn't set up on this chirpo server yet. Ask the person who runs it."
)

GITHUB_NOT_CONFIGURED = "GitHub isn't set up on this chirpo server yet. Ask the person who runs it."

SEND_IAM_HERE = "Link your GitHub account here first: send /iam in this chat."

NO_REPO_LINKED = "No repo is linked to this chat yet. An admin can add one: /link owner/repo"

# Refusals (core/permissions.Refusal)
LINK_GITHUB_FIRST = "I need to know who you are on GitHub first: send /iam."
ROLE_TOO_LOW = "Only a {role} can do that in this chat."
NO_REPO_ACCESS = (
    "Your GitHub account @{login} needs {permission} access to {repo} for that. "
    "Ask a repo admin on GitHub."
)
APP_NOT_INSTALLED = "I can't reach {repo} on GitHub: chirpo's GitHub App isn't installed there."
APP_INSTALL_HINT = " Install it on that repo: {url}"
LAST_ADMIN = "This chat needs at least one admin. Make someone else admin first."
FIRST_LINK_NEEDS_GROUP_ADMIN = (
    "The first repo in a group must be linked by one of the group's admins "
    "(they become chirpo's admin here)."
)

LINK_USAGE = "Tell me which repo: /link owner/repo"
LINKED = "Linked {repo}."
LINKED_DEFAULT = " It's the default repo for /issue."
LINKED_ADMIN = " You're the admin of this chat's workspace."
ALREADY_LINKED = "{repo} is already linked here."

UNLINK_USAGE = "Tell me which repo: /unlink owner/repo"
UNLINKED = "Unlinked {repo}."
NOT_LINKED_HERE = "{repo} isn't linked to this chat."

MEMBERS_TITLE = "People here:"
MEMBER_LINE = "- {name}{chat}: {role}, {github}"
MEMBER_GITHUB = "GitHub @{login}"
MEMBER_NOT_VERIFIED = "GitHub not linked yet"

ROLE_USAGE = "Use: /role @name admin|maintainer|member"
MEMBER_NOT_FOUND = (
    "I don't know {name} here. They need to have talked to me in this chat "
    "(and done /iam if you use their GitHub name)."
)
ROLE_CHANGED = "{name} is now {role}."
MEMBER_AMBIGUOUS = "Several people here match {name}. Use their GitHub name instead."
MEMBERS_MORE = "... and {count} more."

ISSUE_USAGE = "Give the issue a title: /issue Login button broken"
ISSUE_TITLE_TOO_LONG = "That title is too long for GitHub (256 characters at most)."
ISSUE_BODY = "Requested by @{login} via chirpo."
ISSUE_CREATED = "Created #{number}: {title}\n{url}"
ISSUE_FAILED = "GitHub didn't accept that issue ({reason}). Nothing was created."
