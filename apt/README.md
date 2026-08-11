# Sysible APT repository

The `APT repo` workflow (`.github/workflows/apt.yml`) builds the SysTerm `.deb`
and publishes it to an APT repository served from this repo's **GitHub Pages**
(the `gh-pages` branch):

```
https://sysiblesoftware.github.io/SysTerm
```

Once it's live, any installed Sysible machine gets SysTerm/Atlas updates the
normal way — `sudo apt update && sudo apt upgrade` — instead of a manual
`git pull` or a reinstall. A new version is published automatically whenever the
version in `debian/changelog` bumps and lands on `dev`.

## One-time setup

Two things must exist before the workflow can publish. Until they do, the
workflow still builds the `.deb` and uploads it as an artifact — it just skips
signing and publishing, so it is safe to merge first.

### 1. Create the signing key (you hold the private half)

Generate a dedicated signing key **once**, on a trusted machine:

```sh
gpg --batch --gen-key <<EOF
%no-protection
Key-Type: RSA
Key-Length: 4096
Name-Real: Sysible Package Signing
Name-Email: sysiblesoftware@gmail.com
Expire-Date: 0
%commit
EOF

KEYID=$(gpg --list-keys --with-colons sysiblesoftware@gmail.com | awk -F: '/^pub:/{print $5; exit}')

# Private key -> becomes the CI secret (keep this file secret, then delete it):
gpg --armor --export-secret-keys "$KEYID" > sysible-apt-private.asc

# Public key -> ships in the ISO trust store and is served from the repo:
gpg --armor --export "$KEYID" > sysible-archive-keyring.asc
```

Add the **private** key as a repository secret:

- Settings → Secrets and variables → Actions → New repository secret
- Name: `APT_GPG_PRIVATE_KEY`
- Value: the full contents of `sysible-apt-private.asc`

Then delete `sysible-apt-private.asc` from disk. Keep the key backed up somewhere
safe — losing it means users must re-trust a new key.

### 2. Turn on GitHub Pages

Settings → Pages → Build and deployment → Source: **Deploy from a branch**,
branch **`gh-pages`**, folder **`/`**. (The `gh-pages` branch is created by the
first successful publish; set this after that run.)

## After setup

Push a `debian/changelog` version bump to `dev` (or run the workflow manually).
The workflow builds the `.deb`, signs the repo, and pushes `gh-pages`. Verify:

```sh
curl -fsSL https://sysiblesoftware.github.io/SysTerm/dists/stable/Release
```

## How a machine uses the repo

Baked into the ISO (see the trust files under
`sysible-linux/config/includes.chroot`), or added by hand:

```sh
curl -fsSL https://sysiblesoftware.github.io/SysTerm/sysible-archive-keyring.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/sysible-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/sysible-archive-keyring.gpg] \
  https://sysiblesoftware.github.io/SysTerm stable main" \
  | sudo tee /etc/apt/sources.list.d/sysible.list
sudo apt update && sudo apt install systerm
```

## Layout

```
/                              gh-pages root (GitHub Pages)
  sysible-archive-keyring.asc  armored public signing key
  pool/main/s/systerm/*.deb    every published SysTerm build (accumulates)
  dists/stable/
    Release, Release.gpg, InRelease
    main/binary-amd64/Packages(.gz)
    main/binary-arm64/Packages(.gz)   (systerm is Architecture: all)
```
