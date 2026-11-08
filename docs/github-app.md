# Connecting chirpo to GitHub

chirpo acts on GitHub as a **GitHub App**: it has its own identity ("chirpo-yourname[bot]"),
only sees the repositories you install it on, and only has the permissions you grant. Its
tokens expire after one hour and are renewed automatically.

This guide creates the App by hand, which is what you need for development and for a
self-hosted install. It takes about five minutes.

## 1. Create the App

Open <https://github.com/settings/apps/new> (for an organization:
`https://github.com/organizations/<org>/settings/apps/new`) and fill in:

| Field | Value |
| --- | --- |
| GitHub App name | Any unique name, e.g. `chirpo-dev-franck` |
| Homepage URL | `https://github.com/tekeufranck681/chirpo` or your own site |
| Webhook | Untick **Active** (chirpo polls GitHub for now) |
| Where can this GitHub App be installed? | **Only on this account** |

**Repository permissions**:

| Permission | Access | Used for |
| --- | --- | --- |
| Issues | Read and write | creating, assigning, labelling and closing issues |
| Pull requests | Read and write | listing pull requests and their status |
| Contents | Read and write | merging pull requests |
| Checks | Read-only | showing CI results on pull requests |
| Commit statuses | Read-only | showing CI results on pull requests |
| Metadata | Read-only | required by GitHub for every App |

Contents write also lets the App push code, so you can keep Contents on read-only until you
want chirpo to merge pull requests. Leave the other permissions on "No access". Later chirpo
features (repository creation, membership sync) will list the extra permissions they need.

Click **Create GitHub App**.

## 2. Get the App ID and private key

On the App's settings page:

1. Copy the **App ID** (a number).
2. Under **Private keys**, click **Generate a private key**. A `.pem` file downloads.
3. Move it into the chirpo folder as `secrets/github-app.pem`, readable only by you:

   ```bash
   mkdir -p secrets
   mv ~/Downloads/<app-name>.*.private-key.pem secrets/github-app.pem
   chmod 600 secrets/github-app.pem
   ```

`secrets/` is git-ignored. Treat this file like a password: anyone holding it can act as the App.
If it leaks, generate a new key on the same page and delete the old one.

## 3. Install the App on a repository

On the App's settings page, open **Install App**, choose your account, select
**Only select repositories**, and pick the repositories chirpo should manage.
Start with a test repository.

## 4. Configure chirpo

In your `.env`:

```bash
CHIRPO_GITHUB_APP_ID=123456
CHIRPO_GITHUB_APP_PRIVATE_KEY_PATH=/absolute/path/to/chirpo/secrets/github-app.pem
```

The path is used when you run chirpo directly on your machine. With Docker Compose, the
`secrets/` folder is mounted read-only into the containers and the path is set for you.

## 5. Check the connection

```bash
cd backend
uv run chirpo github whoami owner/repo
# or, with Docker Compose, from the repo root:
docker compose run --rm api chirpo github whoami owner/repo
```

Expected output:

```
Logged in as GitHub App chirpo-dev-franck
Can reach owner/repo (private repo)
  checks: read
  contents: write
  issues: write
  metadata: read
  pull_requests: write
  statuses: read
```

| Message | Fix |
| --- | --- |
| `Can't read the GitHub App private key` | Check the path and that the file exists. |
| `... is not a PEM private key` | Use the downloaded `.pem` file, unchanged. |
| `The GitHub App is not installed on owner/repo` | Install the App on that repo (step 3) and check the spelling. |
| `GitHub rejected chirpo's credentials (401)` | The App ID doesn't match the key, or the key was deleted. |

To see the repo the way chirpo does (open issues, and open pull requests with their CI result,
approvals and mergeability):

```bash
uv run chirpo github demo owner/repo           # read-only
uv run chirpo github demo owner/repo --write   # also creates, comments on and closes a test issue
```

## Development shortcut: a personal access token

For quick local experiments you can skip the App and create a
[fine-grained personal access token](https://github.com/settings/personal-access-tokens/new)
limited to one test repository, with the same repository permissions:

```bash
CHIRPO_GITHUB_DEV_TOKEN=github_pat_...
```

chirpo then acts as **you**, not as a bot, so its actions are attributed to your account.
The token is refused when `CHIRPO_ENV=production` and ignored when an App is configured.
