"""All user-facing bot text, in one place so it can be translated later."""

import math

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
/context on|off - keep recent messages so I understand follow-ups (admins)
/personality off|light|playful - how chatty I am (admins)

Or just tell me what you need, mentioning me (in a private chat, just write):
"{bot} checkout crashes on Android, give it to Franck"
"{bot} move #12 to in progress and assign me"
For now I show what I would do; confirming and running it comes soon."""

UNKNOWN_COMMAND = "I don't know that command. Try /help."

RATE_LIMITED_MEMBER = "That's a lot of requests in a short time. Try again in {wait}."
RATE_LIMITED_TEAM = "This team has sent a lot of requests this hour. Try again in {wait}."
RATE_LIMITED_BUDGET = (
    "This team has used its AI allowance for today; it resets in {wait}. "
    "Commands like /issue still work."
)
IMAGE_TOO_BIG = "I couldn't read an image: it's over 4 MB."
IMAGE_UNSUPPORTED = "I couldn't read an attachment: I only read JPEG, PNG and WebP images."
IMAGE_UNAVAILABLE = "I couldn't get an image from the chat. Send it again if it matters."
IMAGES_LIMIT = "I only looked at the first 3 images."
FILES_NOT_READ = "I can only read images for now, so I left out the file."

WRITE_LIMITED = "That's a lot of changes on GitHub in a short time. Try again in {wait}."


ABOUT = """chirpo: manage your GitHub from your team chat, in plain language.
Open source, created by Tekeu Franck.
https://github.com/tekeufranck681/chirpo

In linked chats I keep recent messages for a limited time, encrypted, so I understand follow-ups
like "assign it to Franck". See /context."""

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
CONTEXT_NOTICE = (
    "\n\nI keep this chat's messages for {hours} hours, encrypted, and send recent ones with "
    'requests to the language model, so I understand follow-ups like "assign it to Franck". '
    "An admin can turn this off with /context off."
)
CONTEXT_IS_ON = (
    "I keep this chat's messages for {hours} hours as context. "
    "An admin can turn it off with /context off."
)
CONTEXT_IS_OFF = "I don't keep this chat's messages. An admin can turn context on with /context on."
CONTEXT_TURNED_ON = "Done: I'll keep this chat's messages for {hours} hours as context."
CONTEXT_TURNED_OFF = "Done: I no longer keep this chat's messages, and deleted the ones I had."
CONTEXT_OFF_FOR_INSTANCE = "This chirpo keeps no messages: its operator turned context off."
CONTEXT_USAGE = "Usage: /context on or /context off (admins). /context alone shows the setting."
PERSONALITY_IS = (
    "My tone here is {level}: off is short and neutral, light adds a friendly reply to thanks "
    "and greetings, playful also allows jokes. An admin can change it with /personality."
)
PERSONALITY_SET = "Done: my tone here is now {level}."
PERSONALITY_USAGE = (
    "Usage: /personality off, light or playful (admins). /personality alone shows the setting."
)
ALREADY_LINKED = "{repo} is already linked here."

UNLINK_USAGE = "Tell me which repo: /unlink owner/repo"
UNLINKED = "Unlinked {repo}."
NOT_LINKED_HERE = "{repo} isn't linked to this chat."

MEMBERS_TITLE = "People here:"
MEMBER_LINE = "- {name}{chat}: {role}, {github}"
MEMBER_GITHUB = "GitHub {login}"  # no "@": Telegram would ping whoever has that username
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
ISSUE_BODY = "Requested by @{login} via chirpo."  # the same line closes every posted text
ISSUE_CREATED = "Created #{number}: {title}\n{url}"
ISSUE_FAILED = "GitHub didn't accept that issue ({reason}). Nothing was created."

# Confirming and running (M5, D53)
BUTTON_CONFIRM = "✅ Confirm"
BUTTON_CANCEL = "✖ Cancel"
BUTTON_UNDO = "↩ Undo"
UNDONE_HEADER = "↩ Undone."
CONFIRM_FOOTER = "Only {name} or a maintainer can confirm."
RUNNING = "⏳ Working on it…"
CANCELLED = "Cancelled by {name}."
REQUESTED_BY = ISSUE_BODY
EXPIRED = "This request expired. Ask me again."
REPLACED = "Replaced by a newer proposal below."
USE_THE_BUTTONS = "Use Confirm or Cancel on the card to go ahead, or tell me what to change."
CARD_UNCHANGED = (
    "I wasn't sure how to change that card, so I left it as it is. "
    'Tell me what to change, for example "assign it to me".'
)
TOO_BIG_TO_CHANGE = "That card is too big to change by reply. Cancel it and ask again."
REPLACES_NOTE = "This replaces the earlier proposal."
STALE_NOTE = "I was offline when you sent this. Check it is still what you want before confirming."
CB_UNAVAILABLE = "That request isn't available any more."
CB_ALREADY = "That request was already handled."
CB_RUNNING = "Already on it."
CB_UNDO_LATE = "Too late to undo that one."
CB_UNDONE = "That was already undone."
CB_EXPIRED = "That request expired. Ask me again."
CB_NOT_ALLOWED = "Only {name} or a maintainer can confirm this."
CB_NOT_ALLOWED_ANYONE = "Only a maintainer can confirm this."
CB_ANONYMOUS = "Anonymous admins can't confirm. Use your own account."
CB_WRITE_LIMITED = "That's a lot of changes on GitHub in a short time. Try again in {wait}."
CB_CANCELLED = "Cancelled."
CB_STARTED = "On it."
DONE_UNDO_LABELS = "Put the labels on {issue} back as they were."
DONE_ASSIGNED = "Assigned {people} to {issue}."
DONE_UNASSIGNED = "Removed {people} from {issue}."
DONE_STATUS = '{issue} is now "{status}".'
DONE_ADDED_LABELS = "Added {labels} to {issue}."
DONE_REMOVED_LABELS = "Removed {labels} from {issue}."
DONE_COMMENTED = "Commented on {issue}.\n{url}"
DONE_CLOSED = "Closed {issue} as {reason}."
DONE_REOPENED = "Reopened {issue}."
STOPPED = "I stopped there: {reason}"
STOPPED_REST = "The remaining steps weren't run."
STEP_NO_LONGER_ALLOWED = "{name} isn't allowed to do that any more."
STEP_NOT_FOUND = "that issue or repo wasn't found on GitHub."
STEP_REFUSED = "GitHub refused it ({reason})."
STEP_WRITE_LIMITED = "too many changes on GitHub in a short time. Try again in {wait}."
STEP_CRASHED = "something unexpected went wrong. It may be partly done: check GitHub."
STEP_NOT_LINKED = "that repo isn't linked to this chat any more."
TOO_BIG = "That's too much to confirm in one message. Ask for fewer things at a time."
STEP_BROKEN = "something about this request doesn't add up. Nothing else was run."
NOT_RUN = "Something went wrong before I could do that, so nothing was changed. Try again."
CRASHED = "Something went wrong while running that. It may be partly done: check GitHub."

# Resolving what a message meant (D47)
PERSON_UNKNOWN = (
    "I don't know {name} here. They need to talk to me in this chat and link GitHub with /iam."
)
PERSON_NOT_LINKED = "{name} hasn't linked a GitHub account yet. They can send me /iam."
PERSON_AMBIGUOUS = "Several people match {name}: {options}. Which one? Use their @name."
REPO_NOT_LINKED = "{name} isn't linked to this chat. Linked repos: {repos}."
REPO_AMBIGUOUS = "Several linked repos are called {name}: {options}. Which one?"
ISSUE_NOT_FOUND_NUMBER = "There's no issue #{number} in {repo}."
ISSUE_NOT_FOUND = 'I can\'t find an open issue matching "{description}". Which issue number?'
ISSUE_NOT_FOUND_CLOSED = (
    'I can\'t find a closed issue matching "{description}". Which issue number?'
)
ISSUE_AMBIGUOUS = 'Several open issues match "{description}": {options}. Which one?'
LABELS_LEFT_OUT = "Left out, not labels in {repo}: {labels}."
NO_SUCH_LABELS = "{repo} has no label {labels}. Its labels: {existing}."
STATUS_UNKNOWN = 'I don\'t know the status "{status}". Use todo, in progress, review or done.'

# Plain-language requests (D47)
LLM_NOT_CONFIGURED = (
    "I can't understand plain language on this chirpo: no language model is set up "
    "(CHIRPO_LLM_API_KEY). Commands still work: /help"
)
LLM_BUSY = "I'm busy right now. Try again in a minute."
GITHUB_TROUBLE = "I couldn't reach GitHub just now. Try again in a minute."
LLM_REFUSED = "I can't help with that message."
LLM_UNCLEAR = "I couldn't work out what to do. Try rephrasing, or see /help."
LLM_UNAVAILABLE = "I can't reach my language model right now. Try again later; commands still work."
NOT_A_REQUEST = (
    'I can create and update issues for you, e.g. "{bot} checkout crashes on Android, '
    'give it to Franck". See /help.'
)
DRY_RUN_HEADER = "Here's what I'll do once it is confirmed:"
DRY_CREATE = '• Create an issue in {repo}: "{title}"'
DRY_ASSIGNED = "  assigned to {people}"
DRY_LABELS = "  labels: {labels}"
DRY_DETAILS = '  details: "{text}"'
DRY_ASSIGN = "• Assign {people} to {issue}"
DRY_UNASSIGN = "• Remove {people} from {issue}"
DRY_STATUS = '• Move {issue} to "{status}"'
DRY_ADD_LABELS = "• Add {labels} to {issue}"
DRY_REMOVE_LABELS = "• Remove {labels} from {issue}"
DRY_COMMENT = '• Comment on {issue}: "{text}"'
DRY_CLOSE = "• Close {issue} as {reason}"
DRY_REOPEN = "• Reopen {issue}"
DRY_FILTER_ASSIGNEE = " assigned to {login}"
DRY_FILTER_LABELS = " labelled {labels}"
DRY_FILTER_MILESTONE = " in {title}"
DRY_FILTER_DUE = " (due {day} {month})"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
NO_MILESTONE = "There's no open milestone in {repo}, so I can't tell which sprint you mean."
WHICH_MILESTONE = "Which milestone do you mean: {names}?"
MILESTONE_DUPLICATE = (
    'There are {count} open milestones called "{name}" in {repo}. Rename one on GitHub so I can '
    "tell them apart."
)
MILESTONE_UNKNOWN = 'I can\'t find an open milestone called "{name}" in {repo}. Open ones: {names}.'
DRY_ISSUE = '#{number} "{title}"'
ISSUES_TITLE = "{state} issues in {repo}{filters}:"
ISSUES_NONE = "No {state} issues in {repo}{filters}."
ISSUE_ITEM = "• #{number} {title}{people}"
ISSUE_PEOPLE = " ({people})"
ISSUE_ALSO = " (also {people})"
FILTER_YOURS = " assigned to you"
DRY_IN_REPO = " in {repo}"
DRY_NOTE = "  ({note})"
DRY_MORE = "… (+{count} more characters)"
CLOSE_REASONS = {"completed": "done", "not_planned": "not planned", "duplicate": "a duplicate"}


def duration(seconds: int) -> str:
    minutes = math.ceil(seconds / 60)
    if minutes <= 1:
        return "a minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours = math.ceil(minutes / 60)
    return "1 hour" if hours == 1 else f"{hours} hours"
