# Publishing to GitHub

The reviewed project name is **0xRemnant App Hub**. The repository slug is
`0xRemnant-App-Hub` (GitHub repository names cannot contain spaces).

On the workstation where your GitHub CLI is already authenticated, run this from
the prepared source directory:

```sh
gh auth status
python3 scripts/publish_github.py
```

The script checks publication files, initializes a local Git repository if needed,
stages only the reviewed source allowlist, uses your public GitHub login and
GitHub noreply email for the commit, creates a **public** repository in that
account, and pushes source, examples, documentation, and screenshots. It refuses
unexpected tracked files, an enclosing repository, or existing remotes. It never
force-pushes, rewrites history, or deletes an existing repository.

If the repository already exists, use ordinary Git commands after reviewing its
contents; this script intentionally does not overwrite it. Check the Actions tab
after uploading and address any failures before treating the release as tested.
The real Docker job is especially important because Docker execution was not
available in the preparation environment.

After the first successful run, configure GitHub secret scanning/push protection,
private vulnerability reporting, and branch protection when available. These are
repository/account settings and are not automatically enabled by committing files.

For a local download instead:

```sh
python3 scripts/make_release.py --output /tmp/0xRemnant-App-Hub.zip
```
