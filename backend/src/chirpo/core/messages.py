"""All user-facing bot text, in one place so it can be translated later."""

PONG = "pong"

HELP = """Hi, I'm chirpo. I turn your team chat into GitHub actions.

Commands:
/ping - check that I'm alive
/help - show this message
/about - who made me
/iam - link your GitHub account (once)
/iam_switch - link a different GitHub account

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
